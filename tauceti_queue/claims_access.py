"""Feed TauCeti's claim access from the merge queue: Forgejo landings and a prompt trigger.

TauCeti's `claims-access` workflow (TauCetiTools `claims_access.py`) invites the author of every landed
pull request to the shared claim repository, TauCetiProject/tauceti-claims, and
`forgejo_team_sync` then mirrors those collaborators into the Forgejo `contributor-fleets` team.
Two things stop that working from the workflow alone, and this job, run every ten minutes on the
queue host by `tauceti-queue-claims-access.timer`, supplies both.

Attribution. The workflow reads GitHub, so it sees only pull requests landed there. A Forgejo
pull request the queue landed is attributed here, from evidence only this host holds:

1. The queue's own ledger (`inputs`, state `landed`, with a landing time) names the landed Forgejo
   pull requests of the configured repository, and the Forgejo login each was admitted under.
2. For each author, Forgejo's answer for their most recently landed pull request gives its poster's
   numeric user ID. A login can be renamed and then taken by someone else; the poster's ID cannot.
3. Forgejo's site-admin user listing gives that user's sign-in source and `login_name`. An account
   created by signing in with GitHub (`--github-source-id`) holds the GitHub numeric user ID there,
   which only a site administrator can change (the field `forgejo_team_sync` matches on). Any other
   account is skipped, so a local account named like a GitHub user is never anyone's.

The result, each landed Forgejo author's GitHub user ID with the pull request that proves it, is
written to `claims-forgejo-landers.json` next to the ledger, and the queue server publishes it at
GET /claims/forgejo-landers, where the workflow reads it and resolves each ID to a login on GitHub.

Schedule. The workflow is scheduled hourly, but GitHub has started those runs only every five to
eleven hours. A `workflow_dispatch` starts promptly, so this dispatches the workflow, as the queue's
GitHub App (`actions: write`), whenever a landed author appears that it has not dispatched for yet,
on either forge, and otherwise once every `--dispatch-interval` seconds. The schedule stays as the
fallback. A dispatch that fails is retried on the next run.

Credentials: the Forgejo token of `forgejo_team_sync` (`read:admin`), and the queue App's
credential directory (docs/queue/operations.md). Neither is written anywhere or logged.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

SCHEMA = "tauceti-queue.claims-forgejo-landers/v1"
STATE_SCHEMA = "tauceti-queue.claims-access-state/v1"
OUTPUT_NAME = "claims-forgejo-landers.json"
GITHUB_API = "https://api.github.com"


def log(message):
    print(f"claims-access: {message}", file=sys.stderr, flush=True)


def forgejo_get(url, token=None):
    """A Forgejo API read; `token` only for the site-admin listing. The repository is public, and the
    team-sync token is scoped to `read:admin` and `write:organization`, which do not read pull requests."""
    headers = {"Accept": "application/json", "User-Agent": "tauceti-claims-access"}
    if token:
        headers["Authorization"] = f"token {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return json.loads(response.read() or b"null")


def _source_matches(identity, forgejo_url, repository_id):
    """Is this stored source identity the configured Forgejo repository?"""
    try:
        parsed = json.loads(identity)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    forge = parsed.get("forge") if isinstance(parsed.get("forge"), dict) else {}
    return (
        forge.get("kind") == "forgejo"
        and str(forge.get("url") or "").rstrip("/") == forgejo_url.rstrip("/")
        and parsed.get("id") == repository_id
    )


def ledger_landings(database, repository, forgejo_url, repository_id):
    """({Forgejo author: [landed PR numbers, newest first]}, {GitHub authors}) from the queue ledger.

    Read-only, and only inputs with a landing time: a landing receipt, the evidence `statistics`
    counts. The GitHub authors are only used to notice a new one, never to grant anything: the
    workflow checks GitHub itself."""
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as c:
        c.execute("PRAGMA query_only=ON")
        rows = c.execute(
            "SELECT author, number, source_identity FROM inputs WHERE repository=? AND state='landed' "
            "AND landed_at IS NOT NULL ORDER BY landed_at DESC, id DESC",
            (repository,),
        ).fetchall()
    forgejo, github = {}, set()
    for author, number, identity in rows:
        if not author:
            continue
        if not identity:
            github.add(author)
        elif _source_matches(identity, forgejo_url, repository_id):
            numbers = forgejo.setdefault(author, [])
            if number not in numbers:
                numbers.append(number)
    return forgejo, github


def forgejo_users(base, token, get=forgejo_get):
    """{numeric user ID: user} from the site-admin listing, which is what exposes `login_name`."""
    users, page = {}, 1
    while True:
        batch = get(f"{base}/api/v1/admin/users?limit=50&page={page}", token)
        if not batch:
            return users
        for user in batch:
            users[user["id"]] = user
        page += 1


def attribute(landed, base, repository, repository_id, token, source_id, get=forgejo_get, posters=None, tries=3):
    """[{github_id, forgejo_pr}]: each landed PR, up to `tries` per ledger author, attributed by its
    poster's numeric ID, deduplicated by GitHub ID. The ledger's author is a login, which can be renamed
    and then taken by another account, so it only selects PRs to read: every one read is attributed
    independently, and a PR by an account that is not a GitHub sign-in skips that PR alone. `posters`,
    {PR number: poster ID}, caches what is immutable, and is updated in place. A PR that cannot be read
    is skipped this run."""
    posters = {} if posters is None else posters
    users = forgejo_users(base, token, get)
    found = {}
    for author in sorted(landed):
        for number in landed[author][:tries]:
            poster = posters.get(str(number))
            if poster is None:
                try:
                    pr = get(f"{base}/api/v1/repos/{quote(repository, safe='/')}/pulls/{number}", None)
                except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
                    log(f"could not read Forgejo PR {number} of {author}: {error}")
                    continue
                base_repo = ((pr or {}).get("base") or {}).get("repo") or {}
                poster = ((pr or {}).get("user") or {}).get("id")
                if base_repo.get("id") != repository_id or type(poster) is not int:
                    log(f"Forgejo PR {number} of {author} is not a PR of repository {repository_id}; skipped")
                    continue
                posters[str(number)] = poster
            account = users.get(poster)
            name = str((account or {}).get("login_name") or "")
            if not account or account.get("source_id") != source_id or not name.isdigit():
                log(f"Forgejo PR {number} was posted by Forgejo user {poster}, not a GitHub sign-in; skipped")
                continue
            github_id = int(name)
            found.setdefault(github_id, {"github_id": github_id, "forgejo_pr": number})
    return sorted(found.values(), key=lambda row: row["github_id"])


def write_json(path, value):
    """Replace `path` atomically, so the server never reads half a file."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def read_state(path):
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"dispatched_at": 0, "authors": []}
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        return {"dispatched_at": 0, "authors": []}
    return state


def due(state, authors, now, interval):
    """Why to dispatch now, or None: a landed author not yet dispatched for, or the interval."""
    new = sorted(set(authors) - set(state.get("authors") or []))
    if new:
        return f"{len(new)} landed author(s) not yet dispatched for"
    if now - float(state.get("dispatched_at") or 0) >= interval:
        return "the dispatch interval has passed"
    return None


def dispatch(repository, workflow, ref, credentials_dir):
    """Start the workflow on `ref` as the queue's App, with a token limited to `actions: write`."""
    from . import github_app
    from .github_adapter import UrllibTransport

    transport = UrllibTransport()
    credentials = github_app.AppCredentials.load(credentials_dir, webhook=False)
    tokens = github_app.InstallationTokenSource(credentials, transport, permissions={"actions": "write"})
    url = f"{GITHUB_API}/repos/{repository}/actions/workflows/{quote(workflow)}/dispatches"
    headers = {
        "Authorization": f"Bearer {tokens.token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "tauceti-claims-access",
    }
    status, _, body = transport("POST", url, headers, json.dumps({"ref": ref}).encode())
    if status != 204:
        raise RuntimeError(f"dispatching {workflow} on {repository} failed with HTTP {status}: {body[:300]!r}")


def run(args, now=None, get=forgejo_get, dispatcher=dispatch):
    now = time.time() if now is None else now
    base = args.forgejo_url.rstrip("/")
    landed, github_authors = ledger_landings(args.database, args.repository, base, args.forgejo_repository_id)
    output = Path(args.output) if args.output else Path(args.database).with_name(OUTPUT_NAME)
    state_path = Path(args.state) if args.state else Path(args.database).with_name("claims-access-state.json")
    state = read_state(state_path)
    posters = dict(state.get("posters") or {})
    status = 0
    try:
        landers = attribute(
            landed,
            base,
            args.forgejo_repository,
            args.forgejo_repository_id,
            args.forgejo_token,
            args.github_source_id,
            get,
            posters,
        )
    except Exception as error:  # noqa: BLE001 - a Forgejo outage must not stop the dispatch below
        # The previous attribution stays published: it was true when written, and grants are monotone.
        log(f"FAILED to attribute Forgejo landings, keeping {output} as it is: {error}")
        landers, status = None, 1
    if landers is not None:
        document = {
            "schema": SCHEMA,
            "generated_at": round(now, 3),
            "repository": args.repository,
            "forgejo": {"url": base, "repository": args.forgejo_repository, "id": args.forgejo_repository_id},
            "definition": (
                "the GitHub user ID of the poster of a Forgejo pull request the merge queue landed, read from the "
                "site-admin record of a Forgejo account created by signing in with GitHub; one pull request each"
            ),
            "landers": landers,
        }
        if args.dry_run:
            log(f"would write {output}: {len(landers)} Forgejo lander(s): {json.dumps(landers)}")
        else:
            write_json(output, document)
            log(f"wrote {output}: {len(landers)} Forgejo lander(s) of {len(landed)} Forgejo author(s)")
    else:
        # Keep the authors already dispatched for, so that the outage alone does not look like news.
        landers = [
            {"github_id": int(a.split(":", 1)[1])} for a in state.get("authors") or [] if a.startswith("github-id:")
        ]
    authors = sorted(
        {f"github:{login}" for login in github_authors} | {f"github-id:{row['github_id']}" for row in landers}
    )
    reason = due(state, authors, now, args.dispatch_interval)
    dispatched_at, dispatched_for = state.get("dispatched_at") or 0, state.get("authors") or []
    if reason is not None and args.dry_run:
        log(f"would dispatch {args.workflow} on {args.repository}@{args.ref}: {reason}")
    elif reason is not None:
        try:
            dispatcher(args.repository, args.workflow, args.ref, args.config)
        except Exception as error:  # noqa: BLE001 - reported, and retried on the next run
            log(f"FAILED to dispatch {args.workflow}: {error}")
            status = 1
        else:
            dispatched_at, dispatched_for = round(now, 3), authors
            log(f"dispatched {args.workflow} on {args.repository}@{args.ref}: {reason}")
    if not args.dry_run:
        write_json(
            state_path,
            {"schema": STATE_SCHEMA, "dispatched_at": dispatched_at, "authors": dispatched_for, "posters": posters},
        )
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", type=Path, required=True, help="the queue ledger, opened read-only")
    parser.add_argument("--repository", default="TauCetiProject/TauCeti", help="the ledger's repository")
    parser.add_argument("--forgejo-url", default="https://git.taucetiproject.org")
    parser.add_argument("--forgejo-repository", default="TauCetiProject/TauCeti")
    parser.add_argument("--forgejo-repository-id", type=int, default=1)
    parser.add_argument("--forgejo-token-file", type=Path, required=True)
    parser.add_argument("--github-source-id", type=int, default=1, help="Forgejo's GitHub OAuth source")
    parser.add_argument("--output", type=Path, help=f"default: {OUTPUT_NAME} next to the ledger")
    parser.add_argument("--state", type=Path, help="default: claims-access-state.json next to the ledger")
    parser.add_argument("--workflow", default="claims-access.yml")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--dispatch-interval", type=float, default=3600)
    parser.add_argument("--config", type=Path, help="the queue App's credential directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.forgejo_token = args.forgejo_token_file.read_text().strip()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()

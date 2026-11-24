"""Mirror who may take shared claims on GitHub into a Forgejo team.

The direct collaborators of the GitHub claim repository (TauCetiProject/tauceti-claims) are the
people whose workers may coordinate through shared claims. TauCeti's `claims-access` workflow adds
every author of a landed PR, and a maintainer removes abuse by hand there. On Forgejo, native claims
are refs in the content repository itself, so the same people need a team granting code write on it
(`contributor-fleets`). This makes that team's membership equal to those collaborators.

A Forgejo account matches a GitHub collaborator only through its GitHub sign-in: Forgejo stores the
GitHub numeric user ID as the `login_name` of accounts created through the GitHub OAuth source. An
account with a different source is never added, only reported. One direction, idempotent: rerunning
with nothing changed on GitHub changes nothing on Forgejo.

Credentials: a GitHub token that can list the claim repository's collaborators (read with
`gh auth token --user LOGIN`), and a Forgejo token of a site administrator with the `read:admin` and
`write:organization` scopes (admin user listing exposes the GitHub ID; team membership needs
organization ownership or site administration).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

GITHUB_API = "https://api.github.com"


def log(message):
    print(f"forgejo-team-sync: {message}", file=sys.stderr, flush=True)


def request(method, url, token, scheme="token"):
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"{scheme} {token}", "Accept": "application/json", "User-Agent": "tauceti-team-sync"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        body = response.read()
        return (json.loads(body) if body else None), response.headers.get("Link", "")


def github_collaborator_ids(repository, token, get=request):
    """Numeric IDs of the repository's direct collaborators (all pages)."""
    ids = set()
    url = f"{GITHUB_API}/repos/{repository}/collaborators?affiliation=direct&per_page=100"
    while url:
        rows, link = get("GET", url, token, "Bearer")
        ids.update(row["id"] for row in rows)
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None
    return ids


def forgejo_pages(base, path, token, get=request):
    rows, page = [], 1
    separator = "&" if "?" in path else "?"
    while True:
        batch, _ = get("GET", f"{base}/api/v1/{path}{separator}limit=50&page={page}", token)
        if not batch:
            return rows
        rows.extend(batch)
        page += 1


def plan(users, members, github_ids, source_id):
    """(add, remove, unlinked): Forgejo logins to add to and remove from the team, and accounts
    that are not GitHub sign-ins, so cannot be matched."""
    linked = {}
    unlinked = []
    for user in users:
        name = user.get("login_name") or ""
        if user.get("source_id") == source_id and name.isdigit():
            linked[user["login"]] = int(name)
        else:
            unlinked.append(user["login"])
    desired = {login for login, github_id in linked.items() if github_id in github_ids}
    current = {member["login"] for member in members}
    return sorted(desired - current), sorted(current - desired), sorted(unlinked)


def sync(args, get=request):
    github_ids = github_collaborator_ids(args.github_repository, args.github_token, get)
    if not github_ids:
        raise SystemExit(f"{args.github_repository} reported no collaborators; changing nothing")
    base = args.forgejo_url.rstrip("/")
    teams = forgejo_pages(base, f"orgs/{quote(args.org)}/teams", args.forgejo_token, get)
    [team] = [team for team in teams if team["name"] == args.team]
    users = forgejo_pages(base, "admin/users", args.forgejo_token, get)
    members = forgejo_pages(base, f"teams/{team['id']}/members", args.forgejo_token, get)
    add, remove, unlinked = plan(users, members, github_ids, args.github_source_id)
    if unlinked:
        log(f"skipped Forgejo accounts not signed in through GitHub: {', '.join(unlinked)}")
    for verb, method, logins in (("add", "PUT", add), ("remove", "DELETE", remove)):
        for login in logins:
            if args.dry_run:
                log(f"would {verb} {login} ({args.team})")
                continue
            get(method, f"{base}/api/v1/teams/{team['id']}/members/{quote(login)}", args.forgejo_token)
            log(f"{verb} {login} ({args.team})")
    log(f"{len(members) + len(add) - len(remove)} members of {args.team}; {len(add)} added, {len(remove)} removed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github-repository", default="TauCetiProject/tauceti-claims")
    parser.add_argument("--github-user", required=True, help="gh account whose token lists collaborators")
    parser.add_argument("--forgejo-url", default="https://git.taucetiproject.org")
    parser.add_argument("--forgejo-token-file", type=Path, required=True)
    parser.add_argument("--org", default="TauCetiProject")
    parser.add_argument("--team", default="contributor-fleets")
    parser.add_argument("--github-source-id", type=int, default=1, help="Forgejo's GitHub OAuth source")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.github_token = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com", "--user", args.github_user],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    args.forgejo_token = args.forgejo_token_file.read_text().strip()
    sync(args)


if __name__ == "__main__":
    main()

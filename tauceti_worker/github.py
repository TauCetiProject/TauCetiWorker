"""tauceti_worker.github — the gh CLI wrapper: PR/issue queries, reactions, fork resolution, and
GitHub REST rate-limit handling."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .config import Die, log, one_line
from .constants import (
    _GH_PRIMARY_RE,
    _GH_SECONDARY_RE,
    _GH_TRANSIENT_RE,
    _GQL_MUTATION_RE,
    CLAIMS,
    CONTEST_CLAIM_EMOJI,
    GH_INROUND_WAIT,
    GH_SECONDARY_BASE,
    GH_TRANSIENT_BASE,
    GH_TRANSIENT_TRIES,
    OPEN_PR_MAX_PAGES,
    OPEN_PR_PAGE,
    TAUCETI,
)
from .review_diagnostics import public_diagnostic_quality


@functools.lru_cache(maxsize=1)
def me() -> str:
    """The GitHub login the worker is authenticated as (gh). Its PRs are the ones the worker tends
    (fix / fix-ci / rebase). Never hardcoded: whoever set up `gh auth` is who the worker acts as."""
    r = gh_run(["gh", "api", "user", "--jq", ".login"])  # waits out a rate limit rather than failing setup
    login = (r.stdout or "").strip()
    if not login:
        raise Die("could not determine the authenticated GitHub account (run `gh auth login`)")
    return login


def can_push(repo: str) -> bool | None:
    """Does the authenticated account have push (write) access to `repo`? `true`/`false` from GitHub's
    own `permissions.push`, or None when we can't tell (network/rate-limit/parse)."""
    r = gh_run(["gh", "api", f"repos/{repo}", "--jq", ".permissions.push"])
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return True if out == "true" else False if out == "false" else None


def claims_repo() -> str:
    """Where this worker publishes its cooperative claim leases (`refs/tauceti-claims/<key>`).

    `$CLAIM_REPO` overrides everything, verbatim: that is how a fleet pins one namespace of its own
    (`CLAIM_REPO=<you>/TauCeti` in every container) without asking anyone for access. It is read on
    every call rather than cached, so a worker can be repointed without a restart. Everything else is
    resolved once per process (two API calls at most) by `_resolve_claims_repo`."""
    return os.environ.get("CLAIM_REPO", "").strip() or _resolve_claims_repo()


@functools.lru_cache(maxsize=1)
def _resolve_claims_repo() -> str:
    """The claim namespace this account can actually push to: CLAIMS if it has been granted, else the
    contributor's own fork.

    Canonical is deliberately not a candidate. Nobody outside the org can push there, and a claim repo
    you cannot push to is worse than no claim at all: every `acquire` errors, every worker proceeds
    unclaimed, and a fleet of four spends four subscriptions on one report. The fork always works, so
    a brand-new operator de-duplicates within their own fleet on day one; CLAIMS then widens that to
    every operator, and is granted automatically on their first merged PR.

    `can_push` returning None (network, rate limit, or a private CLAIMS we cannot see) picks the fork:
    guessing "shared" and being wrong costs a failed claim on every task for the rest of the round,
    while the fork is right whenever we can tell at all."""
    if shared_claims_granted():
        log(f"claims: {CLAIMS} (shared namespace — de-duplicating against every operator)")
        return CLAIMS
    try:
        fork = ensure_fork()
    except Die as e:
        # Claims are [COOP]: never fail a round over one. Naming a repo we cannot push to leaves
        # `acquire` erroring and every task proceeding unclaimed, which is exactly the old behaviour.
        log(f"claims: no writable claim namespace ({e}) — rounds will proceed unclaimed")
        return CLAIMS
    log(
        f"claims: {fork} (your fork — de-duplicating within your own fleet; the shared namespace "
        f"{CLAIMS} opens on your first merged {TAUCETI} PR)"
    )
    return fork


def shared_claims_granted() -> bool:
    """Can this account push to the shared claim namespace? Accepts a pending invitation first, so an
    operator whose grant landed between rounds does not have to do anything by hand."""
    if can_push(CLAIMS) is True:
        return True
    return accept_claims_invitation() and can_push(CLAIMS) is True


def accept_claims_invitation() -> bool:
    """Accept a pending collaborator invitation to CLAIMS, and to nothing else. True if one was accepted.

    Access to the shared namespace is granted automatically, but GitHub grants it as an *invitation*:
    left unaccepted it sits in the operator's email while their workers keep colliding, and repository
    invitations expire after seven days. So the worker accepts its own. The `full_name` match is exact
    and no other invitation is ever touched — this must not become an "accept whatever GitHub offers"
    button. Best-effort: any failure just means we fall through to the fork."""
    jq = f'[.[] | select(.repository.full_name == "{CLAIMS}") | .id] | first // empty'
    p = gh_run(["gh", "api", "/user/repository_invitations", "--jq", jq])
    invitation = (p.stdout or "").strip()
    if p.returncode != 0 or not invitation:
        return False
    accepted = gh_run(["gh", "api", "-X", "PATCH", f"/user/repository_invitations/{invitation}"])
    if accepted.returncode != 0:
        log(f"claims: could not accept the invitation to {CLAIMS} ({(accepted.stderr or '').strip()})")
        return False
    log(f"claims: accepted the collaborator invitation to {CLAIMS}")
    return True


def _find_fork() -> str | None:
    """The authed user's fork of TAUCETI, resolved by PARENT (not by name, so a same-named non-fork is
    never mistaken for it): the first owned fork whose parent is TAUCETI, as `owner/repo`, or None."""
    r = gh_run(["gh", "repo", "list", "--fork", "--limit", "200", "--json", "nameWithOwner,parent"])
    if r.returncode != 0:
        return None
    try:
        for repo in json.loads(r.stdout or "[]"):
            parent = repo.get("parent") or {}
            owner = (parent.get("owner") or {}).get("login") or ""
            full = f"{owner}/{parent.get('name') or ''}"
            if full.lower() == TAUCETI.lower():
                return repo.get("nameWithOwner")
    except (ValueError, json.JSONDecodeError):
        return None
    return None


@functools.lru_cache(maxsize=1)
def ensure_fork() -> str:
    """The contributor's own fork of TAUCETI (`owner/repo`), creating it if absent. The worker pushes
    authored branches here and opens PRs from it, so it never needs write access to canonical. Fails
    closed if the resolved fork can't be pushed to (e.g. a token scoped only to the base repo)."""
    fork = _resolve_fork()
    if can_push(fork) is False:  # explicit denial only; None (couldn't tell) fails open
        raise Die(
            f"resolved your fork {fork}, but this `gh` account cannot push to it. Use a `gh auth` that can "
            f"push to your fork (a token scoped only to {TAUCETI} is not enough), or set TAUCETI_FORK."
        )
    return fork


def _resolve_fork() -> str:
    """Locate (or create) the fork. `$TAUCETI_FORK=<owner/repo>` overrides (escape hatch; also for a fork
    under a non-default name/org). Otherwise resolve the existing fork by parent; if none, `gh repo fork`
    and poll until GitHub surfaces it (fork creation is async; a concurrent same-account worker may win
    the create — the re-query then finds it). Fails closed if a non-fork repo squats the fork's name."""
    override = os.environ.get("TAUCETI_FORK", "").strip()
    if override:
        return override
    found = _find_fork()
    if found:
        return found
    gh_run(["gh", "repo", "fork", TAUCETI, "--clone=false"])
    name = TAUCETI.split("/", 1)[1]
    for attempt in range(8):
        found = _find_fork()
        if found:
            return found
        clash = gh_run(["gh", "api", f"repos/{me()}/{name}", "--jq", ".fork"])
        if clash.returncode == 0 and clash.stdout.strip() == "false":
            raise Die(
                f"you already own {me()}/{name}, which is NOT a fork of {TAUCETI} — rename it, or set "
                f"TAUCETI_FORK=<owner/repo> to your fork of {TAUCETI}."
            )
        time.sleep(2 * (attempt + 1))
    raise Die(f"could not create or find your fork of {TAUCETI} via `gh repo fork` (check `gh auth status`).")


# ============================================================================
# run() — subprocess helpers. close_fds=True (Python default) means children never
# inherit the round.lock fd (the old shell worker needed a hand-managed `9>&-` for this).
# ============================================================================


class GitHubError(Exception):
    """A `gh` call failed (distinct from 'ran fine, returned no rows')."""


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    capture: bool = True,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
        input=input_text,
        check=check,
    )


def _parse_iso8601(s: str | None) -> int | None:
    """ISO 8601 (e.g. GitHub's '2026-06-19T08:03:48Z') → epoch seconds, or None on a bad value."""
    if not s:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


# ============================================================================
# GitHub REST rate limits. A 403 mid-round wastes the agent's work, so we wait the limit out and retry
# rather than failing the round (see GH_* constants). gh prints the limit kind on stdout/stderr.
# Two kinds, handled differently because of the ROUND_TIMEOUT hard cap on a child round:
#  - SECONDARY ("abuse") limits clear after a short, unspecified cooldown → wait IN PLACE and retry,
#    bounded by GH_INROUND_WAIT so the wait can't blow the round timeout.
#  - PRIMARY limits clear only at the hourly bucket reset → too long to wait under the round cap, so
#    surface immediately and let the loop preflight (cmd_loop, no hard cap) wait the reset out before
#    relaunching. (GitHub buckets core and graphql resets independently; the preflight watches both.)
# ============================================================================


def _gh_rate_kind(text: str) -> str | None:
    """Classify a failed `gh` call from its combined stdout+stderr: 'secondary' | 'primary' | None.
    Secondary first — its message also contains 'rate limit', so the primary regex would match it too."""
    if _GH_SECONDARY_RE.search(text):
        return "secondary"
    if _GH_PRIMARY_RE.search(text):
        return "primary"
    return None


_GH_READ_VERBS = frozenset({"list", "view", "status", "checks", "diff", "search"})


def _gh_transient(text: str) -> bool:
    """Whether a failed `gh` call reads as the transport or the server giving out, rather than as an
    answer about our request (see _GH_TRANSIENT_RE)."""
    return bool(_GH_TRANSIENT_RE.search(text))


def _gh_read_only(argv: list[str]) -> bool:
    """Whether this invocation only READS.

    A transient failure is ambiguous in a way a rate limit is not: a rejected request certainly did not
    run, but a 504 may mean GitHub applied the change and lost the response on the way back. Retrying a
    read costs a duplicate query; retrying `issue create` or `api -X PATCH` costs a duplicate issue or a
    change applied twice. So only reads are retried, decided from the command line rather than from a
    flag each caller could forget to pass."""
    args = [a for a in argv[1:] if a]  # drop the `gh` program name
    if not args:
        return False
    if args[0] == "api":
        rest = args[1:]
        if rest[:1] == ["graphql"]:
            # Every GraphQL call is a POST, so the method says nothing about what it does; the document
            # does. A `query` reads and may be retried, a `mutation` writes and may not. An invocation
            # whose document we cannot see (--input, a file) is treated as a write.
            docs = [a[len("query=") :] for a in rest[1:] if a.startswith("query=")]
            return bool(docs) and not any(_GQL_MUTATION_RE.search(d) for d in docs)
        # `gh api` is GET unless told otherwise, and a field/body argument makes it a POST implicitly.
        for i, a in enumerate(rest):
            if a in ("-X", "--method"):
                if i + 1 >= len(rest) or rest[i + 1].upper() != "GET":
                    return False
            elif a.startswith("--method="):
                if a.split("=", 1)[1].upper() != "GET":
                    return False
            elif a in ("-f", "-F", "--field", "--raw-field", "--input"):
                return False
        return True
    return len(args) > 1 and args[1] in _GH_READ_VERBS


def github_budget() -> dict | None:
    """Per-bucket (remaining, reset_epoch) from GitHub's rate_limit endpoint, keyed 'core' and 'graphql'
    — the two buckets a round spends (REST and the progress-guard GraphQL query). That endpoint is itself
    exempt from the budget, so probing it is free. None on any read failure (caller proceeds rather than
    block on a flaky probe)."""
    p = run(
        [
            "gh",
            "api",
            "rate_limit",
            "--jq",
            "{core:[.resources.core.remaining,.resources.core.reset],"
            "graphql:[.resources.graphql.remaining,.resources.graphql.reset]}",
        ]
    )
    if p.returncode != 0:
        return None
    try:
        d = json.loads(p.stdout)
        return {k: (int(v[0]), int(v[1])) for k, v in d.items()}
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def _gh_secondary_wait(text: str, attempt: int) -> int:
    """Seconds to wait before retrying a SECONDARY-limited `gh` call: honor a Retry-After if gh echoed
    one, else exponential from GH_SECONDARY_BASE. >= 1s, clamped to the in-round budget."""
    ra = _parse_retry_after(_gh_retry_after(text))
    nap = int(ra) if ra is not None else GH_SECONDARY_BASE * (1 << min(attempt, 4))
    return max(1, min(nap, GH_INROUND_WAIT))


def _gh_retry_after(text: str) -> str | None:
    """gh occasionally echoes a `Retry-After: N` line for a secondary limit. Pull the value if present."""
    m = re.search(r"retry[- ]after:?\s*(\d+)", text, re.I)
    return m.group(1) if m else None


def gh_run(argv: list[str], *, cwd: Path | None = None, max_wait: int = GH_INROUND_WAIT) -> subprocess.CompletedProcess:
    """Run a `gh` command, waiting out a SECONDARY GitHub rate limit IN PLACE and retrying so the limit
    costs a pause, not a discarded round (bounded by max_wait so it can't blow ROUND_TIMEOUT). A PRIMARY
    (hourly) limit is surfaced immediately — waiting an hour inside a round under the 90-min cap would
    just be SIGKILLed; the loop preflight waits that reset out instead.

    A TRANSIENT failure of a READ (a 5xx, a truncated body, a dropped connection) is retried a few
    seconds later, up to GH_TRANSIENT_TRIES times. Any other failure is returned unchanged for the
    caller to handle as before."""
    waited = 0
    attempt = 0
    tries = 0
    while True:
        p = run(argv, cwd=cwd)
        if p.returncode == 0:
            return p
        text = (p.stderr or "") + "\n" + (p.stdout or "")
        kind = _gh_rate_kind(text)
        if kind is None:
            if not (_gh_transient(text) and _gh_read_only(argv)) or tries >= GH_TRANSIENT_TRIES:
                return p
            nap = GH_TRANSIENT_BASE << tries
            if waited + nap > max_wait:
                return p
            log(f"gh: {one_line(text, 120)} — transient, retrying in {nap}s ({' '.join(argv[1:3])})")
            time.sleep(nap)
            waited += nap
            tries += 1
            continue
        if kind == "primary":
            log(
                "gh: primary rate limit — surfacing so the round backs off and the loop preflight "
                "waits out the hourly reset (waiting in-round would exceed the round timeout)"
            )
            return p
        nap = _gh_secondary_wait(text, attempt)
        if waited + nap > max_wait:
            log(
                f"gh: secondary rate limit, but the in-round wait budget is spent ({waited}s) — "
                f"surfacing the error so the round backs off"
            )
            return p
        log(f"gh: secondary rate limit — waiting {nap}s for it to clear, then retrying ({' '.join(argv[1:3])})")
        time.sleep(nap)
        waited += nap
        attempt += 1


# ============================================================================
# GitHub — every gh wrapper. JSON via `gh ... --json` + stdlib json (no jq).
# ============================================================================


# The survey's open-PR page. Only the fields PRInfo reads, and for the head's status ONLY the commit
# status contexts — never a check run. That distinction is load-bearing and predates this query: a
# check-run reflects a job's outcome and can go red on an infra hiccup while the authoritative `build`
# status is green (see PRInfo.from_json). `statusCheckRollup` mixes both and costs an order of
# magnitude more; `commits(last:1)` is the head, whose status is the one that gates the PR.
_OPEN_PRS_QUERY = """query($owner:String!,$repo:String!,$n:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequests(states:OPEN,first:$n,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title body isDraft mergeable updatedAt headRefOid headRefName
        headRepositoryOwner{login} headRepository{name}
        author{login __typename}
        labels(first:50){totalCount nodes{name}}
        commits(last:1){nodes{commit{status{contexts{context state createdAt}}}}}
      }
    }
  }
}"""


def _pr_json_from_graphql(node: dict) -> dict:
    """One GraphQL PR node in `gh pr list --json` shape, so PRInfo.from_json stays the single place
    that decides what a PR's fields MEAN.

    Only the spellings gh normalizes need translating: it flattens `labels`/`contexts` out of their
    connections, renames a status context's `createdAt` to `startedAt`, and reports a Bot author as
    `is_bot` with an `app/`-prefixed login rather than as a bare `__typename`. Nothing downstream reads
    the prefix, but matching gh exactly is what lets the two paths be compared field for field."""
    commits = (node.get("commits") or {}).get("nodes") or [{}]
    status = ((commits[0] or {}).get("commit") or {}).get("status") or {}
    author = node.get("author") or {}
    is_bot = author.get("__typename") == "Bot"
    login = author.get("login", "")
    return {
        **{
            k: node.get(k)
            for k in ("number", "title", "body", "isDraft", "mergeable", "updatedAt", "headRefOid", "headRefName")
        },
        "headRepositoryOwner": node.get("headRepositoryOwner") or {},
        "headRepository": node.get("headRepository") or {},
        "author": {"login": f"app/{login}" if is_bot and login else login, "is_bot": is_bot},
        "labels": list((node.get("labels") or {}).get("nodes") or []),
        "statusCheckRollup": [
            {"context": c.get("context"), "state": c.get("state"), "startedAt": c.get("createdAt")}
            for c in (status.get("contexts") or [])
        ],
    }


class GitHub:
    def __init__(self, repo: str = TAUCETI):
        self.repo = repo

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess:
        return gh_run(["gh", *args])

    def open_prs(self, *, page: int = OPEN_PR_PAGE) -> list[dict]:
        """Every open PR, paged, in the same shape `gh pr list --json` returns (so PRInfo.from_json
        reads either).

        This is the query that opens every round, and `gh pr list` is the wrong shape for it at any
        size. It asks for `statusCheckRollup` — every check run and status context on every PR's head —
        when the survey reads exactly one thing out of that rollup: the `build` commit status. The
        rollup made one request whose server-side cost grew with the project, until at ~100 open PRs it
        ran ~10s against a gateway that gives up around 11 and failed about a quarter of the time. Its
        `--limit` was the other half of the problem: a limit is a silent truncation, so past it the
        survey would have started quietly missing PRs, which is worse than failing.

        Asking for the status contexts alone puts a page of 100 at ~3s, and paging puts a ceiling on
        what any ONE request costs, independent of how many PRs are open. The work still grows with the
        project, but it grows as more small requests rather than as one request drifting towards a
        timeout, and each page is retried on its own (see gh_run)."""
        owner, _, name = self.repo.partition("/")
        out: list[dict] = []
        seen: set[int] = set()
        truncated: list[int] = []
        cursor = None
        for _ in range(OPEN_PR_MAX_PAGES):
            args = ["api", "graphql", "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"n={page}"]
            if cursor:
                args += ["-F", f"cursor={cursor}"]
            p = self._gh([*args, "-f", f"query={_OPEN_PRS_QUERY}"])
            if p.returncode != 0:
                raise GitHubError(f"open PR query failed: {one_line(p.stderr or p.stdout, 160)}")
            try:
                conn = json.loads(p.stdout)["data"]["repository"]["pullRequests"]
            except (ValueError, TypeError, KeyError) as e:
                raise GitHubError(f"open PR query returned no pull requests ({e})") from e
            for node in conn.get("nodes") or []:
                # Same principle as the page cap, one level down: a PR carrying more labels than we
                # asked for would come back quietly short, and the status pipeline reads labels.
                labels = node.get("labels") or {}
                if labels.get("totalCount", 0) > len(labels.get("nodes") or []):
                    truncated.append(node.get("number"))
                # Ordered by creation, which never changes, so a PR updated mid-scan cannot reorder
                # itself across a page boundary. Dedupe anyway: a skipped PR is invisible work, and
                # this is the one place that would hide it.
                if node and node.get("number") not in seen:
                    seen.add(node["number"])
                    out.append(_pr_json_from_graphql(node))
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                if truncated:
                    log(f"open PR query: {len(truncated)} PR(s) carry more than 50 labels, e.g. #{truncated[0]}")
                return out
            cursor = info.get("endCursor")
        # Refusing here is the point: the alternative is returning a truncated list that reads as the
        # whole project, which is exactly the failure `--limit` used to hide.
        raise GitHubError(f"open PR query exceeded {OPEN_PR_MAX_PAGES} pages of {page} ({len(out)} PRs so far)")

    def pr_list(self, fields: list[str], *, author: str | None = None, state: str = "open") -> list[dict]:
        args = ["pr", "list", "--repo", self.repo, "--state", state, "--limit", "200", "--json", ",".join(fields)]
        if author:
            args += ["--author", author]
        p = self._gh(args)
        if p.returncode != 0:
            raise GitHubError(f"gh pr list failed: {p.stderr.strip()}")
        return json.loads(p.stdout or "[]")

    def issue_list(
        self, repo: str, *, labels: list[str] | None = None, fields: list[str], state: str = "open", limit: int = 200
    ) -> list[dict]:
        """List issues in `repo` (explicit, since the client is bound to its own repo), filtered by
        ALL of `labels` (repeated --label = AND; gh handles slashes in label names). Each dict has
        the requested `fields`. Raises GitHubError on failure."""
        args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit), "--json", ",".join(fields)]
        for label in labels or []:
            args += ["--label", label]
        p = self._gh(args)
        if p.returncode != 0:
            raise GitHubError(f"gh issue list failed: {p.stderr.strip()}")
        return json.loads(p.stdout or "[]")

    def pr_view(self, pr: int, fields: list[str]) -> dict | None:
        p = self._gh(["pr", "view", str(pr), "--repo", self.repo, "--json", ",".join(fields)])
        if p.returncode != 0:
            return None
        return json.loads(p.stdout or "{}")

    @staticmethod
    def _stuck_issue_body(pr: int, reason: str, diagnostic: str = "") -> str:
        detail = (
            f"\n\nLatest allow-listed worker diagnostics:\n\n{diagnostic}"
            if diagnostic
            else "\n\nNo public worker diagnostic was retained."
        )
        return (
            f"The autonomous worker cannot make progress on #{pr}: {reason}\n"
            f"{detail}\n\n"
            f"It can be neither merged (not all-green) nor auto-retired (its review rounds "
            f"are not advancing). This issue calls for an infrastructure repair, not a one-off "
            f"manual review. The worker re-checks each round and will close this issue's PR-side "
            f"concern once #{pr} merges or is closed.\n\n"
            f"<!--tauceti-review-stuck:{pr}-->"
        )

    def ensure_stuck_issue(self, pr: int, reason: str, diagnostic: str = "") -> None:
        """Ensure a tracking issue exists for a PR the automation can't make progress on (a permanent
        record so a human notices). An existing issue is updated when a later worker has a better
        diagnostic. Deduped by an exact title: one open issue per stuck PR across the whole fleet.
        Best-effort — a GitHub failure is non-fatal (the per-round warning still fires); never raises."""
        title = f"Review stuck: PR #{pr}"
        try:
            p = self._gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--state",
                    "open",
                    "--search",
                    f'in:title "{title}"',
                    "--json",
                    "number,title,body",
                ]
            )
            body = self._stuck_issue_body(pr, reason, diagnostic)
            matches = []
            if p.returncode == 0:
                matches = [
                    issue
                    for issue in json.loads(p.stdout or "[]")
                    if isinstance(issue, dict) and issue.get("title") == title
                ]
            if matches:
                issue = matches[0]
                existing_body = issue.get("body") if isinstance(issue.get("body"), str) else ""
                # Keep equally useful peer reports stable, but let an actual diagnosis
                # replace "review command failed". The comparison uses fixed public
                # categories only; raw subprocess output never crosses this boundary.
                has_public_diagnostic = "Latest allow-listed worker diagnostics:" in existing_body
                better = public_diagnostic_quality(body) > public_diagnostic_quality(existing_body)
                if existing_body != body and (not has_public_diagnostic or better):
                    self._gh(
                        [
                            "issue",
                            "edit",
                            str(issue["number"]),
                            "--repo",
                            self.repo,
                            "--body",
                            body,
                        ]
                    )
                return
            self._gh(["issue", "create", "--repo", self.repo, "--title", title, "--body", body])
        except Exception:
            pass

    def rebase_requested(self, pr: int, head: str) -> bool:
        """A trusted sweep handoff for this exact head; stale requests spend no attempts."""
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            return False
        p = self._gh(
            [
                "api",
                "--paginate",
                f"/repos/{self.repo}/issues/{pr}/comments?per_page=100",
                "--jq",
                ".[] | {body, author: .user.login}",
            ]
        )
        if p.returncode != 0:
            return False
        try:
            comments = [json.loads(line) for line in (p.stdout or "").splitlines() if line.strip()]
        except (ValueError, TypeError):
            return False
        return any(
            isinstance(c, dict)
            and c.get("author") == "tauceti-review-bot[bot]"
            and isinstance(c.get("body"), str)
            and c["body"].startswith("Merge-queue recovery for head `")
            and f"<!--tauceti-rebase:v1 {head}-->" in (c.get("body") or "").splitlines()
            for c in comments
        )

    def issue_comments(self, pr: int) -> list[dict] | None:
        """All issue comments for a PR (paginated). None on fetch failure (distinct from empty)."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/issues/{pr}/comments?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def review_comments(self, pr: int) -> list[dict] | None:
        """All review (inline / thread) comments for a PR — distinct from issue_comments. A contested
        fix replies on a review thread, so the progress guard must count these too. None on failure."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/pulls/{pr}/comments?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def pr_progress_state(self, pr: int) -> dict | None:
        """{'head': <headRefOid>, 'ncomments': <issue + review-thread comments>} in ONE GraphQL request,
        replacing a `pr view` plus two paginated REST comment fetches. The progress guard runs this twice
        per guarded round per PR, so collapsing ~5 REST calls into 1 GraphQL call is the bulk of the
        round's GitHub spend. None on any failure (caller treats that as 'can't tell'). Review threads are
        fetched first:100; on the rare PR with more, we fall back to the exact paginated REST count so the
        guard never undercounts a comment that landed on a later thread."""
        owner, _, name = self.repo.partition("/")
        q = (
            "query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){"
            "pullRequest(number:$pr){headRefOid comments{totalCount}"
            "reviewThreads(first:100){totalCount nodes{comments{totalCount}}}}}}"
        )
        p = self._gh(
            ["api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr}"]
        )
        if p.returncode != 0:
            return None
        try:
            d = json.loads(p.stdout)["data"]["repository"]["pullRequest"]
            threads = d["reviewThreads"]
            if threads["totalCount"] > 100:  # beyond one page — get the exact count via REST
                return self._pr_progress_state_rest(pr, d["headRefOid"])
            nc = d["comments"]["totalCount"] + sum(t["comments"]["totalCount"] for t in threads["nodes"])
            return {"head": d["headRefOid"], "ncomments": nc}
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _pr_progress_state_rest(self, pr: int, head: str) -> dict | None:
        """Exact issue + review comment count via the paginated REST endpoints (the pre-GraphQL path),
        for the rare PR with >100 review threads. None if either fetch fails."""
        cs = self.issue_comments(pr)
        rcs = self.review_comments(pr)
        if cs is None or rcs is None:
            return None
        return {"head": head, "ncomments": len(cs) + len(rcs)}

    def api_jq(self, path: str, jq: str) -> str | None:
        p = self._gh(["api", path, "--jq", jq])
        if p.returncode != 0:
            return None
        return p.stdout.strip()

    def reactions(self, comment_id: int) -> list[dict] | None:
        """Reactions on a pull-request review (thread) comment. Each carries {content, created_at
        (whole-second ISO 8601), user, id}. None on fetch failure (distinct from no reactions)."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def fresh_claim_age(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> int | None:
        """Seconds since the newest `emoji` reaction on this comment, or None if there is none (or the
        fetch failed — fail OPEN: a transient API error must not let a stale claim block work forever,
        and the worst case of a missed claim is a rare double-review, which we've accepted)."""
        rs = self.reactions(comment_id)
        if not rs:
            return None
        newest = 0
        for r in rs:
            if r.get("content") != emoji:
                continue
            ts = _parse_iso8601(r.get("created_at"))
            if ts is not None and ts > newest:
                newest = ts
        if not newest:
            return None
        return max(0, int(time.time()) - newest)

    def add_reaction(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> bool:
        """Add `emoji` to a review comment (idempotent per (login, content)). False on failure — a
        claim we couldn't post just means a peer may double up, which is acceptable."""
        p = self._gh(
            ["api", "-X", "POST", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions", "-f", f"content={emoji}"]
        )
        return p.returncode == 0

    def remove_reaction(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> bool:
        """Remove our own `emoji` reaction from a review comment (releases the claim). Looks up the
        reaction id for THIS login, then deletes it; a no-op (True) if we hold none."""
        rs = self.reactions(comment_id)
        if rs is None:
            return False
        mine = me()
        rid = next(
            (r["id"] for r in rs if r.get("content") == emoji and (r.get("user") or {}).get("login") == mine), None
        )
        if rid is None:
            return True
        p = self._gh(["api", "-X", "DELETE", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions/{rid}"])
        return p.returncode == 0


def _parse_retry_after(raw: str | None) -> float | None:
    """A `Retry-After` header is either delta-seconds or an HTTP-date; return seconds-from-now, clamped to
    [0, 3600] (the server can send bogus/huge/negative values). Returns None when absent/unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    secs: float | None = None
    try:
        secs = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            secs = parsedate_to_datetime(raw).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    if secs is None:
        return None
    return max(0.0, min(3600.0, secs))

#!/usr/bin/env python3
"""gh_run waits out a GitHub rate limit and retries, rather than failing the round and discarding the
agent's (expensive) work.

A 403 mid-round used to fail the round outright — and, for a review, count against the review-ERROR cap
that escalates a healthy PR to a human (the #302 false-escalation in the field log). Now a rate-limited
`gh` call sleeps until the limit clears and retries in place, bounded by GH_INROUND_WAIT so it can't blow
ROUND_TIMEOUT; the loop-level preflight (cmd_loop) waits out the longer hourly primary reset. This harness
pins gh_run's decisions without touching the live API: a scripted FakeRun replays (returncode, stderr)
tuples and a stubbed sleep records the waits.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

PRIMARY = "HTTP 403: API rate limit exceeded for user ID 477956"
SECONDARY = "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
GATEWAY = "HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)"
TRUNCATED = "unexpected end of JSON input"
PR_LIST = ["gh", "pr", "list", "--repo", "owner/repo", "--json", "number,statusCheckRollup"]

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


class FakeRun:
    """Replays a scripted list of (returncode, stderr) for each tc.run() call; records argv seen."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc, err = self.script.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)


def with_stubs(run_script, budget=None):
    """Install a FakeRun and a sleep recorder; budget stubs github_budget() (so primary waits are
    deterministic without a real rate_limit probe). Returns (FakeRun, slept-list)."""
    fr = FakeRun(run_script)
    slept = []
    tc.github.run = fr
    tc.time.sleep = lambda s: slept.append(s)
    tc.github.github_budget = lambda: budget
    return fr, slept


def main() -> int:
    orig_run, orig_sleep, orig_budget = tc.github.run, tc.time.sleep, tc.github.github_budget

    # Classification: secondary first (its text also contains "rate limit").
    check("classify primary", tc._gh_rate_kind(PRIMARY), "primary")
    check("classify secondary", tc._gh_rate_kind(SECONDARY), "secondary")
    check("classify non-limit", tc._gh_rate_kind("HTTP 404: Not Found"), None)

    # Secondary limit clears on the second try: one wait of GH_SECONDARY_BASE, then success.
    fr, slept = with_stubs([(1, SECONDARY), (0, "")])
    p = tc.gh_run(["gh", "pr", "list"])
    check("secondary retries to success rc", p.returncode, 0)
    check("secondary waited once", slept, [tc.GH_SECONDARY_BASE])
    check("secondary made two gh calls", len(fr.calls), 2)

    # Secondary give-up: a wait past the in-round budget surfaces the error rather than overshooting it.
    fr, slept = with_stubs([(1, SECONDARY)])
    p = tc.gh_run(["gh", "pr", "list"], max_wait=1)
    check("secondary over-budget surfaces error", p.returncode, 1)
    check("secondary over-budget did not sleep", slept, [])

    # Non-rate-limit failure passes straight through, no retry, no sleep.
    fr, slept = with_stubs([(1, "HTTP 404: Not Found")])
    p = tc.gh_run(["gh", "pr", "view", "9"])
    check("non-limit returns the failure", p.returncode, 1)
    check("non-limit did not sleep", slept, [])
    check("non-limit made one call", len(fr.calls), 1)

    # Primary limit surfaces IMMEDIATELY (the loop preflight waits the hourly reset out; waiting in a
    # round under ROUND_TIMEOUT would just be SIGKILLed). No retry, no sleep.
    fr, slept = with_stubs([(1, PRIMARY)])
    p = tc.gh_run(["gh", "pr", "list"])
    check("primary surfaces the error", p.returncode, 1)
    check("primary did not sleep", slept, [])
    check("primary made one call", len(fr.calls), 1)

    # A transient failure of a READ is retried in place. The survey's opening `gh pr list` asks for
    # statusCheckRollup over every open PR, which GitHub's GraphQL gateway answers with a 504 often
    # enough to abort rounds that a retry seconds later would have completed.
    check("classify 504 as transient", tc._gh_transient(GATEWAY), True)
    check("classify a truncated body as transient", tc._gh_transient(TRUNCATED), True)
    check("a 404 is an answer, not a transient failure", tc._gh_transient("HTTP 404: Not Found"), False)
    check("a rate limit is not a transient failure", tc._gh_transient(SECONDARY), False)

    fr, slept = with_stubs([(1, GATEWAY), (0, "")])
    p = tc.gh_run(PR_LIST)
    check("504 on a read retries to success", p.returncode, 0)
    check("504 waited once, briefly", slept, [tc.GH_TRANSIENT_BASE])
    check("504 made two gh calls", len(fr.calls), 2)

    fr, slept = with_stubs([(1, TRUNCATED), (0, "")])
    check("a truncated body retries too", tc.gh_run(PR_LIST).returncode, 0)
    check("truncated body waited once", slept, [tc.GH_TRANSIENT_BASE])

    # Retries are bounded: the failure surfaces once the allowance is spent, and the round backs off.
    fr, slept = with_stubs([(1, GATEWAY)] * (tc.GH_TRANSIENT_TRIES + 1))
    p = tc.gh_run(PR_LIST)
    check("a persistent 504 surfaces the error", p.returncode, 1)
    check("a persistent 504 backs off geometrically", slept, [5, 10, 20])
    check("a persistent 504 stops at the try limit", len(fr.calls), tc.GH_TRANSIENT_TRIES + 1)

    # A WRITE is never retried on a transient failure: a 504 may mean GitHub applied the change and
    # lost the response, so a second attempt risks a duplicate issue, comment or reaction.
    for name, argv in (
        ("issue create", ["gh", "issue", "create", "--title", "t", "--body", "b"]),
        ("api -X DELETE", ["gh", "api", "-X", "DELETE", "/repos/o/r/pulls/comments/1/reactions/2"]),
        ("api --method=PATCH", ["gh", "api", "--method=PATCH", "/user/repository_invitations/3"]),
        ("api with a field", ["gh", "api", "/repos/o/r/issues", "-f", "title=t"]),
        ("repo fork", ["gh", "repo", "fork", "owner/repo", "--clone=false"]),
    ):
        fr, slept = with_stubs([(1, GATEWAY), (0, "")])
        p = tc.gh_run(argv)
        check(f"504 on `{name}` is not retried", (p.returncode, len(fr.calls), slept), (1, 1, []))

    # GraphQL is always a POST, so only the document says whether a call reads. The survey's paged PR
    # query depends on this: without it, the one query that must survive a flaky gateway is the one
    # call that never gets retried.
    for name, argv, want in (
        ("a graphql query", ["gh", "api", "graphql", "-f", "query=query($n:Int!){viewer{login}}"], True),
        ("the bare {...} shorthand", ["gh", "api", "graphql", "-f", "query={viewer{login}}"], True),
        ("a commented query", ["gh", "api", "graphql", "-f", "query=# open PRs\nquery{viewer{login}}"], True),
        ("a graphql mutation", ["gh", "api", "graphql", "-f", "query=mutation{addComment(input:{}){id}}"], False),
        ("a document we cannot see", ["gh", "api", "graphql", "--input", "doc.json"], False),
    ):
        check(f"{name} is read-only", tc._gh_read_only(argv), want)

    fr, slept = with_stubs([(1, GATEWAY), (0, "")])
    p = tc.gh_run(["gh", "api", "graphql", "-f", "query=query{viewer{login}}"])
    check("504 on a graphql query retries", (p.returncode, len(fr.calls)), (0, 2))

    fr, slept = with_stubs([(1, GATEWAY), (0, "")])
    p = tc.gh_run(["gh", "api", "graphql", "-f", "query=mutation{addComment(input:{}){id}}"])
    check("504 on a graphql mutation is not retried", (p.returncode, len(fr.calls)), (1, 1))

    # The wait budget bounds transient retries as it bounds rate-limit ones.
    fr, slept = with_stubs([(1, GATEWAY), (0, "")])
    p = tc.gh_run(PR_LIST, max_wait=1)
    check("504 over the wait budget surfaces the error", (p.returncode, slept), (1, []))

    # github_budget parses the rate_limit JSON object into per-bucket tuples (restore the real fn).
    tc.github.github_budget = orig_budget

    class BudgetRun:
        def __call__(self, argv, **kw):
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"core":[4321,1750000000],"graphql":[4999,1750000100]}', stderr=""
            )

    tc.github.run = BudgetRun()
    check(
        "github_budget parses both buckets",
        tc.github_budget(),
        {"core": (4321, 1750000000), "graphql": (4999, 1750000100)},
    )

    tc.github.run, tc.time.sleep, tc.github.github_budget = orig_run, orig_sleep, orig_budget

    # pr_progress_state: head + (issue + review-thread) comment count from one GraphQL payload.
    class GHGraphql(tc.GitHub):
        def __init__(self, payload, rest_counts=None):
            super().__init__("owner/repo")
            self._payload, self._rest = payload, rest_counts

        def _gh(self, args):
            return subprocess.CompletedProcess(args, 0, stdout=__import__("json").dumps(self._payload), stderr="")

        def issue_comments(self, pr):
            return [{}] * self._rest[0] if self._rest else None

        def review_comments(self, pr):
            return [{}] * self._rest[1] if self._rest else None

    def payload(head, issue_n, thread_counts, thread_total=None):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": head,
                        "comments": {"totalCount": issue_n},
                        "reviewThreads": {
                            "totalCount": thread_total if thread_total is not None else len(thread_counts),
                            "nodes": [{"comments": {"totalCount": c}} for c in thread_counts],
                        },
                    }
                }
            }
        }

    gh = GHGraphql(payload("abc123", 3, [2, 1]))
    check(
        "pr_progress_state counts head+issue+thread comments",
        gh.pr_progress_state(7),
        {"head": "abc123", "ncomments": 6},
    )  # 3 + 2 + 1

    # >100 threads: fall back to the exact paginated REST count (here 12 issue + 9 review = 21).
    gh = GHGraphql(payload("def456", 99, [], thread_total=101), rest_counts=(12, 9))
    check(
        "pr_progress_state falls back to REST past 100 threads",
        gh.pr_progress_state(8),
        {"head": "def456", "ncomments": 21},
    )
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

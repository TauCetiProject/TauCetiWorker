#!/usr/bin/env python3
"""The open-PR survey pages, and every page is one bounded, retryable request.

`gh pr list --limit 200 --json ...,statusCheckRollup` did not scale in two separate ways: the request
grew with the project until GitHub's GraphQL gateway timed it out (~10s of work against ~11s of
patience at 100 open PRs), and `--limit` meant that past 200 PRs the survey would have started
silently dropping the rest. GitHub.open_prs pages a query that asks only for what PRInfo reads, so one
request costs the same at 100 open PRs as at 10,000, and a missing page is an error rather than a
short list. This harness pins the paging, the shape it hands PRInfo, and the refusal — no network.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


def node(number, *, build=None, author="kim", bot=False, labels=("awaiting-review",), draft=False):
    """One PR as the GraphQL query returns it. `build` is (state, createdAt) for the head's `build`
    commit status, or None for a head that has posted no status at all."""
    contexts = [] if build is None else [{"context": "build", "state": build[0], "createdAt": build[1]}]
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "isDraft": draft,
        "mergeable": "MERGEABLE",
        "headRefOid": f"head{number}",
        "headRefName": f"branch-{number}",
        "headRepositoryOwner": {"login": "kim"},
        "headRepository": {"name": "TauCeti"},
        "updatedAt": "2026-09-17T01:00:00Z",
        "author": {"login": author, "__typename": "Bot" if bot else "User"},
        "labels": {"nodes": [{"name": n} for n in labels]},
        "commits": {"nodes": [{"commit": {"status": {"contexts": contexts} if contexts else None}}]},
    }


def page(nodes, *, more=False, cursor="CUR"):
    body = {
        "data": {
            "repository": {"pullRequests": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}
        }
    }
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(body), stderr="")


class FakeGH(tc.GitHub):
    """A GitHub client whose `gh` calls replay scripted pages and record the argv they were asked for."""

    def __init__(self, pages):
        super().__init__("TauCetiProject/TauCeti")
        self.pages = list(pages)
        self.calls = []

    def _gh(self, args):
        self.calls.append(args)
        return self.pages.pop(0) if self.pages else page([])


# --- the shape PRInfo receives ---------------------------------------------
gh = FakeGH([page([node(1, build=("SUCCESS", "2026-09-09T02:34:16Z"), labels=("awaiting-review", "roadmap/X"))])])
pr = tc.PRInfo.from_json(gh.open_prs()[0])
check("a green head reads as build_success", (pr.build_success, pr.build_failed), (True, False))
check("the build status timestamp survives", pr.build_status_at, 1788921256)
check("labels come out of their connection", pr.labels, ("awaiting-review", "roadmap/X"))
# The freshness key ReviewState reads is carried by this query and nothing else: if it stops arriving,
# every per-PR comment read silently falls back to the plain TTL and the survey goes linear again.
check("the PR's updatedAt survives into PRInfo", pr.updated_at, "2026-09-17T01:00:00Z")
check(
    "head fields carry through",
    (pr.head_oid, pr.head_ref, pr.head_owner, pr.head_repo),
    ("head1", "branch-1", "kim", "TauCeti"),
)

for state, want in (("FAILURE", (False, True)), ("ERROR", (False, True)), ("PENDING", (False, False))):
    gh = FakeGH([page([node(2, build=(state, "2026-09-09T02:34:16Z"))])])
    pr = tc.PRInfo.from_json(gh.open_prs()[0])
    check(f"a {state} build reads as (success, failed)", (pr.build_success, pr.build_failed), want)

# A head with no status yet is PENDING, never failed — it is waiting for the trusted build to post.
gh = FakeGH([page([node(3)])])
pr = tc.PRInfo.from_json(gh.open_prs()[0])
check(
    "no status yet is neither green nor red",
    (pr.build_success, pr.build_failed, pr.build_status_at),
    (False, False, None),
)

# gh spells a Bot author `app/<login>`; matching it keeps the two paths comparable field for field.
gh = FakeGH([page([node(4, author="tauceti-review-bot", bot=True)])])
pr = tc.PRInfo.from_json(gh.open_prs()[0])
check("a bot author keeps gh's spelling", (pr.author, pr.author_is_bot), ("app/tauceti-review-bot", True))

# --- paging ----------------------------------------------------------------
gh = FakeGH([page([node(1), node(2)], more=True, cursor="C1"), page([node(3)])])
check("every page's PRs are returned", [d["number"] for d in gh.open_prs()], [1, 2, 3])
check("two pages took two calls", len(gh.calls), 2)
check("the first page asks for no cursor", [a for a in gh.calls[0] if a.startswith("cursor=")], [])
check("the second page follows the first's cursor", [a for a in gh.calls[1] if a.startswith("cursor=")], ["cursor=C1"])

# A PR seen twice (a concurrent update reordering it across a boundary) is carried once, not twice.
gh = FakeGH([page([node(1), node(2)], more=True), page([node(2), node(3)])])
check("a PR repeated across pages appears once", [d["number"] for d in gh.open_prs()], [1, 2, 3])

# --- refusing to truncate ---------------------------------------------------
# The whole point of paging: a survey that cannot see every open PR must FAIL, because a short list
# reads exactly like a quiet project and would silently drop work.
gh = FakeGH([page([node(i)], more=True) for i in range(tc.OPEN_PR_MAX_PAGES + 5)])
try:
    gh.open_prs()
    check("running out of pages raises", False, True)
except tc.GitHubError as e:
    check("running out of pages raises rather than truncating", "exceeded" in str(e), True)
    check("...and stops at the page cap", len(gh.calls), tc.OPEN_PR_MAX_PAGES)

gh = FakeGH([subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 504: 504 Gateway Timeout\n")])
try:
    gh.open_prs()
    check("a failed page raises", False, True)
except tc.GitHubError as e:
    check("a failed page reports what gh said", "HTTP 504: 504 Gateway Timeout" in str(e), True)

gh = FakeGH([subprocess.CompletedProcess([], 0, stdout="{}", stderr="")])
try:
    gh.open_prs()
    check("an unreadable payload raises", False, True)
except tc.GitHubError as e:
    check("an unreadable payload is not an empty project", "no pull requests" in str(e), True)

# --- the page must be retryable --------------------------------------------
# gh_run only retries a transient failure of a READ, and every GraphQL call is a POST — so this query
# is retried only because it is classified by its document. If that ever stops holding, the survey
# silently loses its per-page safety net.
gh = FakeGH([page([node(1)])])
gh.open_prs()
check("the survey's own page is classified read-only", tc._gh_read_only(["gh", *gh.calls[0]]), True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

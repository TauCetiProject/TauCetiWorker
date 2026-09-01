#!/usr/bin/env python3
"""Regression guard for GitHub.pr_list's split-and-retry.

The survey asks for twelve fields across every open PR in one `gh pr list`. On a busy repo that
query exceeds GitHub's GraphQL execution budget and comes back 504 — measured on a 135-PR repo, the
full field set failed 3/3 while the descriptive half and the `statusCheckRollup` half each answered
at the same --limit. So pr_list fetches the costly fields separately and merges them on `number`.

Splitting by FIELD rather than by page is forced: `gh pr list` has --limit but no offset or cursor,
so there is no second page to ask for, and a smaller limit would silently truncate the survey.

Retry is not belt-and-braces either: the same repo gave 3/4, 1/4 and 4/4 successes at descending
limits, so the residual failure tracks GitHub's load rather than our query size. A survey failure
aborts the whole round before any work unit is chosen, so riding it out beats backing off.

The rules this locks in:
  - a request with no costly field is ONE call, unchanged (the `pr_list(["number"])` callers);
  - a mixed request is two calls, the costly one carrying only `number` + the costly fields;
  - the descriptive half defines the roster, and a PR missing from the costly half gets None —
    which PRInfo.from_json reads as "no build status yet", i.e. pending, never a false verdict;
  - `number` is added when the caller did not ask for it, and stripped back out of the result;
  - a transient GitHub failure (5xx, the prose timeout body, a truncated page) is retried, and a
    real error (404) is NOT — it raises on the first attempt.

Exit 0 = every rule holds; 1 = a regression.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github as gh_mod
from tauceti_worker.github import GitHub, GitHubError

gh_mod.time.sleep = lambda _s: None  # the retry pauses are not what we are testing

FIELDS = ["number", "title", "statusCheckRollup"]
failures: list[str] = []


def done(rc, out="", err=""):
    return subprocess.CompletedProcess(["gh"], rc, out, err)


class FakeGitHub(GitHub):
    """A GitHub whose every `gh` invocation is scripted, recording the --json it was asked for."""

    def __init__(self, *responses):
        self.repo = "owner/repo"
        self._queued = list(responses)
        self.asked: list[str] = []

    def _gh(self, args):
        self.asked.append(args[args.index("--json") + 1])
        return self._queued.pop(0)


def check(name, cond):
    if not cond:
        failures.append(name)


# A request with nothing costly stays a single call.
one = FakeGitHub(done(0, json.dumps([{"number": 7}])))
check("cheap-only stays one call", one.pr_list(["number"]) == [{"number": 7}] and len(one.asked) == 1)

# A mixed request splits, and the costly call carries only the join key plus the costly fields.
split = FakeGitHub(
    done(0, json.dumps([{"number": 1, "title": "a"}, {"number": 2, "title": "b"}])),
    done(
        0,
        json.dumps(
            [{"number": 1, "statusCheckRollup": [{"context": "build"}]}, {"number": 2, "statusCheckRollup": []}]
        ),
    ),
)
rows = split.pr_list(FIELDS)
check("mixed request splits in two", len(split.asked) == 2)
check("costly call is narrow", split.asked[1] == "number,statusCheckRollup")
check("descriptive fields survive", [r["title"] for r in rows] == ["a", "b"])
check("costly fields merge on number", rows[0]["statusCheckRollup"] == [{"context": "build"}])

# A PR that appears between the two calls is absent from the costly half: None, which reads as
# pending downstream rather than as a verdict.
gap = FakeGitHub(
    done(0, json.dumps([{"number": 1, "title": "a"}, {"number": 9, "title": "landed mid-survey"}])),
    done(0, json.dumps([{"number": 1, "statusCheckRollup": []}])),
)
straggler = gap.pr_list(FIELDS)[1]
check("merge gap yields None", straggler["statusCheckRollup"] is None)
check("merge gap reads as pending", (straggler.get("statusCheckRollup") or []) == [])

# `number` is the join key, so it is requested even when unwanted — and then removed.
joined = FakeGitHub(
    done(0, json.dumps([{"number": 1, "title": "a"}])),
    done(0, json.dumps([{"number": 1, "statusCheckRollup": []}])),
)
got = joined.pr_list(["title", "statusCheckRollup"])
check("join key requested anyway", joined.asked[0].startswith("number"))
check("join key stripped from result", "number" not in got[0] and got[0]["title"] == "a")

# Transient failures are retried: a 504, its prose body, and a truncated page that still exits 0.
for label, response in (
    ("504 status", done(1, err="HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)")),
    ("prose timeout", done(1, err="HTTP 504: We couldn't respond to your request in time.")),
    ("truncated page", done(0, '[{"num')),
):
    flaky = FakeGitHub(response, done(0, json.dumps([{"number": 3}])))
    check(f"retried after {label}", flaky.pr_list(["number"]) == [{"number": 3}] and len(flaky.asked) == 2)

# A real error is not a timeout: it raises on the first attempt rather than burning retries.
hard = FakeGitHub(done(1, err="HTTP 404: Not Found (https://api.github.com/graphql)"))
try:
    hard.pr_list(["number"])
    check("404 raises", False)
except GitHubError as exc:
    check("404 raises immediately", "404" in str(exc) and len(hard.asked) == 1)

# A persistent timeout gives up rather than retrying for ever.
stuck = FakeGitHub(*[done(1, err="HTTP 504: Gateway Timeout")] * 8)
try:
    stuck.pr_list(["number"])
    check("persistent timeout raises", False)
except GitHubError:
    check("persistent timeout is bounded", 1 < len(stuck.asked) <= 5)

if failures:
    print("github_pr_list_split: FAILED")
    for name in failures:
        print(f"  [FAIL] {name}")
    sys.exit(1)
print("github_pr_list_split: all cases passed")

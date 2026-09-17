#!/usr/bin/env python3
"""The one candidate a round spends on is re-read from GitHub first.

The survey triages on cached comment reads keyed to each PR's `updatedAt` (see
review_state_freshness), which is what stops a round paying per open PR. That key cannot see a
deleted comment, so the candidate the cascade actually picks is re-read live in dispatch() before
anything that costs money: a peer may have taken the head, the board may have been reviewed since,
the contested reply may be gone. A candidate that no longer stands is DECLINED, which is the
cascade's existing "offered but not taken" path — the round moves to the next candidate rather than
spending on a stale one.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

wu = tc.work_units
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


HEAD = "abc123"


class FakeRS:
    """The review state as it looks on a LIVE re-read, with a busted() flag so the test can prove the
    cache was dropped rather than consulted."""

    def __init__(self, *, meta=None, clean_head="", held=(), contest=None):
        self.meta = meta if meta is not None else tc.Meta({"head_sha": HEAD}, "fresh")
        self._clean_head, self._held, self._contest = clean_head, set(held), contest
        self.busted = []

    def bust(self, pr):
        self.busted.append(pr)

    def gh_meta(self, pr):
        return self.meta

    def ledger_clean_head(self, pr):
        return self._clean_head

    def ledger_blocking(self, pr, head):
        return True

    def inflight_review(self, pr, head):
        return set(self._held)

    def newest_contest_reply(self, pr):
        return self._contest


def worker(rs):
    return SimpleNamespace(rs=rs, counters=SimpleNamespace(read=lambda key: 0), cfg=SimpleNamespace(wid="test"))


def survey_with(pr=1, build_success=True):
    sv = tc.Survey(worker_id="test")
    sv.open_prs = [
        tc.PRInfo(
            number=pr,
            head_oid=HEAD,
            head_ref="b",
            head_owner="o",
            head_repo="r",
            is_draft=False,
            mergeable="MERGEABLE",
            author="me",
            build_success=build_success,
            build_failed=False,
        )
    ]
    return sv


def still(stage, rs, c=None, sv=None):
    return wu._still_actionable(stage, worker(rs), sv or survey_with(), c or tc.Candidate(1, HEAD, ""))


# --- review ---------------------------------------------------------------------------------------
rs = FakeRS()
check("a review candidate that still stands is taken", still("review", rs), True)
check("...after dropping its cached state", rs.busted, [1])

check("a head a peer now holds is declined", still("review", FakeRS(held=("codex",))), False)
check("a head reviewed since the survey is declined", still("review", FakeRS(clean_head=HEAD)), False)

contest_c = tc.Candidate(1, HEAD, "", contest="reuse", contest_reply_id=7)
check(
    "a contest whose reply is still there is taken",
    still("review", FakeRS(clean_head=HEAD, contest={"id": 7, "rubric": "reuse"}), contest_c),
    True,
)
check(
    "a contest whose reply vanished is declined",
    still("review", FakeRS(clean_head=HEAD, contest=None), contest_c),
    False,
)
check(
    "a contest answered by a NEWER reply than the one we picked is declined",
    still("review", FakeRS(clean_head=HEAD, contest={"id": 9, "rubric": "reuse"}), contest_c),
    False,
)

# --- fix ------------------------------------------------------------------------------------------
check("a fix candidate still blocking at head is taken", still("fix", FakeRS()), True)
check(
    "a fix candidate whose board moved off this head is declined",
    still("fix", FakeRS(meta=tc.Meta({"head_sha": "def456"}, "fresh"))),
    False,
)

# A contest that landed after the survey means this scoreboard is about to be re-adjudicated: sending a
# fixer at the identical finding would only burn the per-head budget.
check(
    "a fix candidate contested since the survey is declined",
    still(
        "fix",
        FakeRS(meta=tc.Meta({"head_sha": HEAD, "replies_through": 3}, "fresh"), contest={"id": 9, "rubric": "reuse"}),
    ),
    False,
)
check(
    "a fix candidate whose contest was already adjudicated is taken",
    still(
        "fix",
        FakeRS(meta=tc.Meta({"head_sha": HEAD, "replies_through": 9}, "fresh"), contest={"id": 9, "rubric": "reuse"}),
    ),
    True,
)


# --- a re-read we could not trust is never treated as confirmation --------------------------------
for provenance in ("stale", "fetch_failed"):
    for stage in ("review", "fix"):
        check(
            f"a {provenance} re-read declines the {stage} candidate",
            still(stage, FakeRS(meta=tc.Meta({"head_sha": HEAD}, provenance))),
            False,
        )

# --- stages that do not read review state are not charged for a re-read ---------------------------
for stage in ("rebase", "fix-ci", "bump", "roadmap", "progress"):
    rs = FakeRS()
    check(f"{stage} needs no re-read", (still(stage, rs), rs.busted), (True, []))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

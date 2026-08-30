#!/usr/bin/env python3
"""Reviewer first refusal and age-weighted review selection."""

import random
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

NOW = 2_000_000_000
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def candidate(pr, *, age=0, owner=""):
    ready_at = None if age is None else NOW - age
    return tc.Candidate(pr, f"head{pr}", ready_at=ready_at, preferred_reviewer=owner)


def ordered(candidates, reviewer="alice", seed=0):
    picked, deferred = tc.prioritize_review_candidates(candidates, reviewer, now=NOW, rng=random.Random(seed))
    return [c.pr for c in picked], [c.pr for c in deferred]


# The previous publisher gets a strict first tier during the grace period; peers defer it.
mine = candidate(1, age=10, owner="Alice")
shared = candidate(2, age=10)
foreign = candidate(3, age=10, owner="bob")
check("owner gets first refusal before shared work", ordered([shared, mine]), ([1, 2], []))
check("login matching is case-insensitive", ordered([mine], reviewer="ALICE"), ([1], []))
check("foreign affinity is deferred", ordered([shared, foreign]), ([2], [3]))

# At exactly twenty minutes the unit re-enters the shared pool. Missing metadata always fails open.
expired = candidate(4, age=tc.REVIEW_AFFINITY_GRACE_S, owner="bob")
unknown_time = candidate(5, age=None, owner="bob")
unknown_owner = candidate(6, age=10)
check("affinity expires at the boundary", ordered([expired]), ([4], []))
check("missing timestamp fails open", ordered([unknown_time]), ([5], []))
check("missing owner fails open", ordered([unknown_owner]), ([6], []))
check("missing current login disables affinity", ordered([foreign], reviewer=""), ([3], []))
check("scoreboard publisher is read", tc._scoreboard_reviewer(tc.Meta({"submitted_by": "alice"}, "fresh")), "alice")
check("malformed publisher fails open", tc._scoreboard_reviewer(tc.Meta({"submitted_by": 7}, "fresh")), "")


class ContestComments:
    def review_comments(self, _pr):
        return [
            {"id": 100, "in_reply_to_id": None, "body": "<!--tauceti-rubric:reuse-->"},
            {
                "id": 101,
                "in_reply_to_id": 100,
                "body": "I contest this",
                "created_at": "2033-05-18T03:33:20Z",
            },
        ]


state = tc.ReviewState(types.SimpleNamespace(sbcache=Path("/unused")), ContestComments())
check(
    "contest discovery retains its affinity timestamp",
    state.newest_contest_reply(1)["created_at"],
    "2033-05-18T03:33:20Z",
)

# Weighted permutation preserves the eligible set exactly and is deterministic with a seeded RNG.
candidates = [candidate(i, age=i * 600) for i in range(10, 20)]
first, deferred = ordered(candidates, seed=42)
second, _ = ordered(candidates, seed=42)
check("weighted order preserves candidate set", sorted(first), list(range(10, 20)))
check("weighted order is deterministic for a seed", first, second)
check("shared candidates are not deferred", deferred, [])

# Across fixed seeds, a day-old unit must be selected first far more often than a fresh one while the
# result remains a lottery rather than strict oldest-first ordering.
fresh = candidate(30, age=0)
old = candidate(31, age=tc.REVIEW_AGE_CAP_S)
wins = {30: 0, 31: 0}
for seed in range(1000):
    first, _ = ordered([fresh, old], seed=seed)
    wins[first[0]] += 1
check("old work wins a strong majority", wins[31] > 900, True)
check("fresh work still sometimes wins", wins[30] > 0, True)
check(
    "age weight caps after one day",
    tc._review_age_weight(candidate(40, age=10 * tc.REVIEW_AGE_CAP_S), NOW),
    tc._review_age_weight(old, NOW),
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

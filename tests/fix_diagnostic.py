#!/usr/bin/env python3
"""`fix_disposition` classifies a tended PR for the `fix` stage and phrases why it isn't actionable.

This is the signal behind Bryan's report: he opened PR #470, ran a one-shot `work --only fix` minutes
before the review bots posted the scoreboard, and got a bare "no eligible work this round under
--only=fix" with no hint that the PR was simply awaiting its first review. The survey now records a
one-line reason for every tended PR that isn't a fix candidate, and a fix-focused worker logs them.

`fix_disposition` is the pure decision: given the scoreboard meta, the current head, whether the build
is green, the authoritative `blocking` predicate (rs.ledger_blocking), and the per-head fix-attempt
count, it returns (disposition, reason). The blocking rule itself is NOT re-implemented here — it is
passed in — so this function and `ledger_blocking` can never drift. Dependency-free.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

HEAD = "a7952da7d6c21accf63db1163faa24a04c0c57e8"
OLD = "0000000000000000000000000000000000000000"
MAX = tc.MAX_FIX_ATTEMPTS

fails = 0


def meta(data, provenance="fresh"):
    return tc.Meta(data, provenance)


def disp(meta_obj, head=HEAD, build_success=True, blocking=False, per_head=0):
    return tc.fix_disposition(meta_obj, head, build_success, blocking, per_head)


def check(name, got, want_disp, want_substr=None):
    global fails
    d, reason = got
    ok = d == want_disp and (want_substr is None or want_substr in reason)
    fails += not ok
    extra = "" if want_substr is None else f" reason~={want_substr!r} got_reason={reason!r}"
    print(f"[{'OK ' if ok else 'XX '}] {name}: disp={d!r} want={want_disp!r}{extra}")


# --- actionable: a blocking rubric stands at the current head, under budget ---------------------
check(
    "blocking at head, under budget -> actionable",
    disp(meta({"head_sha": HEAD, "states": {"reuse": "blocking_request"}}), blocking=True, per_head=0),
    "actionable",
)
check(
    "blocking at head, one under budget -> actionable",
    disp(meta({"head_sha": HEAD}), blocking=True, per_head=MAX - 1),
    "actionable",
)
# Build red but a blocking review stands at head: still fix's to address (the cascade runs fix-ci first,
# so a red PR is greened before this candidate is ever dispatched — fix never gates on the build itself).
check(
    "blocking at head, build red -> still actionable",
    disp(meta({"head_sha": HEAD}), build_success=False, blocking=True, per_head=0),
    "actionable",
)
check(
    "blocking at head with a pending contest -> wait for adjudication",
    tc.fix_disposition(meta({"head_sha": HEAD}), HEAD, True, True, 1, pending_contest=True),
    "waiting",
    "contest awaiting re-review",
)

# --- exhausted: blocking, but the per-head fixer budget is spent ---------------------------------
check(
    "blocking at head, attempts spent -> exhausted",
    disp(meta({"head_sha": HEAD}), blocking=True, per_head=MAX),
    "exhausted",
    f"{MAX}/{MAX}",
)

# --- waiting: head matches but nothing blocks ----------------------------------------------------
check(
    "reviewed at head, all green -> waiting (nothing to fix)",
    disp(meta({"head_sha": HEAD, "states": {"reuse": "green"}}), blocking=False),
    "waiting",
    "all green",
)
# head matches a scoreboard that carries no verdicts at all (malformed/skeleton): ledger_blocking is
# also false, but "all green" would mislead — say the verdicts aren't in yet.
check(
    "reviewed at head, no verdicts -> waiting (awaiting review), not 'all green'",
    disp(meta({"head_sha": HEAD}), blocking=False),
    "waiting",
    "no rubric verdicts yet",
)

# --- waiting: build-green, no scoreboard at this head (Bryan's case) -----------------------------
check(
    "build-green, no scoreboard -> waiting (awaiting first review)",
    disp(meta({}, "missing"), blocking=False),
    "waiting",
    "awaiting first review",
)

# --- waiting: scoreboard exists at an older head (head moved since review) -----------------------
check(
    "scoreboard at old head -> waiting (awaiting re-review)",
    disp(meta({"head_sha": OLD}), blocking=False),
    "waiting",
    "awaiting re-review",
)

# --- waiting: a transient fetch failure must not masquerade as 'awaiting first review' -----------
check(
    "fetch failed -> waiting (will retry), not 'awaiting first review'",
    disp(meta({}, "fetch_failed"), blocking=False),
    "waiting",
    "fetch failed",
)
# A stale cache (fetch failed, serving an old head) must not assert "head moved" — head_sha is unreliable.
check(
    "stale cache at old head -> waiting (could not read), not 'head moved'",
    disp(meta({"head_sha": OLD}, "stale"), blocking=False),
    "waiting",
    "could not read current review state",
)

# --- skip: a red PR with no review at head is fix-ci/bump's job, not a fix-stage status line ------
check(
    "red build, no scoreboard -> skip (no status line)",
    disp(meta({}, "missing"), build_success=False, blocking=False),
    "skip",
)
check(
    "red build, scoreboard at old head -> skip",
    disp(meta({"head_sha": OLD}), build_success=False, blocking=False),
    "skip",
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

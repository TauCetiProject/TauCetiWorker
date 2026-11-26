#!/usr/bin/env python3
"""Regression guard for PRInfo.from_json's build signal.

`statusCheckRollup` mixes two node types: CheckRun (carries `name`/`conclusion`) and StatusContext
(carries `context`/`state`). The required `build` result the merge gate reads is a commit STATUS
(context=="build"), posted by the trusted sandboxed-build workflow. A check-run reflects a JOB's
outcome, which can go red on a transient INFRA / status-report hiccup while the authoritative status
is green (the false-red that once wedged a green PR into fix-ci), so it is NOT a build signal.

The rules this locks in:
  - the commit STATUS (context=="build") is the SOLE authority for the build signal;
  - a check-run named "build" is IGNORED entirely — regardless of the head owner (fork or same-repo)
    — so the signal never depends on a job's check-run again;
  - build_success needs a non-empty, unanimously-SUCCESS set of statuses; build_failed needs any
    failing state; the two are mutually exclusive and never invent a verdict from nothing.

Exit 0 = all cases classify correctly; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

SAME = tc.TAUCETI_OWNER  # head in the base repo
FORK = "some-fork-owner"  # head in a fork


def checkrun(conclusion):
    return {"__typename": "CheckRun", "name": "build", "conclusion": conclusion}


def status(state):
    return {"__typename": "StatusContext", "context": "build", "state": state}


def pr(rollup, owner):
    return tc.PRInfo.from_json({"number": 1, "headRepositoryOwner": {"login": owner}, "statusCheckRollup": rollup})


# (label, rollup, owner, want_success, want_failed)
CASES = [
    # Status-only, both polarities.
    ("status-only SUCCESS", [status("SUCCESS")], SAME, True, False),
    ("status-only FAILURE", [status("FAILURE")], SAME, False, True),
    # A pending / expected build status is neither success nor failure (the PR simply waits).
    ("status PENDING", [status("PENDING")], SAME, False, False),
    ("status EXPECTED", [status("EXPECTED")], SAME, False, False),
    # A non-"FAILURE" terminal build state still counts as failed (matches BUILD_FAIL).
    ("status ERROR is failed", [status("ERROR")], SAME, False, True),
    # Mixed statuses: failure wins, success needs unanimity.
    ("two statuses SUCCESS + FAILURE → failed", [status("SUCCESS"), status("FAILURE")], SAME, False, True),
    ("two statuses SUCCESS + PENDING → wait", [status("SUCCESS"), status("PENDING")], SAME, False, False),
    ("duplicate status SUCCESS → success", [status("SUCCESS"), status("SUCCESS")], SAME, True, False),
    # The status is authoritative and the check-run is ignored: a green status with a red `build`
    # check-run (the historical false-red) is a PASS. Order must not matter.
    ("status SUCCESS + checkrun FAILURE → success", [status("SUCCESS"), checkrun("FAILURE")], FORK, True, False),
    ("checkrun FAILURE + status SUCCESS → success", [checkrun("FAILURE"), status("SUCCESS")], FORK, True, False),
    ("status FAILURE + checkrun SUCCESS → failed", [status("FAILURE"), checkrun("SUCCESS")], SAME, False, True),
    # A present-but-pending status wins over any check-run: still just waiting.
    ("status PENDING + checkrun FAILURE → wait", [status("PENDING"), checkrun("FAILURE")], FORK, False, False),
    # No build STATUS at all → pending, no matter what the check-run says or who owns the head. These
    # are the cases the old check-run fallback used to (mis)read; they are now uniformly pending.
    ("checkrun-only SUCCESS → wait (ignored)", [checkrun("SUCCESS")], SAME, False, False),
    ("checkrun-only FAILURE → wait (ignored)", [checkrun("FAILURE")], SAME, False, False),
    ("checkruns SUCCESS + FAILURE → wait (ignored)", [checkrun("SUCCESS"), checkrun("FAILURE")], SAME, False, False),
    # No build signal at all → neither (don't invent a pass or a fail).
    ("no build entries", [{"__typename": "CheckRun", "name": "label", "conclusion": "SUCCESS"}], SAME, False, False),
    ("empty rollup", [], SAME, False, False),
]

# The check-run is ignored regardless of head owner: every rollup below must classify the SAME under a
# fork head and a same-repo head, proving the signal no longer depends on who owns the branch.
OWNER_INVARIANT = [
    [checkrun("SUCCESS")],
    [checkrun("FAILURE")],
    [status("SUCCESS"), checkrun("FAILURE")],
    [status("FAILURE")],
]


def main():
    fails = 0
    for label, rollup, owner, want_s, want_f in CASES:
        p = pr(rollup, owner)
        ok = p.build_success == want_s and p.build_failed == want_f
        flag = "OK " if ok else "XX "
        print(
            f"[{flag}] {label:48} success={p.build_success!s:5} failed={p.build_failed!s:5} "
            f"(want success={want_s!s:5} failed={want_f})"
        )
        if not ok:
            fails += 1
    # build_success and build_failed must never both be true.
    for label, rollup, owner, *_ in CASES:
        p = pr(rollup, owner)
        if p.build_success and p.build_failed:
            print(f"[XX ] {label}: build_success AND build_failed both true")
            fails += 1
    # Owner-independence: fork and same-repo heads must agree on every rollup.
    for rollup in OWNER_INVARIANT:
        f, s = pr(rollup, FORK), pr(rollup, SAME)
        ok = (f.build_success, f.build_failed) == (s.build_success, s.build_failed)
        print(f"[{'OK ' if ok else 'XX '}] owner-invariant {str(rollup):64} fork==same={ok}")
        if not ok:
            fails += 1
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} case mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

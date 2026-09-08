#!/usr/bin/env python3
"""A round aborted by a GitHub read must say WHICH failure aborted it.

The survey captures gh's own stderr when `gh pr list` fails, but run_round used to discard it and raise
a fixed "gh pr list failed (GitHub API?)". That question mark was the whole problem: the answer was an
HTTP 504 from the GraphQL gateway (the survey's statusCheckRollup query runs ~10s and the gateway gives
up around 11), and an operator reading the dashboard had no way to tell that from a broken credential.
Dependency-free: the survey is stubbed, so no network and no gh.

Exit 0 = all cases agree; 1 = a mismatch.
"""

import sys
import types
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


def abort_reason(errors):
    """run_round's message when the opening survey could not read GitHub."""
    sv = tc.Survey(worker_id="test", roadmap_only="auto", roadmap_skip=[])
    sv.github_failed = True
    sv.errors = list(errors)
    original = tc.work_units.survey
    tc.work_units.survey = lambda *a, **k: sv
    try:
        # dry_run keeps the round out of the credential mirror; the abort precedes everything else.
        opts = tc.RoundOpts(only=[], agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
        tc.work_units.run_round(types.SimpleNamespace(cfg=None, gh=None, rs=None, counters=None), opts)
    except tc.NoProgress as e:
        return str(e)
    finally:
        tc.work_units.survey = original
    return "(no NoProgress raised)"


GATEWAY = "gh pr list failed: HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)"

check(
    "the reason names the failure gh reported",
    abort_reason([GATEWAY]),
    f"{GATEWAY} — aborting round, not falling through to authoring",
)
check(
    "a multi-line stderr is flattened onto the one status line",
    abort_reason(["gh pr list failed: HTTP 502\n  Bad Gateway\n"]),
    "gh pr list failed: HTTP 502 Bad Gateway — aborting round, not falling through to authoring",
)
check(
    "with nothing captured it still says what it was doing",
    abort_reason([]),
    "gh pr list failed (GitHub API?) — aborting round, not falling through to authoring",
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""preflight() gates a HOST authoring round on a local `lake` toolchain — but review is not an
authoring stage: it runs the fetched-on-demand review engine and never compiles. Since the sandbox
default flipped to host, `tauceti work --only review` runs on the host by default, so a stray `lake`
requirement would falsely block every review-only machine that has no Lean toolchain. This test pins
that review-host preflight passes without `lake`, while an authoring stage still requires it.

Exit 0 = both cases agree; 1 = a mismatch."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def opts(only):
    return tc.RoundOpts(only=only, agent="claude", work_model="claude", sandbox_host=True, dry_run=False)


# Only `lake` is absent; gh/git/uvx present so preflight reaches the toolchain gate.
tc.cli._have = lambda tool: tool != "lake"
CFG = SimpleNamespace()  # review-host preflight never touches cfg (uses_fork excludes review)

raised = False
try:
    tc.cli.preflight(CFG, opts(["review"]))
except tc.Die:
    raised = True
check("host review without lake is NOT blocked", not raised)

# Guard the other direction: an authoring stage (fix) on the host STILL needs lake.
raised = False
try:
    tc.cli.preflight(CFG, opts(["fix"]))
except tc.Die as e:
    raised = "lake" in str(e)
check("host fix without lake IS blocked (lake still required for authoring)", raised)

sys.exit(1 if fails else 0)

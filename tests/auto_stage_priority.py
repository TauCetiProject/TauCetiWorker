#!/usr/bin/env python3
"""The unrestricted work predictor and runtime follow the documented priority order."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc


def check(name, got, want):
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    return ok


sv = tc.Survey(worker_id="test")
candidate = tc.Candidate(1, "deadbeef", "test")
sv.reviewable.actionable.append(candidate)
sv.needs_fix.actionable.append(candidate)

checks = [
    check("fix beats unrelated review", tc._next_auto_stage(sv), "fix"),
    check(
        "single shared auto order",
        tc.AUTO_STAGES,
        ("rebase", "bump", "progress", "fix-ci", "fix", "review"),
    ),
]
if "TAUCETI_PROGRESS_GAP" not in os.environ:
    checks.append(check("default progress attempt gap is eight hours", tc.PROGRESS_ATTEMPT_GAP, 8 * 3600))

progress_cmd = tc.progress_argv(Path("/worker-state"), "due")
checks.append(
    check(
        "progress tool cache is keyed by its immutable ref",
        progress_cmd[1:3],
        ["--cache-dir", f"/worker-state/cache/uvx/tauceti-progress/{tc.PROGRESS_REF}"],
    )
)
checks.append(check("progress tool still receives its command", progress_cmd[-1], "due"))

# Drive the real cascade as well as its status predictor. A future edit must not let their shared
# priority drift while leaving this display-only helper green.
saved_survey = tc.work_units.survey
saved_dispatch = tc.work_units.dispatch
seen = []
tc.work_units.survey = lambda *_a, **_k: sv
tc.work_units.dispatch = lambda stage, *_a, **_k: seen.append(stage) or 0
try:
    worker = SimpleNamespace(cfg=None, gh=None, rs=None, counters=None)
    tc.work_units.run_round(worker, SimpleNamespace(only=[], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("runtime cascade agrees with predictor", seen, ["fix"]))

# A due progress report must preempt the unbounded PR queues so they cannot starve the eight-hour
# cadence.
busy = tc.Survey(worker_id="test")
busy.progress.actionable.append(tc.Candidate(0, "", "due"))
busy.reviewable.actionable.append(candidate)
busy.red_ci.actionable.append(candidate)
checks.append(check("due progress beats fix-ci and review", tc._next_auto_stage(busy), "progress"))

saved_survey = tc.work_units.survey
saved_dispatch = tc.work_units.dispatch
seen = []
tc.work_units.survey = lambda *_a, **_k: busy
tc.work_units.dispatch = lambda stage, *_a, **_k: seen.append(stage) or 0
try:
    tc.work_units.run_round(worker, SimpleNamespace(only=[], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("runtime selects due progress before fix-ci and review", seen, ["progress"]))

# A stale cached due verdict is represented by progress returning None after its fresh plan re-check;
# the same round must continue to the next actionable stage instead of backing off.
seen = []
tc.work_units.survey = lambda *_a, **_k: busy
tc.work_units.dispatch = lambda stage, *_a, **_k: seen.append(stage) or (None if stage == "progress" else 0)
try:
    tc.work_units.run_round(worker, SimpleNamespace(only=[], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("stale progress verdict falls through to fix-ci", seen, ["progress", "fix-ci"]))

# The real progress implementation maps a fresh plan's not-due result onto that fallthrough signal.
saved_prepare_checkout = tc.work_units.prepare_checkout
saved_run = tc.work_units.subprocess.run
writes = []
plan_commands = []


def fake_run(argv, *_a, **_k):
    if "plan" in argv:
        plan_commands.append(argv)
        return SimpleNamespace(returncode=tc.EX_NOPROGRESS, stdout="", stderr="not due")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


with tempfile.TemporaryDirectory() as tmp:
    progress_worker = SimpleNamespace(
        cfg=SimpleNamespace(state=Path(tmp), checkout=Path(tmp) / "code"),
        counters=SimpleNamespace(write=lambda name, value: writes.append((name, value))),
    )
    tc.work_units.prepare_checkout = lambda _cfg: True
    tc.work_units.subprocess.run = fake_run
    try:
        progress_result = tc.work_units._do_progress_inner(progress_worker, None)
    finally:
        tc.work_units.prepare_checkout = saved_prepare_checkout
        tc.work_units.subprocess.run = saved_run
checks.append(check("fresh not-due plan returns the fallthrough signal", progress_result, None))
checks.append(check("fresh plan re-check records the attempt", writes[0][0], "progress-attempt-ts"))
checks.append(
    check(
        "progress bootstraps from main history",
        plan_commands[0][plan_commands[0].index("--ref") + 1],
        "origin/main",
    )
)

# Bumps and rebases remain ahead of reporting.
busy.bump.actionable.append(candidate)
checks.append(check("bump beats a due progress report", tc._next_auto_stage(busy), "bump"))
busy.rebaseable.actionable.append(candidate)
checks.append(check("rebase remains first", tc._next_auto_stage(busy), "rebase"))

# But with the queue empty it must be chosen ahead of roadmap authoring, or the open-ended fallback
# (which always has work) would defer it for ever.
quiet = tc.Survey(worker_id="test")
quiet.progress.actionable.append(tc.Candidate(0, "", "due"))
checks.append(check("progress beats roadmap authoring", tc._next_auto_stage(quiet), "progress"))

# And when no report is due, the fallback is roadmap authoring as before.
idle = tc.Survey(worker_id="test")
checks.append(check("no report due falls back to roadmap", tc._next_auto_stage(idle), "roadmap"))

sv.red_ci.actionable.append(candidate)
checks.append(check("red CI beats review findings", tc._next_auto_stage(sv), "fix-ci"))

sv.rebaseable.actionable.append(candidate)
checks.append(check("conflict remains first", tc._next_auto_stage(sv), "rebase"))

sys.exit(0 if all(checks) else 1)

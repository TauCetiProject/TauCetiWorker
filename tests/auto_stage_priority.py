#!/usr/bin/env python3
"""The unrestricted work predictor and runtime follow the documented priority order."""

import os
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        ("rebase", "bump", "lint-repair", "progress", "fix-ci", "fix", "review", "overlap-fix", "overlap-scan"),
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
    worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None, rs=None, counters=None)
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
plan_calls = []


def fake_run(argv, *_a, **_k):
    if "plan" in argv:
        plan_calls.append(argv)
        return SimpleNamespace(returncode=tc.EX_NOPROGRESS, stdout="", stderr="not due")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


with tempfile.TemporaryDirectory() as tmp:
    progress_worker = SimpleNamespace(
        cfg=SimpleNamespace(state=Path(tmp), checkout=Path(tmp) / "code", logdir=Path(tmp) / "logs"),
        counters=SimpleNamespace(write=lambda name, value: writes.append((name, value))),
    )
    tc.work_units.prepare_checkout = lambda _cfg, **_: True
    tc.work_units.subprocess.run = fake_run
    try:
        progress_result = tc.work_units._do_progress_inner(progress_worker, None)
    finally:
        tc.work_units.prepare_checkout = saved_prepare_checkout
        tc.work_units.subprocess.run = saved_run
checks.append(check("fresh not-due plan returns the fallthrough signal", progress_result, None))
checks.append(
    check("worker requires 25 PRs for a progress report", plan_calls[0][plan_calls[0].index("--min-prs") + 1], "25")
)
checks.append(
    check(
        "fresh not-due plan does not spend a prose attempt", any(k == "progress-attempt-ts" for k, _ in writes), False
    )
)

# Starting mechanical progress work consumes the cached verdict even when the fresh plan fails.
# Genuine errors still charge the tool error breaker, while the prose cadence remains unspent.
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp)
    due_cache = state / "cache" / "progress-due.json"
    due_cache.parent.mkdir(parents=True)
    due_cache.write_text('{"due": true, "reason": "due"}')
    counters = tc.Counters(SimpleNamespace(state=state))
    progress_worker = SimpleNamespace(
        cfg=SimpleNamespace(state=state, checkout=state / "code", logdir=state / "logs"),
        counters=counters,
    )

    def failing_plan(argv, *_a, **_k):
        if "plan" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="broken plan")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    tc.work_units.prepare_checkout = lambda _cfg, **_: True
    tc.work_units.subprocess.run = failing_plan
    try:
        try:
            tc.work_units._do_progress_inner(progress_worker, None)
        except tc.Die:
            pass
        else:
            checks.append(check("a failed progress plan raises Die", False, True))
    finally:
        tc.work_units.prepare_checkout = saved_prepare_checkout
        tc.work_units.subprocess.run = saved_run
    checks.append(check("a failed attempt consumes the cached due verdict", due_cache.exists(), False))
    checks.append(check("a failed plan still charges the error breaker", counters.read(tc.PROGRESS_ERROR_KEY), 1))
    checks.append(
        check("a failed mechanical plan does not spend the prose cadence", counters.read("progress-attempt-ts"), 0)
    )

# Upgrading the progress tool must recover workers latched by the previous release, while
# repeated failures of the current release still trip the breaker without another tool call.
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp)
    cfg = SimpleNamespace(state=state)
    counters = tc.Counters(cfg)
    counters.write("progress-err", tc.MAX_PROGRESS_ERRORS)  # legacy unversioned state
    counters.write("progress-err-old-release", tc.MAX_PROGRESS_ERRORS)
    calls = []

    def due_tool(argv, *_a, **_k):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="due", stderr="")

    tc.work_units.subprocess.run = due_tool
    try:
        due, reason = tc.progress_due(cfg, counters)
        checks.append(check("old progress failures do not suppress an upgraded tool", due, True))
        for _ in range(tc.MAX_PROGRESS_ERRORS):
            counters.incr(tc.PROGRESS_ERROR_KEY)
        tc.bust_progress_cache(cfg)
        due, reason = tc.progress_due(cfg, counters)
        checks.append(check("current progress failures still trip the breaker", due, False))
        checks.append(check("the breaker names the current counter file", tc.PROGRESS_ERROR_KEY in reason, True))
        checks.append(check("a latched current release does not launch the tool", len(calls), 1))
    finally:
        tc.work_units.subprocess.run = saved_run

# Branch refs used to trial a release must still produce a writable counter filename.
with tempfile.TemporaryDirectory() as tmp:
    with patch.dict(os.environ, {"TAUCETI_PROGRESS_REF": "fix/bors-progress"}):
        branch_constants = runpy.run_path(str(REPO / "tauceti_worker" / "constants.py"))
    branch_key = branch_constants["PROGRESS_ERROR_KEY"]
    counters = tc.Counters(SimpleNamespace(state=Path(tmp)))
    for _ in range(tc.MAX_PROGRESS_ERRORS):
        counters.incr(branch_key)
    checks.append(check("a branch ref can persist its error budget", counters.read(branch_key), tc.MAX_PROGRESS_ERRORS))
    checks.append(check("a branch ref does not become a counter directory", "/" in branch_key, False))

# If no requested phase can run, a focused progress worker must retain the survey's suppression reason
# for its manager/status output instead of collapsing it into the generic no-eligible-work fallback.
suppressed = tc.Survey(worker_id="test")
suppressed.progress.suppressed.append(tc.Candidate(0, "", "progress rounds have failed 3x; clear the breaker"))
saved_survey = tc.work_units.survey
tc.work_units.survey = lambda *_a, **_k: suppressed
try:
    try:
        tc.work_units.run_round(worker, SimpleNamespace(only=["bump", "progress"], dry_run=True))
    except tc.NoProgress as exc:
        suppressed_reason = str(exc)
    else:
        suppressed_reason = ""
finally:
    tc.work_units.survey = saved_survey
checks.append(
    check(
        "focused progress no-work preserves its suppression reason",
        suppressed_reason,
        "progress: progress rounds have failed 3x; clear the breaker",
    )
)

# Bumps and rebases remain ahead of reporting.
busy.bump.actionable.append(candidate)
checks.append(check("bump beats a due progress report", tc._next_auto_stage(busy), "bump"))
busy.bump.actionable.clear()
busy.lint_repair.actionable.append(candidate)
checks.append(check("lint-repair beats a due progress report", tc._next_auto_stage(busy), "lint-repair"))
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
idle.overlap_scan.actionable.append(tc.Candidate(0, "cursor", "behind pin"))
checks.append(check("Mathlib scan beats new roadmap authoring", tc._next_auto_stage(idle), "overlap-scan"))
idle.overlap_fix.actionable.append(tc.Candidate(0, "123", "overlap issue"))
checks.append(check("overlap repair beats further scanning", tc._next_auto_stage(idle), "overlap-fix"))
idle.reviewable.actionable.append(candidate)
checks.append(check("existing PR review still beats overlap work", tc._next_auto_stage(idle), "review"))

sv.red_ci.actionable.append(candidate)
checks.append(check("red CI beats review findings", tc._next_auto_stage(sv), "fix-ci"))

sv.rebaseable.actionable.append(candidate)
checks.append(check("conflict remains first", tc._next_auto_stage(sv), "rebase"))


# Work on an automatic Mathlib bump comes before every other candidate, whatever its stage, and
# keeps stage order among bump candidates. Main moves faster than a bump can wait its turn.
def pr_info(number, *, bump):
    return tc.PRInfo(
        number=number,
        head_oid="b" * 40,
        head_ref=("bump-mathlib/" + "c" * 40) if bump else "feature",
        head_owner="TauCetiProject",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="MERGEABLE",
        author="tauceti-review-bot[bot]" if bump else "someone",
        build_success=True,
        build_failed=False,
        author_is_bot=bump,
    )


mixed = tc.Survey(worker_id="test")
mixed.open_prs = [pr_info(7, bump=False), pr_info(9, bump=True)]
other, bump_fix, bump_review = (
    tc.Candidate(7, "b" * 40, "conflicting"),
    tc.Candidate(9, "b" * 40, "blocking review at head"),
    tc.Candidate(9, "b" * 40, "ready for review"),
)
mixed.rebaseable.actionable.append(other)
mixed.progress.actionable.append(tc.Candidate(0, "", "due"))
mixed.needs_fix.actionable.append(bump_fix)
mixed.reviewable.actionable.append(bump_review)
checks.append(check("bump candidates precede every other stage", tc._next_auto_stage(mixed), "fix"))
checks.append(
    check(
        "cascade order: bump work in stage order, then the rest",
        [(stage, c.pr) for stage, c in tc.cascade(mixed, tc.AUTO_STAGES)],
        [("fix", 9), ("review", 9), ("rebase", 7), ("progress", 0)],
    )
)
checks.append(check("non-bump PRs are not promoted", tc.bump_prs(mixed), {9}))

seen = []
tc.work_units.survey = lambda *_a, **_k: mixed
tc.work_units.dispatch = lambda stage, _w, _sv, c, _o: seen.append((stage, c.pr)) or (None if stage == "fix" else 0)
try:
    tc.work_units.run_round(worker, SimpleNamespace(only=[], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("runtime offers bump work first and falls through in order", seen, [("fix", 9), ("review", 9)]))

seen = []
tc.work_units.survey = lambda *_a, **_k: mixed
tc.work_units.dispatch = lambda stage, _w, _sv, c, _o: seen.append((stage, c.pr)) or 0
try:
    tc.work_units.run_round(worker, SimpleNamespace(only=["rebase", "review"], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("--only still restricts the stages bump work can use", seen, [("review", 9)]))

# A peer's first-refusal window defers ordinary reviews, never a bump review.
import time as _time

affine = tc.Survey(worker_id="test")
affine.open_prs = [pr_info(7, bump=False), pr_info(9, bump=True)]
for number in (7, 9):
    affine.reviewable.actionable.append(
        tc.Candidate(number, "b" * 40, "ready for review", preferred_reviewer="peer", ready_at=_time.time())
    )
seen = []
saved_me = tc.work_units.me
tc.work_units.survey = lambda *_a, **_k: affine
tc.work_units.dispatch = lambda stage, _w, _sv, c, _o: seen.append((stage, c.pr)) or None
tc.work_units.review_publisher = lambda: "me"
try:
    try:
        tc.work_units.run_round(worker, SimpleNamespace(only=["review"], dry_run=True))
    except Exception:
        pass  # nothing performed: the round reports no progress
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
    tc.work_units.me = saved_me
checks.append(check("bump review skips the peer affinity window", seen, [("review", 9)]))

sys.exit(0 if all(checks) else 1)

#!/usr/bin/env python3
"""A head whose pr-build finished without posting the `build` status is red, not pending.

The runner can die before pr-build's report step (TauCeti#11179: the build ran it out of memory), and
then no `build` status ever appears and nothing re-runs the build without a push. These cases lock in
when survey.build_lost calls such a head lost, and that survey.lost_builds only spends a REST call on a
head that has no status and has not moved for the grace period, caching the verdict per head.

Exit 0 = all cases classify correctly; 1 = a mismatch.
"""

import importlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
survey_mod = importlib.import_module("tauceti_worker.survey")

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


NOW = 1_790_000_000.0
GRACE = survey_mod.LOST_BUILD_GRACE_S


def iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(status="completed", conclusion="failure", ago=GRACE + 60):
    return {"status": status, "conclusion": conclusion, "updated_at": iso(NOW - ago)}


# --- build_lost: which run lists prove the status is never coming --------------------------------
lost = survey_mod.build_lost
check("no runs: pending, not lost", lost([], NOW), False)
check("a failed run past the grace: lost", lost([run()], NOW), True)
check("a failed run inside the grace: not yet", lost([run(ago=GRACE - 60)], NOW), False)
check("a run still in progress: pending", lost([run(status="in_progress", conclusion=None), run()], NOW), False)
check("only cancelled runs: superseded, not lost", lost([run(conclusion="cancelled")], NOW), False)
check(
    "the newest verdict decides the grace",
    lost([run(conclusion="cancelled", ago=60), run(ago=GRACE - 60), run(ago=GRACE + 600)], NOW),
    False,
)
check("a successful run without its status is lost too", lost([run(conclusion="success")], NOW), True)


# --- lost_builds: which PRs are looked up, and the per-head cache ---------------------------------
def pr(number, *, reported=False, moved_ago=GRACE + 60):
    return survey_mod.PRInfo(
        number=number,
        head_oid=f"head-{number}",
        head_ref="feature",
        head_owner="fork",
        head_repo="TauCeti",
        is_draft=False,
        mergeable="MERGEABLE",
        author="me",
        build_success=False,
        build_failed=False,
        updated_at=iso(NOW - moved_ago),
        build_reported=reported,
    )


class GH:
    def __init__(self, runs):
        self.runs = runs
        self.calls = []

    def pr_build_runs(self, head):
        self.calls.append(head)
        return self.runs.get(head)


with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(state=Path(tmp))
    gh = GH({"head-1": [run()], "head-4": [run(status="queued", conclusion=None)]})
    prs = [pr(1), pr(2, reported=True), pr(3, moved_ago=60), pr(4), pr(5)]
    check("only the stuck head is lost", survey_mod.lost_builds(cfg, gh, prs, NOW), {1})
    check("a head with a status, or a PR that moved recently, costs no call", gh.calls, ["head-1", "head-4", "head-5"])
    cached = json.loads((Path(tmp) / "cache" / "lost-builds.json").read_text())
    check("a failed read is not cached", sorted(cached), ["head-1", "head-4"])

    gh.calls.clear()
    check("a cached verdict is reused", survey_mod.lost_builds(cfg, gh, prs, NOW + 60), {1})
    check("so only the unread head is fetched again", gh.calls, ["head-5"])

    gh.calls.clear()
    survey_mod.lost_builds(cfg, gh, prs, NOW + survey_mod.LOST_BUILD_TTL + 1)
    check("an expired verdict is fetched again", gh.calls, ["head-1", "head-4", "head-5"])

    (Path(tmp) / "cache" / "lost-builds.json").write_text("not json")
    check("a corrupt cache reads as empty", survey_mod.lost_builds(cfg, gh, prs, NOW), {1})

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""A head whose pr-build finished without posting the `build` status is red, not pending.

The runner can die before pr-build's report step (TauCeti#11179: the build ran it out of memory), and
then no `build` status ever appears and nothing re-runs the build without a push. These cases lock in
when survey.build_lost calls such a head lost (including the day-long fallback for builds that were
all cancelled), that a replacement dispatched by PR number keeps it pending although the API keys that
run to main, and that survey.lost_builds only spends a REST call on a head that has no status and has
not moved for the grace period, caching the verdict per head.

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


CANCEL_GRACE = survey_mod.LOST_BUILD_CANCEL_GRACE_S


def run(status="completed", conclusion="failure", ago=GRACE + 60, title=None):
    r = {"status": status, "conclusion": conclusion, "updated_at": iso(NOW - ago)}
    if title is not None:
        r["display_title"] = title
    return r


# --- build_lost: which run lists prove the status is never coming --------------------------------
lost = survey_mod.build_lost
check("no runs: pending, not lost", lost([], NOW), False)
check("a failed run past the grace: lost", lost([run()], NOW), True)
check("a failed run inside the grace: not yet", lost([run(ago=GRACE - 60)], NOW), False)
check("a run still in progress: pending", lost([run(status="in_progress", conclusion=None), run()], NOW), False)
check("only cancelled runs: superseded, not lost", lost([run(conclusion="cancelled")], NOW), False)
check(
    "only cancelled runs, a day old: nothing superseded them",
    lost([run(conclusion="cancelled", ago=CANCEL_GRACE + 60)], NOW),
    True,
)
check("only skipped runs, a day old: lost too", lost([run(conclusion="skipped", ago=CANCEL_GRACE + 60)], NOW), True)
check(
    "the newest verdict decides the grace",
    lost([run(conclusion="cancelled", ago=60), run(ago=GRACE - 60), run(ago=GRACE + 600)], NOW),
    False,
)
check("a successful run without its status is lost too", lost([run(conclusion="success")], NOW), True)

# --- dispatch_pending: a replacement dispatched by PR number is keyed to main, not the head -------
pending = survey_mod.dispatch_pending
active = {"status": "in_progress", "conclusion": None}
check("no dispatches", pending([], 1, NOW), False)
check("a running dispatch for this PR", pending([run(**active, title="pr-build 1")], 1, NOW), True)
check("a running dispatch for another PR", pending([run(**active, title="pr-build 2")], 1, NOW), False)
check("a running untitled dispatch may be this PR's", pending([run(**active, title="pr-build")], 1, NOW), True)
check("a dispatch for this PR that just finished", pending([run(ago=60, title="pr-build 1")], 1, NOW), True)
check("a dispatch for this PR that finished long ago", pending([run(title="pr-build 1")], 1, NOW), False)
check("a finished untitled dispatch cannot be attributed", pending([run(ago=60, title="pr-build")], 1, NOW), False)


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
    def __init__(self, runs, dispatches=()):
        self.runs = runs
        self.dispatches = list(dispatches)
        self.calls = []

    def pr_build_runs(self, head):
        self.calls.append(head)
        return self.runs.get(head)

    def pr_build_dispatches(self):
        self.calls.append("dispatches")
        return self.dispatches


with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(state=Path(tmp))
    gh = GH({"head-1": [run()], "head-4": [run(status="queued", conclusion=None)]})
    prs = [pr(1), pr(2, reported=True), pr(3, moved_ago=60), pr(4), pr(5)]
    check("only the stuck head is lost", survey_mod.lost_builds(cfg, gh, prs, NOW), {1})
    check(
        "a head with a status, or a PR that moved recently, costs no call",
        gh.calls,
        ["head-1", "head-4", "head-5", "dispatches"],
    )
    cached = json.loads((Path(tmp) / "cache" / "lost-builds.json").read_text())
    check("a failed read is not cached", sorted(cached), ["head-1", "head-4"])

    gh.calls.clear()
    check("a cached verdict is reused", survey_mod.lost_builds(cfg, gh, prs, NOW + 60), {1})
    check("so only the unread head is fetched again", gh.calls, ["head-5", "dispatches"])

    gh.calls.clear()
    survey_mod.lost_builds(cfg, gh, prs, NOW + survey_mod.LOST_BUILD_TTL + 1)
    check("an expired verdict is fetched again", gh.calls, ["head-1", "head-4", "head-5", "dispatches"])

    (Path(tmp) / "cache" / "lost-builds.json").write_text("not json")
    check("a corrupt cache reads as empty", survey_mod.lost_builds(cfg, gh, prs, NOW), {1})

with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(state=Path(tmp))
    # The old run died without a status; a replacement dispatched by PR number is still building. Its
    # API head_sha is main's, so only the dispatch listing can see it.
    gh = GH({"head-1": [run()]}, [run(**active, title="pr-build 1")])
    check("an active dispatched replacement keeps the PR pending", survey_mod.lost_builds(cfg, gh, [pr(1)], NOW), set())
    gh.dispatches = [run(**active, title="pr-build 9")]
    check("a dispatch for another PR does not", survey_mod.lost_builds(cfg, gh, [pr(1)], NOW), {1})
    gh.dispatches = None
    check("an unreadable dispatch listing declares nothing lost", survey_mod.lost_builds(cfg, gh, [pr(1)], NOW), set())
    gh = GH({"head-2": [run(status="queued", conclusion=None)]})
    survey_mod.lost_builds(cfg, gh, [pr(2)], NOW)
    check("dispatches are read only when a head looks lost", gh.calls, ["head-2"])

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

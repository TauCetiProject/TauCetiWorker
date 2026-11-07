#!/usr/bin/env python3
"""A head whose pr-build finished without posting the `build` status is red, not pending.

The runner can die before pr-build's report step (TauCeti#11179: the build ran it out of memory), and
then no `build` status ever appears and nothing re-runs the build without a push. These cases lock in
when survey.build_lost calls such a head lost (including the day-long fallback for builds that were
all cancelled); that survey.lost_builds only spends a REST call on a head that has no status and has
not moved for the grace period, caching the verdict per head; and that before fix-ci spends on such a
head, survey.confirm_lost_build re-reads it live: a rerun started inside the cache's TTL, or a
replacement dispatched by PR number (which the API keys to main) anywhere in the listing, declines it.
Those last cases run through the real REST wrappers against a fake that pages like GitHub.

Exit 0 = all cases classify correctly; 1 = a mismatch.
"""

import importlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

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


# --- confirm_lost_build, through the real REST wrappers -------------------------------------------
def done(stdout="", rc=0):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


class FakeREST(tc.GitHub):
    """GitHub whose `gh` is an in-memory Actions API that filters and pages as GitHub's does, so the
    wrappers' own query strings and pagination are what decide what the test sees."""

    def __init__(self, runs, *, view=None, failing=()):
        super().__init__("owner/repo")
        self.runs, self.failing, self.calls = runs, set(failing), []
        self.view = {"headRefOid": "head-1", "isDraft": False, "state": "OPEN", "statusCheckRollup": []}
        self.view.update(view or {})

    def _gh(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            return done(json.dumps(self.view))
        path = next(a for a in args if a.startswith("/repos/"))
        query = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        if query.get("status") in self.failing:
            return done(rc=1)
        rows = [r for r in self.runs if matches(r, query)]
        size = int(query.get("per_page", 30))
        pages = [rows[i : i + size] for i in range(0, len(rows), size)] or [[]]
        shown = [r for page in (pages if "--paginate" in args else pages[:1]) for r in page]
        fields = [{k: r.get(k) for k in ("status", "conclusion", "updated_at", "display_title")} for r in shown]
        jq = args[args.index("--jq") + 1]
        return done(json.dumps(fields) if jq.startswith("[") else "\n".join(map(json.dumps, fields)))


def matches(r, query):
    if "event" in query and r["event"] != query["event"]:
        return False
    if "head_sha" in query and r["head_sha"] != query["head_sha"]:
        return False
    if "status" in query and r["status"] != query["status"]:
        return False
    since = query.get("created", "")
    return not since.startswith(">=") or r["created_at"] >= since[2:]


def head_run(status="completed", conclusion="failure", ago=GRACE + 60):
    return {
        **run(status, conclusion, ago),
        "event": "pull_request_target",
        "head_sha": "head-1",
        "created_at": iso(NOW - ago - 600),
    }


def dispatch(pr_number, status="in_progress", conclusion=None, ago=60):
    return {
        **run(status, conclusion, ago, title=f"pr-build {pr_number}"),
        "event": "workflow_dispatch",
        "head_sha": "main-sha",
        "created_at": iso(NOW - ago - 600),
    }


confirm = survey_mod.confirm_lost_build
busy = [dispatch(200 + i) for i in range(120)]  # more active dispatches than one page holds
check("a lost head with no replacement is confirmed", confirm(FakeREST([head_run(), *busy]), 1, "head-1", NOW), True)
check(
    "an active replacement past the first page keeps it pending",
    confirm(FakeREST([head_run(), *busy, dispatch(1)]), 1, "head-1", NOW),
    False,
)
check(
    "so does a queued one",
    confirm(FakeREST([head_run(), *busy, dispatch(1, status="queued")]), 1, "head-1", NOW),
    False,
)
check(
    "and one that finished within the grace",
    confirm(FakeREST([head_run(), *busy, dispatch(1, "completed", "failure", ago=60)]), 1, "head-1", NOW),
    False,
)
check(
    "an unreadable listing confirms nothing",
    confirm(FakeREST([head_run()], failing=("waiting",)), 1, "head-1", NOW),
    False,
)
check(
    "a moved head is not confirmed",
    confirm(FakeREST([head_run()], view={"headRefOid": "head-2"}), 1, "head-1", NOW),
    False,
)
check(
    "a status posted since is not confirmed",
    confirm(
        FakeREST([head_run()], view={"statusCheckRollup": [{"context": "build", "state": "FAILURE"}]}), 1, "head-1", NOW
    ),
    False,
)

# A verdict cached as lost, then a rerun of the head's own workflow starts inside the TTL. Discovery
# still answers from the cache; the gate before fix-ci spends reads the head's runs again.
with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(state=Path(tmp))
    rest = FakeREST([head_run()])
    check("the head is found lost", survey_mod.lost_builds(cfg, rest, [pr(1)], NOW), {1})
    rest.runs = [head_run(status="in_progress", conclusion=None)]
    check(
        "a rerun inside the TTL leaves the cached discovery", survey_mod.lost_builds(cfg, rest, [pr(1)], NOW + 60), {1}
    )
    worker = SimpleNamespace(gh=rest)
    lost_c = tc.Candidate(1, "head-1", survey_mod.LOST_BUILD_REASON)
    check(
        "but fix-ci declines it before spending", tc.work_units._still_actionable("fix-ci", worker, None, lost_c), False
    )
    rest.calls.clear()
    red_c = tc.Candidate(1, "head-1", "build failed at head")
    check("an ordinary red build is taken", tc.work_units._still_actionable("fix-ci", worker, None, red_c), True)
    check("without any extra call", rest.calls, [])

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""WHERE the Claude bootstrap may happen: the launch stage, and nowhere earlier.

Opening a reset window costs a real request, so the decision to spend it sits at the one point where
everything it depends on is already true — a concrete work unit has been chosen, the survey (and with
it the GitHub preflight) succeeded, and Claude is the model actually about to run. Deciding that there
is nothing to do must cost nothing.

This suite drives the real call chain — resolve_work_model, cmd_loop, run_round, dispatch — with the
endpoint and the request itself stubbed, and counts requests. Dependency-free; no network.
"""

import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


tc.quota._claude_keychain_creds = lambda: None


def iso(delta_s):
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


# A session that has reset and not reopened, with a healthy weekly: the one state a bootstrap fixes.
IDLE = {"five_hour": None, "seven_day": {"utilization": 10, "resets_at": iso(3 * 86400)}}

CALLS = {"bootstrap": 0}


def stub_endpoint(url, headers, timeout=15):
    return 200, IDLE, None


def stub_bootstrap(_self):
    CALLS["bootstrap"] += 1
    return True, "ok"


tc.quota._http_get_json = stub_endpoint
tc.quota.Quota._claude_bootstrap_request = stub_bootstrap


def make_cfg(wid="test"):
    home = Path(tempfile.mkdtemp())
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(tc.json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    state = home / "state" / wid
    return tc.Config(
        wid=wid,
        home=home,
        data_home=home,
        state=state,
        checkout=home / "co",
        store_dir=home / "store",
        sbcache=state / "cache" / "scoreboard",
        logdir=home / "logs",
        quota_cache=state / "cache",
    )


class Stop(Exception):
    """Breaks out of the driver loop after the step under test."""


class FakeTime:
    """time for the loop module: sleeping means the loop decided to wait, which ends the test."""

    @staticmethod
    def sleep(_n):
        raise Stop

    time = staticmethod(time.time)


# --- selecting a model never spends ---------------------------------------------------------------
CALLS["bootstrap"] = 0
cfg = make_cfg()
model, pending = tc.resolve_work_model(cfg, "claude", dry=False, ignore_quota=False, quota_cmd=None)
check("an unopened window is selectable, provisionally", (model, pending), ("claude", True))
check("selecting it spends nothing", CALLS["bootstrap"], 0)
check("choosing is pure", (tc.Quota(cfg).choose("claude")[0], CALLS["bootstrap"]), (None, 0))

# --- the driver loop: preflight first, request later ------------------------------------------------
real_time = tc.loop.time
real_budget = tc.loop.github_budget
real_spawn = tc.loop.run_round_subprocess
tc.loop.time = FakeTime

spawned = []


def stub_round(tail):
    spawned.append(tail)
    raise Stop


tc.loop.run_round_subprocess = stub_round
args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)

# GitHub budget too low ⇒ the round can't proceed ⇒ nothing is spent on quota either.
CALLS["bootstrap"] = 0
spawned.clear()
tc.loop.github_budget = lambda: {"core": (0, int(time.time()) + 60)}
try:
    tc.cmd_loop(args, make_cfg("gh-low"), only=["review"], agent="claude")
except Stop:
    pass
check("a failed GitHub preflight spawns no round", spawned, [])
check("...and makes no bootstrap request", CALLS["bootstrap"], 0)

# Budget fine ⇒ the round is spawned, carrying the authorization — and the LOOP still spends nothing.
CALLS["bootstrap"] = 0
spawned.clear()
tc.loop.github_budget = lambda: {"core": (5000, 0)}
try:
    tc.cmd_loop(args, make_cfg("gh-ok"), only=["review"], agent="claude")
except Stop:
    pass
check("a round is spawned", len(spawned), 1)
check("...pinned to claude", "claude" in spawned[0], True)
check("...carrying the launch-stage authorization", "--claude-bootstrap" in spawned[0], True)
check("the loop itself never made the request", CALLS["bootstrap"], 0)

tc.loop.time = real_time
tc.loop.github_budget = real_budget
tc.loop.run_round_subprocess = real_spawn

# --- a round that finds no work spends nothing -------------------------------------------------------
CALLS["bootstrap"] = 0
cfg = make_cfg("no-work")
tc.work_units.survey = lambda *a, **k: tc.Survey(worker_id=cfg.wid)
tc.work_units.mirror_creds = lambda _cfg: None
w = tc.Worker(cfg, None, None, tc.Counters(cfg), None, None)
opts = tc.RoundOpts(
    only=["review"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False, claude_bootstrap=True
)
try:
    tc.run_round(w, opts)
    check("a round with no work raises NoProgress", False, True)
except tc.NoProgress as e:
    check("a round with no work raises NoProgress", "no eligible work" in str(e), True)
check("deciding there is nothing to do costs no quota", CALLS["bootstrap"], 0)

# --- with work in hand, dispatch asks for authorization exactly once ----------------------------------
authorized = {"n": 0}


def stub_authorize(_self):
    authorized["n"] += 1
    return tc.Provider("claude", True, "opus", [tc.Window("session", 1.0, 20.0, None, "under-pace", 20.0)])


ran = []
tc.work_units._host_agent_binary = lambda stage, model: None  # the binary preflight is not what's under test
tc.work_units._still_actionable = lambda *a: True  # nor is the pre-launch re-read (see review_state_freshness)
tc.work_units.do_review = lambda *a, **k: ran.append("review") or 0
tc.quota.Quota.authorize_claude_launch = stub_authorize

rc = tc.work_units.dispatch("review", w, tc.Survey(worker_id=cfg.wid), tc.Candidate(1, "head", ""), opts)
check("with work in hand, the launch stage runs", authorized["n"], 1)
check("...and the stage then runs", (ran, rc), (["review"], 0))

# Authorization that comes back unavailable stops the round instead of launching the model.
authorized["n"] = 0
ran.clear()


def stub_refuse(_self):
    authorized["n"] += 1
    win = tc.Window("session", None, 0.0, None, tc.STATE_IDLE, 0.0, "bootstrap failed: claude exited 1")
    return tc.Provider("claude", False, None, [win])


tc.quota.Quota.authorize_claude_launch = stub_refuse
try:
    tc.work_units.dispatch("review", w, tc.Survey(worker_id=cfg.wid), tc.Candidate(1, "head", ""), opts)
    check("a refused launch stops the round", False, True)
except tc.NoProgress as e:
    check("a refused launch stops the round", "bootstrap failed" in str(e), True)
check("...before the model runs", ran, [])
check("...having asked exactly once", authorized["n"], 1)

# A round that was NOT given the authorization never asks, even on claude.
authorized["n"] = 0
plain = tc.RoundOpts(only=["review"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)
tc.work_units.do_review = lambda *a, **k: ran.append("review") or 0
tc.work_units.dispatch("review", w, tc.Survey(worker_id=cfg.wid), tc.Candidate(1, "head", ""), plain)
check("an unauthorized round never asks to spend", authorized["n"], 0)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

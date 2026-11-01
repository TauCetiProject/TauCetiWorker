#!/usr/bin/env python3
"""Ordinary idle-Claude selection/launch retains GitHub preflight and no-work behaviour."""

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


# An explicitly idle session and a weekly window with headroom.
IDLE = {"five_hour": None, "seven_day": {"utilization": 10, "resets_at": iso(3 * 86400)}}

CALLS = {"bootstrap": 0}
real_run = tc.subprocess.run


def no_dummy_turn(argv, *args, **kwargs):
    if isinstance(argv, (list, tuple)) and "claude" in Path(str(argv[0])).name and "-p" in argv:
        CALLS["bootstrap"] += 1
        raise AssertionError("selection/preflight must not send dummy model turns")
    return real_run(argv, *args, **kwargs)


tc.subprocess.run = no_dummy_turn


def stub_endpoint(url, headers, timeout=15):
    return 200, IDLE, None


tc.quota._http_get_json = stub_endpoint
assert not hasattr(tc.Quota, "authorize_claude_launch")


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
model = tc.resolve_work_model(cfg, "claude", dry=False, ignore_quota=False, quota_cmd=None)
check("an idle window permits ordinary selection", model, "claude")
check("selecting it spends nothing", CALLS["bootstrap"], 0)
check("choosing is pure", (tc.Quota(cfg).choose("claude")[0], CALLS["bootstrap"]), ("claude", 0))

# --- the driver loop retains preflight ------------------------------------------------
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

# Budget fine: spawn an ordinary round without a separate initialization handoff.
CALLS["bootstrap"] = 0
spawned.clear()
tc.loop.github_budget = lambda: {"core": (5000, 0)}
try:
    tc.cmd_loop(args, make_cfg("gh-ok"), only=["review"], agent="claude")
except Stop:
    pass
check("a round is spawned", len(spawned), 1)
check("...pinned to claude", "claude" in spawned[0], True)
check("...without a bootstrap handoff", "--claude-bootstrap" in spawned[0], False)
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
opts = tc.RoundOpts(only=["review"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)
try:
    tc.run_round(w, opts)
    check("a round with no work raises NoProgress", False, True)
except tc.NoProgress as e:
    check("a round with no work raises NoProgress", "no eligible work" in str(e), True)
check("deciding there is nothing to do costs no quota", CALLS["bootstrap"], 0)

# Ordinary dispatch uses the existing work/preflight path, without another quota request.
ran = []
tc.work_units._host_agent_binary = lambda stage, model: None
tc.work_units._still_actionable = lambda *a: True
tc.work_units.do_review = lambda *a, **k: ran.append("review") or 0
rc = tc.work_units.dispatch("review", w, tc.Survey(worker_id=cfg.wid), tc.Candidate(1, "head", ""), opts)
check("eligible idle Claude work dispatches normally", (ran, rc), (["review"], 0))
check("dispatch makes no dummy turn", CALLS["bootstrap"], 0)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""The pacer's under/over-pace threshold is a piecewise-linear budget curve from $TAUCETI_PACE
(--pace), defaulting to `60:40`. Verify parsing (incl. endpoint fill and rejection of bad specs),
interpolation, that _classify_window actually honours the curve, and that a window the curve blocks
reports when the curve frees it. Dependency-free; no network."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    fails += not cond


# --- parse_pace_curve: shape + endpoint fill --------------------------------------------------------
DEFAULT = [(0.0, 0.0), (60.0, 40.0), (100.0, 100.0)]  # the shipped default, spelled out
check("the default curve is 60:40", tc._DEFAULT_PACE == DEFAULT)
check("empty -> default curve", tc.parse_pace_curve("") == DEFAULT)
check("None -> default curve", tc.parse_pace_curve(None) == DEFAULT)
check(
    "fills both endpoints (0->0, 100->100)",
    tc.parse_pace_curve("50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)],
)
check(
    "keeps operator endpoints when given",
    tc.parse_pace_curve("0:10,50:70,90:90") == [(0.0, 10.0), (50.0, 70.0), (90.0, 90.0), (100.0, 100.0)],
)
check("sorts out-of-order points", tc.parse_pace_curve("90:90,0:10") == [(0.0, 10.0), (90.0, 90.0), (100.0, 100.0)])
check("budget may exceed 100", tc.parse_pace_curve("100:250") == [(0.0, 0.0), (100.0, 250.0)])
check("dup time same budget is fine", tc.parse_pace_curve("50:70,50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])


def rejects(spec):
    try:
        tc.parse_pace_curve(spec)
        return False
    except ValueError:
        return True


for bad in ("abc", "50", "50:", ":70", "150:10", "-5:10", "50:-1", "50:70,50:80", "50:x"):
    check(f"rejects {bad!r}", rejects(bad))
# non-finite values slip through float() but must be rejected (they'd unlock all spend / make NaN budgets)
for bad in ("50:inf", "50:nan", "50:1e999", "nan:50", "inf:50", "50:-inf"):
    check(f"rejects non-finite {bad!r}", rejects(bad))
# a spec with real (comma) structure but no points is a typo, not a request for the default
for bad in (",", ",,", ", ,"):
    check(f"rejects empty-but-nonblank {bad!r}", rejects(bad))
# but a genuinely blank/whitespace spec is 'unset' = the default, and stray commas around real points are fine
check("blank -> default curve", tc.parse_pace_curve("   ") == DEFAULT)
check("tolerates stray commas", tc.parse_pace_curve("50:70,") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])


# --- pace_budget: interpolation ---------------------------------------------------------------------
c = tc.parse_pace_curve("0:10,50:70,90:90")
check("budget at a control point", tc.pace_budget(c, 50) == 70.0)
check("budget interpolated midway 0..50", tc.pace_budget(c, 25) == 40.0)  # 10 + (25/50)*(70-10)
check("budget interpolated 50..90", tc.pace_budget(c, 70) == 80.0)  # 70 + (20/40)*(90-70)
check("budget clamps below 0", tc.pace_budget(c, -10) == 10.0)  # e clamped to 0 -> 10
check("budget clamps above 100", tc.pace_budget(c, 200) == 100.0)
ident = tc.parse_pace_curve("0:0,100:100")
check("identity budget == elapsed", tc.pace_budget(ident, 37) == 37.0)

# The default curve: two thirds of clock rate to the 60% mark, then a ramp to the full quota. It sits
# strictly below the identity line everywhere in between, so switching to it can only tighten pacing.
d = tc.parse_pace_curve("")
check("default at the knee", tc.pace_budget(d, 60) == 40.0)
check("default before the knee", tc.pace_budget(d, 30) == 20.0)
check("default after the knee", tc.pace_budget(d, 80) == 70.0)  # 40 + (20/40)*(100-40)
check("default reaches the full quota", tc.pace_budget(d, 100) == 100.0)
check("default is below identity everywhere between", all(tc.pace_budget(d, e) < e for e in range(1, 100)))


# --- pace_curve: what the LIVE env resolves to -------------------------------------------------------
# The CLI rejects a malformed spec up front, so this is the guard for a path that got past it. It must
# resolve to the default rather than raising inside the pacer or unlocking spend.
def live(spec):
    if spec is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = spec
    return tc.pace_curve()


check("unset env -> default curve", live(None) == DEFAULT)
check("empty env -> default curve", live("") == DEFAULT)
check("malformed env -> default curve, not a raise", live("0:0,90:0,100:oops") == DEFAULT)
check("valid env is honoured", live("50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])
os.environ.pop("TAUCETI_PACE", None)


# --- the CLI validates the spec that will be USED ----------------------------------------------------
# $TAUCETI_PACE is linted for every subcommand. `work`/`_round` are the ones that REPLACE it, so a
# --pace they were given is what gets validated: a stale `export` in the operator's shell must not veto
# the flag documented to override it. Nothing else may claim that exemption.
_dispatched = []


def cli(argv, env):
    """Run `tauceti <argv>` as far as the pacing validation, with the commands it can dispatch to stubbed
    out so nothing after it runs. Returns None when it got through, or the message it died with."""
    was = os.environ.get("TAUCETI_PACE")
    if env is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = env
    _dispatched.clear()
    saved_work, saved_workers = tc.cli.cmd_work, tc.cli.cmd_workers
    # `workers add` writes the operator's real workers.toml, so stub it too: a regression here must not
    # reach the filesystem, it must show up as this check failing.
    tc.cli.cmd_work = lambda args, **_k: _dispatched.append(("work", getattr(args, "pace", None))) or 0
    tc.cli.cmd_workers = lambda args: _dispatched.append(("workers", getattr(args, "pace", None))) or 0
    try:
        tc.cli.main(argv)
        return None
    except tc.Die as e:
        return str(e)
    finally:
        tc.cli.cmd_work, tc.cli.cmd_workers = saved_work, saved_workers
        os.environ.pop("TAUCETI_PACE", None)
        if was is not None:
            os.environ["TAUCETI_PACE"] = was


def resolved(cmd, pace, env):
    """What resolve_pace leaves in the environment for the pacer to read live, or the message it died
    with. Validation and installation are one step, so this covers both."""
    was = os.environ.get("TAUCETI_PACE")
    if env is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = env
    try:
        tc.cli.resolve_pace(cmd, SimpleNamespace(pace=pace))
        return os.environ.get("TAUCETI_PACE")
    except tc.Die as e:
        return str(e)
    finally:
        os.environ.pop("TAUCETI_PACE", None)
        if was is not None:
            os.environ["TAUCETI_PACE"] = was


# The flag is not merely validated, it REPLACES the environment — the pacer reads $TAUCETI_PACE live, so
# a validated flag that never lands there would leave the run on the stale curve it was meant to override.
check("the flag is installed for the pacer to read", resolved("work", "50:70", None) == "50:70")
check("...over a malformed env", resolved("work", "50:70", "50:") == "50:70")
check("...and over a valid one", resolved("work", "50:70", "0:0,100:100") == "50:70")
check("no flag leaves the env alone", resolved("work", None, "0:0,100:100") == "0:0,100:100")
check("a command that cannot override does not install", resolved("workers", "50:70", None) is None)

check("a malformed env is rejected, naming itself", "$TAUCETI_PACE" in (cli(["work"], "50:") or ""))
check("a malformed flag is rejected, naming itself", "--pace" in (cli(["work", "--pace", "50:"], None) or ""))
check("a valid flag overrides a malformed env", cli(["work", "--pace", "0:0,100:100"], "50:") is None)
check("...and reaches the command that installs it", _dispatched == [("work", "0:0,100:100")])
check("a malformed flag is caught even under a valid env", "--pace" in (cli(["work", "--pace", "50:"], "50:70") or ""))
check("a valid env alone is fine", cli(["work"], "50:70") is None)
check("...as is neither", cli(["work"], None) is None)
# The hidden per-round entry point is the same command by another name.
check("_round overrides too", cli(["_round", "--pace", "0:0,100:100"], "50:") is None)
# `workers add --pace` records a curve for a worker launched LATER; it leaves this process's environment
# alone, so it cannot excuse a malformed one that a manager spawned here would inherit.
check(
    "workers add does not excuse the env it leaves in place",
    "$TAUCETI_PACE" in (cli(["workers", "add", "--pace", "0:0,100:100"], "50:") or ""),
)
check("...and nothing was dispatched", _dispatched == [])
# A subcommand with no --pace at all still gets the lint.
check("a parser without --pace still checks the env", "$TAUCETI_PACE" in (cli(["usage"], "50:") or ""))

# A worker's stored curve is validated where it is configured, so a typo is a config error rather than a
# worker that starts, rejects its own --pace, and crash-loops.
try:
    tc.WorkerSpec.from_dict({"id": "w1", "pace": "not-a-curve"}, 0)
    check("a malformed pace in workers.toml is a config error", False)
except tc.WorkersError as e:
    check("a malformed pace in workers.toml is a config error", "workers[0].pace" in str(e))
check("...and a valid one is kept", tc.WorkerSpec.from_dict({"id": "w1", "pace": "50:70"}, 0).pace == "50:70")


# --- _classify_window honours the live curve --------------------------------------------------------
def status(used, elapsed, pace):
    if pace is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = pace
    return tc._classify_window("session", used, elapsed, None, False).status


check("default: used 40 @ elapsed 50 -> over-pace", status(40, 50, None) == "over-pace")  # budget 33.3
check("default: used 30 @ elapsed 50 -> under-pace", status(30, 50, None) == "under-pace")
check("identity: used 60 @ elapsed 50 -> over-pace", status(60, 50, "0:0,100:100") == "over-pace")
check("identity: used 40 @ elapsed 50 -> under-pace", status(40, 50, "0:0,100:100") == "under-pace")
check("curve 50:70: used 60 @ elapsed 50 -> under-pace", status(60, 50, "50:70") == "under-pace")
check("curve 50:70: used 80 @ elapsed 50 -> over-pace", status(80, 50, "50:70") == "over-pace")
check("curve tail uncapped: used 95 @ elapsed 100 -> under-pace", status(95, 100, "90:90") == "under-pace")
check("exhausted beats the curve: used 100 @ elapsed 5 -> exhausted", status(100, 5, "0:100") == "exhausted")
os.environ["TAUCETI_PACE"] = "50:70"
w = tc._classify_window("session", 80, 50, None, False)
check("over-pace window records the budget it exceeded", w.status == "over-pace" and w.budget == 70.0)
os.environ.pop("TAUCETI_PACE", None)

# Garbage telemetry fails CLOSED (unknown), never fresh/under-pace — even under a permissive curve.
os.environ.pop("TAUCETI_PACE", None)

# --- positive headroom: under-pace means room for the NEXT request, not merely "not over" -----------
check("used < budget -> under-pace", status(49, 50, "0:50,100:50") == "under-pace")
check("used == budget -> at-budget (its own status)", status(50, 50, "0:50,100:50") == "at-budget")
check("used > budget -> over-pace", status(51, 50, "0:50,100:50") == "over-pace")
check("equality at zero is not special-cased", status(0, 0, "") == "at-budget")
check("equality at the tail too", status(90, 90, "0:0,90:90,100:90") == "at-budget")
check(
    "at-budget still records the budget it met", tc._classify_window("session", 50, 50, None, False).budget is not None
)

# --- τ₀: how long a fresh window stays completely unspendable under a curve -------------------------
# This is what decides whether a just-reset window may be INITIALIZED at all: with τ₀ > 0 the operator's
# curve says a window this fresh has no budget, and an unopened window has no clock to wait it out on.
for spec, want in (
    ("", 0.0),
    ("0:0,100:100", 0.0),
    ("0:0,100:95", 0.0),
    ("0:10,100:100", 0.0),
    ("0:0,90:0,100:95", 90.0),
    ("0:0,50:0,100:0", 100.0),
):
    got = tc.pace_zero_plateau(tc.parse_pace_curve(spec))
    check(f"tau0({spec or 'default'}) == {want}", got == want)

# --- pace_recovery: when a pace-blocked window's budget catches up with its usage -------------------
# A window held by the pace line is not waiting for its reset: its usage is fixed while it waits and the
# budget climbs. This is the stretch (start, end) over which the budget stands above that usage — start
# being the EQUALITY point, which is still a block, so callers must wait strictly inside the stretch.
D = tc.parse_pace_curve("")  # the default 60:40
check("default: used 30 @ elapsed 20 -> from 45 to the end", tc.pace_recovery(D, 30, 20) == (45.0, 100.0))
check("default: used 40 @ elapsed 10 -> the knee", tc.pace_recovery(D, 40, 10) == (60.0, 100.0))
check("default: used 70 @ elapsed 10 -> past the knee", tc.pace_recovery(D, 70, 10) == (80.0, 100.0))
check("already under pace -> now", tc.pace_recovery(D, 30, 50) == (50.0, 100.0))
check("elapsed clamps below 0", tc.pace_recovery(D, 30, -10) == (45.0, 100.0))
check("used 0 is free from the instant the budget lifts", tc.pace_recovery(D, 0, 0) == (0.0, 100.0))
check("elapsed past 100 clamps", tc.pace_recovery(D, 30, 150) == (100.0, 100.0))
flat = tc.parse_pace_curve("0:50,100:50")
check("flat curve never reaches -> None", tc.pace_recovery(flat, 60, 10) is None)
check("flat curve, equality is not recovery -> None", tc.pace_recovery(flat, 50, 10) is None)
falling = tc.parse_pace_curve("0:60,100:40")
check("falling curve below usage -> None", tc.pace_recovery(falling, 70, 10) is None)
check("falling curve still above usage -> until it drops through", tc.pace_recovery(falling, 50, 10) == (10.0, 50.0))
check("a budget over 100 is simply always above usage", tc.pace_recovery(tc.parse_pace_curve("0:250"), 99, 5)[0] == 5.0)
# A flat stretch exactly AT current usage must not read as recovery, but the rise after it must.
plateau = tc.parse_pace_curve("0:0,50:30,60:30,100:100")
check("flat stretch at usage -> the rise after it", tc.pace_recovery(plateau, 30, 55) == (60.0, 100.0))
check("the identity line meets usage at usage", tc.pace_recovery(ident, 37, 10) == (37.0, 100.0))
# Rise, fall, rise: a curve may hand budget back briefly and take it away again. Both stretches count,
# and the one reported is the one we are about to live through.
hump = tc.parse_pace_curve("0:0,20:60,40:0,60:0,100:100")
check("the near stretch is reported", tc.pace_recovery(hump, 30, 5) == (10.0, 30.0))
check("...and after it closes, the next one", tc.pace_recovery(hump, 30, 35) == (72.0, 100.0))


# --- _pace_free_at / _next_eligible: the wake-up instant ---------------------------------------------
# The whole point: a window blocked by the CURVE reports when the curve frees it, not when the provider
# does. Waiting for the reset parks a worker that has quota in hand. NOW is fixed so the arithmetic is
# exact rather than racing the clock.
NOW = 1_700_000_000.0
HOUR = 3600


def win(status, used, elapsed, resets_in, name="session"):
    return tc.Window(name, used, elapsed, NOW + resets_in, status, None)


def free_at(w):
    return tc._pace_free_at(w, NOW)


os.environ.pop("TAUCETI_PACE", None)
# 5h window, 20% elapsed (4h left), used 30% under the default: the budget reaches 30% at 45% elapsed,
# i.e. 25% of a 5-hour window = 1h15m away, plus the ease.
check(
    "over-pace frees when the budget catches up", free_at(win("over-pace", 30, 20, 4 * HOUR)) == NOW + 1.25 * HOUR + 180
)
check("...well before the reset it used to wait for", free_at(win("over-pace", 30, 20, 4 * HOUR)) < NOW + 4 * HOUR)
check(
    "at-budget frees at the ease past the equality",
    free_at(win("at-budget", 20, 30, 3.5 * HOUR)) == NOW + tc.PACE_EASE_S,
)
# The ease is bounded by the stretch it eases into, and the stretch ends with the window, so a wake can
# never land past the reset — right at the end of a window it is the ease that shrinks.
check("never later than the reset", free_at(win("over-pace", 99, 99.9, 5)) <= NOW + 5)
check("...by shortening the ease, not by clamping", free_at(win("over-pace", 99, 99.9, 5)) == NOW + 2.5)
check("a window with no clock cannot be solved", free_at(tc.Window("session", 30, 20, None, "over-pace")) is None)
check("nor one already rolling over", free_at(win("over-pace", 30, 20, -1)) is None)

# The ease must not step OVER a brief stretch of budget: half of a narrow one, the full ease of a wide
# one. Here the budget is above 49.9% for a tenth of a percent of the window — about 18 seconds.
os.environ["TAUCETI_PACE"] = "0:0,50:50,50.2:0,100:0"
brief = free_at(win("over-pace", 49.9, 49.8, 2.5 * HOUR))
start, end = tc.pace_recovery(tc.pace_curve(), 49.9, 49.8)
per_pct = 2.5 * HOUR / (100 - 49.8)
check("the wake lands inside a brief stretch", NOW + (start - 49.8) * per_pct < brief < NOW + (end - 49.8) * per_pct)
check(
    "...and the budget there is genuinely above the usage",
    tc.pace_budget(tc.pace_curve(), 49.8 + (brief - NOW) / per_pct) > 49.9,
)

os.environ["TAUCETI_PACE"] = "0:50,100:50"
check(
    "a curve that never catches up falls back to the reset",
    tc.Quota._next_eligible([win("over-pace", 60, 20, 4 * HOUR)]) == NOW + 4 * HOUR,
)
os.environ.pop("TAUCETI_PACE", None)


# _next_eligible reads the same as _pace_free_at, over whichever windows are actually blocking. It calls
# time.time() itself, so rebase the fixtures onto the live clock and compare in seconds from now.
def eligible_in(windows):
    now = time.time()
    live = [
        tc.Window(w.name, w.used, w.elapsed, None if w.resets_at is None else now + (w.resets_at - NOW), w.status)
        for w in windows
    ]
    got = tc.Quota._next_eligible(live)
    return None if got is None else got - now


check(
    "it reports the paced instant, not the reset",
    abs(eligible_in([win("over-pace", 30, 20, 4 * HOUR)]) - (1.25 * HOUR + tc.PACE_EASE_S)) < 1,
)
check(
    "an exhausted window still waits for its reset",
    abs(eligible_in([win("exhausted", 100, 20, 4 * HOUR)]) - 4 * HOUR) < 1,
)
both = [win("over-pace", 30, 20, 4 * HOUR), win("at-budget", 20, 30, 3.5 * HOUR, "weekly")]
check("with several blockers, the earliest one", abs(eligible_in(both) - tc.PACE_EASE_S) < 1)
check(
    "a window with no clock contributes nothing",
    eligible_in([tc.Window("session", 30, 20, None, "over-pace")]) is None,
)
check("under-pace windows are not blocking at all", eligible_in([win("under-pace", 5, 50, HOUR)]) is None)
check("no windows -> None", tc.Quota._next_eligible([]) is None)

os.environ["TAUCETI_PACE"] = "0:100"
check("NaN elapsed -> unknown", tc._classify_window("session", 50, float("nan"), None, False).status == "unknown")
check("inf used -> unknown", tc._classify_window("session", float("inf"), 50, None, False).status == "unknown")
check("bool used -> unknown", tc._classify_window("session", True, 50, None, False).status == "unknown")
check(
    "limit_reached still exhausts despite NaN elapsed",
    tc._classify_window("session", 5, float("nan"), None, True).status == "exhausted",
)
os.environ.pop("TAUCETI_PACE", None)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

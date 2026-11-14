#!/usr/bin/env python3
"""$TAUCETI_PACE_CLAUDE / $TAUCETI_PACE_CODEX (--pace-claude / --pace-codex, `pace_claude` / `pace_codex`
in workers.toml) give one provider's windows a curve of their own; a provider without one paces on
$TAUCETI_PACE. Verify which curve each provider's reader classifies and schedules against, that the
flags and variables are validated and installed like --pace, and the workers.toml spelling.
Dependency-free; no network."""

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker.claude_api import BudgetError, configure_api
from tauceti_worker.cli import build_parser

fails = 0
VARS = ("TAUCETI_PACE", "TAUCETI_PACE_CLAUDE", "TAUCETI_PACE_CODEX")


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    fails += not cond


def env(pace=None, claude=None, codex=None):
    for var, spec in zip(VARS, (pace, claude, codex), strict=True):
        if spec is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = spec


FLAT = [(0.0, 50.0), (100.0, 50.0)]
OPEN = [(0.0, 100.0), (100.0, 100.0)]
DEFAULT = [(0.0, 0.0), (60.0, 40.0), (100.0, 100.0)]

# --- pace_curve: which spec a provider resolves to --------------------------------------------------
env()
check("nothing set: every provider gets the default", tc.pace_curve("claude") == tc.pace_curve("codex") == DEFAULT)
env(pace="0:50,100:50")
check("only $TAUCETI_PACE: every provider gets it", tc.pace_curve("claude") == tc.pace_curve("codex") == FLAT)
env(pace="0:50,100:50", codex="0:100")
check("a provider's own curve is the one it gets", tc.pace_curve("codex") == OPEN)
check("...and the other provider keeps $TAUCETI_PACE", tc.pace_curve("claude") == FLAT)
check("...as does a caller that names no provider", tc.pace_curve() == FLAT)
env(codex="0:100")
check("without $TAUCETI_PACE the other provider gets the default", tc.pace_curve("claude") == DEFAULT)
env(pace="0:50,100:50", claude="  ", codex="")
check("a blank own curve is unset", tc.pace_curve("claude") == tc.pace_curve("codex") == FLAT)

# --- each reader classifies against its own provider's curve ----------------------------------------
q = tc.Quota.__new__(tc.Quota)
WEEK = 604800


def codex(used):
    """A weekly Codex window at 50% elapsed."""
    window = {"used_percent": used, "limit_window_seconds": WEEK, "reset_after_seconds": WEEK // 2}
    return q._codex_from_payload({"rate_limit": {"limit_reached": False, "primary_window": window}})


def iso(delta_s):
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


def claude(used):
    """Claude with both windows at 50% elapsed and the same usage."""
    return q._claude_from_payload(
        {
            "five_hour": {"utilization": used, "resets_at": iso(2.5 * 3600)},
            "seven_day": {"utilization": used, "resets_at": iso(3.5 * 86400)},
        }
    )


env(pace="0:50,100:50", codex="0:100")
check("codex at 80% is under its own open curve", codex(80).available)
check("...with the budget of that curve", codex(80).windows[0].budget == 100.0)
check("claude at 80% is over $TAUCETI_PACE", [w.status for w in claude(80).windows] == ["over-pace"] * 2)
check("claude at 40% is under it", claude(40).available)
env(pace="0:100", claude="0:50,100:50")
check("the other way round: claude at 80% is over its own curve", not claude(80).available)
check("...and codex at 80% is under $TAUCETI_PACE", codex(80).available)

# --- the wake-up follows the same curve as the block ------------------------------------------------
# Used 40% at 50% elapsed of a weekly window. Under 0:0,100:100 that is under pace; under 60:40 the
# budget is 33.3% and reaches 40% at 60% elapsed, a tenth of the window away.
env(pace="0:0,100:100", codex="60:40")
p = codex(40)
check("a provider held by its own curve is not available", [w.status for w in p.windows] == ["over-pace"])
wake = p.next_eligible - time.time()
check("...and wakes where that curve frees it", abs(wake - (WEEK / 10 + tc.PACE_EASE_S)) < 5)
env(pace="0:50,100:50", codex="60:40")
wake = codex(40).next_eligible - time.time()
check("...whatever $TAUCETI_PACE says about the same window", abs(wake - (WEEK / 10 + tc.PACE_EASE_S)) < 5)

# --- an idle Claude window reads Claude's curve -----------------------------------------------------
IDLE = {"five_hour": None, "seven_day": {"utilization": 10, "resets_at": iso(3.5 * 86400)}}
PLATEAU = "0:0,90:0,100:95"
env(pace="0:100", claude=PLATEAU)
check("claude's zero plateau blocks its idle window", q._claude_from_payload(IDLE).windows[0].status == "idle-blocked")
env(pace="0:100", codex=PLATEAU)
check("codex's zero plateau does not", q._claude_from_payload(IDLE).windows[0].status == tc.STATE_IDLE)


# --- the CLI settles each flag against its own variable ---------------------------------------------
def resolved(cmd, **flags):
    """What resolve_pace leaves in the three variables, or the message it died with."""
    args = SimpleNamespace(**{"pace": None, "pace_claude": None, "pace_codex": None, **flags})
    try:
        tc.cli.resolve_pace(cmd, args)
        return tuple(os.environ.get(var) for var in VARS)
    except tc.Die as e:
        return str(e)


env()
check("a provider flag lands in its own variable", resolved("work", pace_codex="0:100") == (None, None, "0:100"))
env(pace="50:70", claude="0:0,100:100")
check(
    "...beside --pace, replacing only what it names",
    resolved("_round", pace="0:10", pace_claude="0:100") == ("0:10", "0:100", None),
)
env(codex="50:")
check("a malformed variable is rejected, naming itself", "$TAUCETI_PACE_CODEX" in resolved("work"))
check("...for a command that does not pace too", "$TAUCETI_PACE_CODEX" in resolved("usage"))
check("...and --pace does not excuse it", "$TAUCETI_PACE_CODEX" in resolved("work", pace="0:100"))
check("its own flag overrides it", resolved("work", pace_codex="0:100") == (None, None, "0:100"))
env()
check("a malformed flag is rejected, naming itself", "--pace-claude" in resolved("work", pace_claude="50:"))
check(
    "...before any flag is installed",
    "--pace-codex" in resolved("work", pace="0:100", pace_codex="x") and "TAUCETI_PACE" not in os.environ,
)
check("a command that cannot override does not install", resolved("workers", pace_codex="0:100") == (None,) * 3)
env(pace="0:50,100:50", claude="0:100")
check("a blank flag returns the provider to --pace", resolved("work", pace_claude="")[1] == "")
check("...in the pacer's reading as well", tc.pace_curve("claude") == FLAT)
env()

# --- workers.toml ------------------------------------------------------------------------------------
for key in ("pace_claude", "pace_codex"):
    try:
        tc.WorkerSpec.from_dict({"id": "w1", key: "not-a-curve"}, 0)
        check(f"a malformed {key} is a config error", False)
    except tc.WorkersError as e:
        check(f"a malformed {key} is a config error", f"workers[0].{key}" in str(e))
spec = tc.WorkerSpec.from_dict({"id": "w1", "pace": "50:70", "pace_claude": "0:100", "pace_codex": "0:0,100:100"}, 0)
parsed = build_parser().parse_args(spec.work_argv()[3:])
check(
    "the keys reach the worker as its flags",
    (parsed.pace, parsed.pace_claude, parsed.pace_codex) == ("50:70", "0:100", "0:0,100:100"),
)
check("...and survive a round trip through the file's form", tc.WorkerSpec.from_dict(spec.as_dict(), 0) == spec)
plain = tc.WorkerSpec.from_dict({"id": "w1"}, 0)
check("a worker without them passes neither flag", not any(a.startswith("--pace") for a in plain.work_argv()))
check(
    "a provider curve changes the fingerprint",
    tc.WorkerSpec.from_dict({"id": "w1", "pace_codex": "0:100"}, 0).fingerprint() != plain.fingerprint(),
)
try:
    tc.WorkerSpec.from_dict({"id": "w1", "agent": "claude", "claude_billing": "api", "pace_claude": "0:100"}, 0)
    check("an API-billed worker takes no provider curve", False)
except tc.WorkersError:
    check("an API-billed worker takes no provider curve", True)

for flag in ("--pace-claude", "--pace-codex"):
    args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", flag, "0:100"])
    try:
        configure_api(args, "claude")
        check(f"an API-billed run rejects {flag}", False)
    except BudgetError as e:
        check(f"an API-billed run rejects {flag}", "subscription pacing" in str(e))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""The Claude usage reader must preserve the raw epistemic state of EACH quota window before pacing.

Anthropic reports two gating windows (a 5-hour session and an unscoped weekly) in two overlapping
representations: a structured `limits` array and the legacy flat `five_hour` / `seven_day` keys. Each
window can be:

  active     a finite usage% AND a valid, plausible, unelapsed reset clock — the only state that paces;
  idle       a recognized post-reset gap (the window rolled, nothing has opened the next cycle);
  absent     no record for that window anywhere in the payload;
  malformed  a record that exists but is contradictory / non-finite / invalid / unrecognized.

The properties under test are architectural, not cosmetic:
  - the two windows reset on independent clocks, so NEITHER may be inferred from the other;
  - the `limits` array is AUTHORITATIVE per window: a present-but-broken limits record fails closed
    even when the legacy flat field still looks healthy;
  - `absent` and `malformed` fail closed — schema drift must never silently delete a hard quota
    constraint by dropping a window from the gating set;
  - an explicit null, an omitted field and an INVALID value are three different statements;
  - a reset clock must be PLAUSIBLE, not merely parseable: one further away than the window is long
    describes no window we know about;
  - `under-pace` means POSITIVE HEADROOM. Sitting exactly on the budget is its own status, because
    the request we are deciding to start costs something;
  - the diagnosis names the window and the offending condition, not "usage unknown".

Timestamps here are generated relative to now, deliberately: a sentinel far-future date is not a valid
reading of a 5-hour window, and the parser now says so.

Dependency-free; no network.
"""

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


# _claude_from_payload is the pure parse: no cache, no reservation, no bootstrap. It reaches self only
# for _next_eligible (a staticmethod), so a bare instance is enough.
q = tc.Quota.__new__(tc.Quota)

# This file is about reading the payload, not about the curve, so pin the identity line (budget ==
# elapsed%) and keep the arithmetic in the expectations obvious. The shipped default is 60:40; what it
# does to classification is pace_curve.py's business.
IDENTITY = "0:0,100:100"
os.environ["TAUCETI_PACE"] = IDENTITY


def iso(delta_s: float) -> str:
    """An ISO reset clock delta_s seconds from now (negative = already elapsed)."""
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


# A live session window with 4h of its 5h left ⇒ 20% elapsed ⇒ 20% budget under the identity curve.
SESSION_LIVE = iso(4 * 3600)
# A live weekly with 6 of its 7 days left ⇒ ~14% elapsed.
WEEKLY_LIVE = iso(6 * 86400)


def state(payload, window):
    return tc._claude_window_reading(payload, window).state


def live_weekly(used=0):
    return {"utilization": used, "resets_at": WEEKLY_LIVE}


# --- the per-record truth table -------------------------------------------------------------------
# usage x reset, over {a usable value, an explicit null, an omitted field, garbage}. `idle` requires an
# EXPLICIT statement that the window is not open; silence (both fields omitted) is unrecognized, not
# idle, so drift can't turn a hard constraint into a free pass.
table = [
    ("value+clock ⇒ active", {"utilization": 10, "resets_at": SESSION_LIVE}, tc.STATE_ACTIVE),
    ("value+null clock, 0%% ⇒ idle", {"utilization": 0, "resets_at": None}, tc.STATE_IDLE),
    ("value+no clock field, 0%% ⇒ idle", {"utilization": 0}, tc.STATE_IDLE),
    ("value+null clock, 80%% ⇒ malformed", {"utilization": 80, "resets_at": None}, tc.STATE_MALFORMED),
    ("value+INVALID clock ⇒ malformed", {"utilization": 10, "resets_at": "not-a-date"}, tc.STATE_MALFORMED),
    ("value+numeric clock ⇒ malformed", {"utilization": 10, "resets_at": 1893456000}, tc.STATE_MALFORMED),
    ("null usage+null clock ⇒ idle", {"utilization": None, "resets_at": None}, tc.STATE_IDLE),
    ("null usage+no clock field ⇒ idle", {"utilization": None}, tc.STATE_IDLE),
    ("no usage field+null clock ⇒ idle", {"resets_at": None}, tc.STATE_IDLE),
    ("null usage+live clock ⇒ malformed", {"utilization": None, "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("no usage field+live clock ⇒ malformed", {"resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("nothing stated at all ⇒ malformed", {}, tc.STATE_MALFORMED),
    ("NaN usage ⇒ malformed", {"utilization": float("nan"), "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("inf usage ⇒ malformed", {"utilization": float("inf"), "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("string usage ⇒ malformed", {"utilization": "10", "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("bool usage ⇒ malformed", {"utilization": True, "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("negative usage ⇒ malformed", {"utilization": -1, "resets_at": SESSION_LIVE}, tc.STATE_MALFORMED),
    ("elapsed clock ⇒ idle (window rolled)", {"utilization": 90, "resets_at": iso(-600)}, tc.STATE_IDLE),
    ("clock about to roll ⇒ still active", {"utilization": 90, "resets_at": iso(5)}, tc.STATE_ACTIVE),
]
for name, record, want in table:
    check(name, state({"five_hour": record}, "session"), want)

# The record itself, before its fields: an explicit null IS a record ("this window is not open"); a
# missing key is the payload not mentioning the window at all; a non-object is garbage.
check("explicit null record ⇒ idle", state({"five_hour": None}, "session"), tc.STATE_IDLE)
check("key absent ⇒ absent", state({"seven_day": None}, "session"), tc.STATE_ABSENT)
check("non-object record ⇒ malformed", state({"five_hour": 3}, "session"), tc.STATE_MALFORMED)
check("list record ⇒ malformed", state({"five_hour": []}, "session"), tc.STATE_MALFORMED)

# A null reset timestamp and an INVALID one must not land in the same place.
check(
    "null vs invalid reset timestamps stay distinguishable",
    (
        state({"five_hour": {"utilization": None, "resets_at": None}}, "session"),
        state({"five_hour": {"utilization": None, "resets_at": "2026-13-45"}}, "session"),
    ),
    (tc.STATE_IDLE, tc.STATE_MALFORMED),
)

# --- a reset clock must be PLAUSIBLE for its window, not merely parseable -------------------------
# Parsing is not validation. A clock further out than the window is long cannot describe this window;
# clamping it to "0% elapsed" would pace the window as if it had just opened, on a figure we know is
# wrong. The two windows have different horizons, and each is judged against its own.
plausibility = [
    ("session 4h out ⇒ active", "session", "five_hour", 4 * 3600, tc.STATE_ACTIVE),
    ("session just inside 5h ⇒ active", "session", "five_hour", 5 * 3600 - 60, tc.STATE_ACTIVE),
    ("session 10h out ⇒ malformed", "session", "five_hour", 10 * 3600, tc.STATE_MALFORMED),
    ("session a week out ⇒ malformed", "session", "five_hour", 7 * 86400, tc.STATE_MALFORMED),
    ("weekly 6d out ⇒ active", "weekly", "seven_day", 6 * 86400, tc.STATE_ACTIVE),
    ("weekly just inside 7d ⇒ active", "weekly", "seven_day", 7 * 86400 - 600, tc.STATE_ACTIVE),
    ("weekly 10d out ⇒ malformed", "weekly", "seven_day", 10 * 86400, tc.STATE_MALFORMED),
]
for name, window, key, delta, want in plausibility:
    check(name, state({key: {"utilization": 10, "resets_at": iso(delta)}}, window), want)

check(
    "the year 2099 is not a valid 5-hour window",
    state({"five_hour": {"utilization": 10, "resets_at": "2099-01-01T00:00:00Z"}}, "session"),
    tc.STATE_MALFORMED,
)
p = q._claude_from_payload(
    {"five_hour": {"utilization": 10, "resets_at": "2099-01-01T00:00:00Z"}, "seven_day": live_weekly()}
)
check("an implausible clock fails closed, and says so", tc._unavail_reason(p)[0], False)
check("...naming the condition", "implausible" in tc._unavail_reason(p)[1], True)

# --- positive headroom, not "not over budget" ------------------------------------------------------
# `under-pace` must mean there is room for the request we are about to start. Exactly ON the budget is
# a soft pacing block. A flat curve makes the equality exact and independent of wall-clock drift.
os.environ["TAUCETI_PACE"] = "0:50,100:50"
at = {
    "five_hour": {"utilization": 50, "resets_at": SESSION_LIVE},
    "seven_day": {"utilization": 10, "resets_at": WEEKLY_LIVE},
}
p = q._claude_from_payload(at)
check("used == budget ⇒ at-budget, not under-pace", [w.status for w in p.windows], ["at-budget", "under-pace"])
check("used == budget ⇒ no task", (p.available, p.model), (False, None))
check("used == budget is a SOFT pacing block", tc._unavail_reason(p)[0], True)
check(
    "...and reports the equality",
    tc._unavail_reason(p)[1],
    "session at budget (20% elapsed: used 50% = 50% pace budget), 50% left",
)
under = {**at, "five_hour": {"utilization": 49.5, "resets_at": SESSION_LIVE}}
check("used < budget ⇒ available", q._claude_from_payload(under).available, True)
over = {**at, "five_hour": {"utilization": 50.5, "resets_at": SESSION_LIVE}}
check(
    "used > budget ⇒ over-pace, soft",
    (q._claude_from_payload(over).windows[0].status, tc._unavail_reason(q._claude_from_payload(over))[0]),
    ("over-pace", True),
)
os.environ["TAUCETI_PACE"] = IDENTITY

# --- the two windows are read independently -------------------------------------------------------
# A session reset must not change how the weekly reads, and vice versa: they roll on separate clocks,
# so keying either window's legitimacy on its sibling re-breaks every time the sibling rolls.
p = q._claude_from_payload({"five_hour": None, "seven_day": {"utilization": 20, "resets_at": WEEKLY_LIVE}})
check(
    "session reset, weekly active: states",
    [(w.name, w.status) for w in p.windows],
    [("session", tc.STATE_IDLE), ("weekly", "over-pace")],
)
check("session reset, weekly active: not available", p.available, False)

p = q._claude_from_payload({"five_hour": {"utilization": 0, "resets_at": SESSION_LIVE}, "seven_day": None})
check(
    "weekly reset, session active: states",
    [(w.name, w.status) for w in p.windows],
    [("session", "under-pace"), ("weekly", tc.STATE_IDLE)],
)
check("weekly reset, session active: not available", p.available, False)

# An idle window NEVER grants availability, even when the sibling is comfortably under pace: missing
# telemetry is not permission to spend.
p = q._claude_from_payload({"five_hour": None, "seven_day": live_weekly()})
check("idle session + healthy weekly ⇒ still blocked", (p.available, p.model), (False, None))
check("idle session stays in the gating set", len(p.windows), 2)

# --- source precedence: `limits` is authoritative per window ---------------------------------------
# A present limits record decides that window's state, including idle and malformed. The legacy flat
# key is a FALLBACK for a window limits does not mention — never a second opinion that can overrule it.
mal_limits = {
    "limits": [{"group": "session", "percent": "garbage", "resets_at": SESSION_LIVE}],
    "five_hour": {"utilization": 1, "resets_at": SESSION_LIVE},  # healthy-looking, and irrelevant
    "seven_day": live_weekly(),
}
p = q._claude_from_payload(mal_limits)
check("malformed limits record + active flat ⇒ malformed", p.windows[0].status, tc.STATE_MALFORMED)
check("malformed limits record + active flat ⇒ never available", p.available, False)
check("...and the diagnosis names the limits array", "(limits)" in tc._unavail_reason(p)[1], True)

idle_limits = {
    "limits": [{"group": "session", "percent": None, "resets_at": None}],
    "five_hour": {"utilization": 1, "resets_at": SESSION_LIVE},
    "seven_day": live_weekly(),
}
p = q._claude_from_payload(idle_limits)
check("idle limits record + active flat ⇒ idle", p.windows[0].status, tc.STATE_IDLE)
check("idle limits record + active flat ⇒ never available", p.available, False)

disagree = {
    "limits": [
        {"group": "session", "percent": 1, "resets_at": SESSION_LIVE},
        {"group": "weekly", "percent": 1, "resets_at": WEEKLY_LIVE},
    ],
    "five_hour": {"utilization": 100, "resets_at": SESSION_LIVE},  # disagrees; limits wins
    "seven_day": {"utilization": 100, "resets_at": WEEKLY_LIVE},
}
p = q._claude_from_payload(disagree)
check("limits active + flat exhausted ⇒ limits decides", [w.used for w in p.windows], [1.0, 1.0])
check("limits active + flat exhausted ⇒ available", p.available, True)

# Fallback is per window: limits carrying only one window leaves the OTHER to the flat key.
only_weekly = {
    "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 30, "resets_at": WEEKLY_LIVE, "scope": None}],
    "five_hour": {"utilization": 4, "resets_at": SESSION_LIVE},
}
p = q._claude_from_payload(only_weekly)
check(
    "limits only weekly + flat session ⇒ both read",
    [(w.name, w.status) for w in p.windows],
    [("session", "under-pace"), ("weekly", "over-pace")],
)
check("limits only weekly + flat session ⇒ both usages parsed", [w.used for w in p.windows], [4.0, 30.0])
check(
    "limits only weekly + flat session ⇒ sources",
    [tc._claude_window_reading(only_weekly, w).source for w in ("session", "weekly")],
    ["five_hour", "limits"],
)

only_session = {
    "limits": [{"group": "session", "percent": 5, "resets_at": SESSION_LIVE}],
    "seven_day": {"utilization": 7, "resets_at": WEEKLY_LIVE},
}
p = q._claude_from_payload(only_session)
check("limits only session + flat weekly ⇒ both usages parsed", [w.used for w in p.windows], [5.0, 7.0])
check(
    "limits only session + flat weekly ⇒ sources",
    [tc._claude_window_reading(only_session, w).source for w in ("session", "weekly")],
    ["limits", "seven_day"],
)

# A window missing from BOTH representations is absent — fail closed, never dropped.
p = q._claude_from_payload({"limits": [{"group": "session", "percent": 5, "resets_at": SESSION_LIVE}]})
check(
    "weekly in neither representation ⇒ absent, blocked", (p.windows[1].status, p.available), (tc.STATE_ABSENT, False)
)
check("absent weekly names itself", tc._unavail_reason(p), (False, "weekly limit missing from usage response"))

# --- gating: the scoped weekly caps never gate, both real windows always do ------------------------
full = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 0, "resets_at": SESSION_LIVE, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 0, "resets_at": WEEKLY_LIVE, "scope": None},
        # a per-model weekly cap, maxed out — must be ignored, never gate opus:
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 99,
            "resets_at": None,
            "scope": {"model": {"display_name": "Fable"}},
        },
    ],
    "five_hour": None,
    "seven_day": None,
    "seven_day_sonnet": None,
}
p = q._claude_from_payload(full)
check("limits: exactly two gating windows (no sonnet/scoped)", [w.name for w in p.windows], ["session", "weekly"])
check("limits: both under pace ⇒ available opus", (p.available, p.model), (True, "opus"))
check("limits: a maxed per-model (scoped) weekly does NOT block opus", p.available, True)

spent = {
    **full,
    "limits": [
        {"group": "session", "percent": 0, "resets_at": SESSION_LIVE, "scope": None},
        {"group": "weekly", "percent": 100, "resets_at": WEEKLY_LIVE, "scope": None},
    ],
}
p = q._claude_from_payload(spent)
check(
    "weekly at 100%% ⇒ exhausted ⇒ unavailable",
    (p.available, tc._unavail_reason(p)),
    (False, (False, "weekly exhausted")),
)

flat = {
    "five_hour": {"utilization": 0, "resets_at": SESSION_LIVE},
    "seven_day": {"utilization": 0, "resets_at": WEEKLY_LIVE},
    "seven_day_sonnet": None,  # present-but-null: must be ignored, not a third window
}
p = q._claude_from_payload(flat)
check("flat: two windows, no sonnet", [w.name for w in p.windows], ["session", "weekly"])
check("flat: available opus", (p.available, p.model), (True, "opus"))
check("empty limits array ⇒ flat still read", q._claude_from_payload({"limits": [], **flat}).available, True)
check("non-list limits ⇒ flat still read", q._claude_from_payload({"limits": {"x": 1}, **flat}).available, True)

p = q._claude_from_payload(flat)
check("active pair reports elapsed", [round(w.elapsed) for w in p.windows], [20, 14])

over = {
    "five_hour": {"utilization": 80, "resets_at": SESSION_LIVE},
    "seven_day": {"utilization": 1, "resets_at": WEEKLY_LIVE},
}
soft, why = tc._unavail_reason(q._claude_from_payload(over))
check("over the pace line ⇒ SOFT block", soft, True)
check(
    "over-pace reason keeps its detail",
    why,
    "session ahead of pace (20% elapsed: used 80% > 20% pace budget), 20% left",
)

# --- the top-level response must be an object ------------------------------------------------------
# .get() on a list/string/number would raise inside the pacer, and an exception is not a quota verdict.
for bad in ([], ["five_hour"], "nope", 42, True, None):
    problem = tc._claude_payload_problem(bad)
    check(f"non-object payload {bad!r} is named, not raised", bool(problem), True)
    p = q._claude_from_payload(bad)  # must not raise
    check(f"non-object payload {bad!r} ⇒ not available", p.available, False)
check("a real object passes the type check", tc._claude_payload_problem(flat), None)

# --- diagnosis: name the condition, never a bare "usage unknown" -----------------------------------
diagnoses = [
    ("session window reset; awaiting initialization", {"five_hour": None, "seven_day": live_weekly()}),
    ("weekly limit missing from usage response", {"five_hour": {"utilization": 0, "resets_at": SESSION_LIVE}}),
    (
        "session reset timestamp invalid (five_hour)",
        {"five_hour": {"utilization": 5, "resets_at": "nope"}, "seven_day": live_weekly()},
    ),
    (
        "session usage figure is not a usable percentage (five_hour)",
        {"five_hour": {"utilization": float("nan"), "resets_at": SESSION_LIVE}, "seven_day": live_weekly()},
    ),
    (
        "session usage figure missing with a live reset clock (five_hour)",
        {"five_hour": {"resets_at": SESSION_LIVE}, "seven_day": live_weekly()},
    ),
    (
        "session usage 80% reported with no reset clock (five_hour)",
        {"five_hour": {"utilization": 80, "resets_at": None}, "seven_day": live_weekly()},
    ),
    ("session exhausted", {"five_hour": {"utilization": 100, "resets_at": SESSION_LIVE}, "seven_day": live_weekly()}),
]
for want, payload in diagnoses:
    check(f"diagnosis: {want}", tc._unavail_reason(q._claude_from_payload(payload)), (False, want))

# Every one of those is a HARD block: --ignore-quota overrides pacing, never availability.
for want, payload in diagnoses:
    verdict = tc._ignore_quota_verdict(None, q._claude_from_payload(payload))
    check(f"hard block ⇒ --ignore-quota waits ({want[:30]})", verdict, "wait")

# Both windows unreadable at once still reports both, and says the response itself is unusable.
p = q._claude_from_payload({})
check(
    "no recognizable record at all ⇒ schema error",
    (p.available, p.error),
    (False, "claude usage response schema unsupported (no session or weekly record)"),
)
check(
    "no recognizable record at all ⇒ both windows named",
    tc._unavail_reason(p),
    (False, "session limit missing from usage response; weekly limit missing from usage response"),
)

# A hard block co-occurring with an over-pace sibling stays HARD (we cannot tell the unreadable window
# is not exhausted), and the unreadable window is what gets reported.
p = q._claude_from_payload(
    {"five_hour": {"utilization": 60, "resets_at": SESSION_LIVE}, "seven_day": {"utilization": 5, "resets_at": "nope"}}
)
soft, why = tc._unavail_reason(p)
check("unreadable dominates a co-occurring over-pace window", soft, False)
check("unreadable window is the reported reason", why, "weekly reset timestamp invalid (seven_day)")

# --- schema drift can never delete a hard quota constraint -----------------------------------------
healthy = {
    "five_hour": {"utilization": 0, "resets_at": SESSION_LIVE},
    "seven_day": {"utilization": 0, "resets_at": WEEKLY_LIVE},
}
check("baseline healthy payload is available", q._claude_from_payload(healthy).available, True)
for drop in ("five_hour", "seven_day"):
    p = q._claude_from_payload({k: v for k, v in healthy.items() if k != drop})
    check(f"dropping {drop} ⇒ blocked (constraint kept, not deleted)", (p.available, len(p.windows)), (False, 2))
for renamed in ("five_hour_v2", "session"):
    p = q._claude_from_payload({renamed: healthy["five_hour"], "seven_day": healthy["seven_day"]})
    check(f"renaming five_hour→{renamed} ⇒ blocked", p.available, False)
for key in ("utilization", "resets_at"):
    degraded = {**healthy, "five_hour": {k: v for k, v in healthy["five_hour"].items() if k != key}}
    check(f"dropping five_hour.{key} ⇒ never available", q._claude_from_payload(degraded).available, False)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

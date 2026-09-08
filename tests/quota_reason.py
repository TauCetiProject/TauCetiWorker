#!/usr/bin/env python3
"""quota_line must tell the truth about *why* a provider is unavailable.

An over-pace window means we're pacing the burn with real quota left (a soft block, yellow ~), which is
NOT the same as an exhausted or unknown window (a hard block, red ✗). The old code printed a single
"over-pace/exhausted" reason and a red ✗ for both, so a healthy worker that was merely ahead of pace
looked out of quota. Dependency-free.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc


def W(name, used, elapsed, status, detail=None, budget=None):
    return tc.Window(name, used, elapsed, None, status, budget, detail)


def prov(*windows):
    return tc.Provider("claude", False, None, list(windows))


# --- _unavail_reason: (soft, reason) ----------------------------------------
reason_cases = [
    (
        "over-pace is a soft block reporting headroom",
        prov(W("session", 36.0, 23.0, "over-pace"), W("weekly", 32.0, 86.0, "under-pace")),
        (True, "session ahead of pace, 64% left"),
    ),
    (
        "exhausted is a hard block",
        prov(W("session", 100.0, 50.0, "exhausted"), W("weekly", 20.0, 80.0, "under-pace")),
        (False, "session exhausted"),
    ),
    (
        "exhausted dominates a co-occurring over-pace window",
        prov(W("session", 100.0, 50.0, "exhausted"), W("weekly", 60.0, 40.0, "over-pace")),
        (False, "session exhausted"),
    ),
    (
        "unknown gating window is a hard block, named",
        prov(W("session", None, None, "unknown"), W("weekly", 20.0, 80.0, "under-pace")),
        (False, "session usage unknown"),
    ),
    (
        "unknown dominates a co-occurring over-pace window (fail-closed, not soft)",
        prov(W("session", None, None, "unknown"), W("weekly", 60.0, 40.0, "over-pace")),
        (False, "session usage unknown"),
    ),
    (
        "two over-pace windows are both listed",
        prov(W("session", 55.0, 40.0, "over-pace"), W("weekly", 48.0, 46.0, "over-pace")),
        (True, "session ahead of pace, 45% left; weekly ahead of pace, 52% left"),
    ),
    # Sitting exactly ON the budget is a soft block of its own: there is quota left and the burn line is
    # simply not ahead of us yet. Saying "ahead of pace" there would be a lie about which way to wait.
    (
        "at budget is soft, and reads as an equality",
        prov(W("session", 50.0, 50.0, "at-budget"), W("weekly", 20.0, 80.0, "under-pace")),
        (True, "session at budget, 50% left"),
    ),
    (
        "at budget and over pace are both pacing blocks",
        prov(W("session", 50.0, 50.0, "at-budget"), W("weekly", 60.0, 40.0, "over-pace")),
        (True, "session at budget, 50% left; weekly ahead of pace, 40% left"),
    ),
    (
        "a hard window still dominates a pacing one",
        prov(
            W("session", 50.0, 50.0, "at-budget"),
            W("weekly", None, None, "absent", "limit missing from usage response"),
        ),
        (False, "weekly limit missing from usage response"),
    ),
    # The states the redesigned Claude reader distinguishes. Each is a HARD block that says what
    # actually happened — "usage unknown" would send an operator looking for the wrong problem.
    (
        "a reset window awaiting its first request",
        prov(
            W("session", None, None, "idle", "window reset; awaiting initialization"),
            W("weekly", 20.0, 80.0, "under-pace"),
        ),
        (False, "session window reset; awaiting initialization"),
    ),
    (
        "a bootstrap request that has been made but not yet reflected",
        prov(
            W("session", None, None, "idle", "bootstrap attempted; awaiting fresh usage"),
            W("weekly", 20.0, 80.0, "under-pace"),
        ),
        (False, "session bootstrap attempted; awaiting fresh usage"),
    ),
    (
        "a bootstrap request that failed",
        prov(
            W("session", None, None, "idle", "bootstrap failed: claude exited 1"),
            W("weekly", 20.0, 80.0, "under-pace"),
        ),
        (False, "session bootstrap failed: claude exited 1"),
    ),
    (
        "a window missing from the response",
        prov(
            W("session", 5.0, 20.0, "under-pace"),
            W("weekly", None, None, "absent", "limit missing from usage response"),
        ),
        (False, "weekly limit missing from usage response"),
    ),
    (
        "an unreadable reset clock",
        prov(
            W("session", None, None, "malformed", "reset timestamp invalid (five_hour)"),
            W("weekly", 20.0, 80.0, "under-pace"),
        ),
        (False, "session reset timestamp invalid (five_hour)"),
    ),
    (
        "two hard conditions at once are both reported",
        prov(
            W("session", 100.0, 50.0, "exhausted"),
            W("weekly", None, None, "malformed", "usage figure is not a usable percentage (seven_day)"),
        ),
        (False, "session exhausted; weekly usage figure is not a usable percentage (seven_day)"),
    ),
]

fails = 0
for name, p, expected in reason_cases:
    got = tc._unavail_reason(p)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got} want={expected}")
    fails += not ok

# --- the pace parenthetical explains itself ---------------------------------
# An operator reads this beside their own usage readout, where every number is used% against elapsed%.
# Without the elapsed fraction and the word `pace`, the budget looks like a second usage figure that
# disagrees with theirs. And rounding both sides to whole percents printed a real `used > budget` as
# `used 8% > 8% budget`: the pair below is over by 0.0017 points, about nine seconds of clock.
pace_text_cases = [
    (
        "the block names how far into the window it is",
        W("session", 44.0, 61.5, "over-pace", budget=42.2),
        "session ahead of pace (62% elapsed: used 44% > 42% pace budget), 56% left",
    ),
    (
        "a hair over the budget still reads as over",
        W("weekly", 8.0, 12.0, "over-pace", budget=7.998347),
        "weekly ahead of pace (12% elapsed: used 8.000% > 7.998% pace budget), 92% left",
    ),
    (
        "an unmeasurable budget drops the parenthetical rather than inventing one",
        W("session", 36.0, 23.0, "over-pace"),
        "session ahead of pace, 64% left",
    ),
]
for name, window, expected in pace_text_cases:
    got = tc._pace_reason(window)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={expected!r}")
    fails += not ok

# --- quota_line: glyph + honest reason end-to-end ---------------------------
soft = {
    "codex": tc.Provider(
        "codex", False, None, [W("session", 9.0, 41.0, "under-pace"), W("weekly", 48.0, 46.0, "over-pace")]
    )
}
line = tc.quota_line(soft)
for want in ("codex", "[yellow]~[/]", "weekly ahead of pace, 52% left"):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] quota_line soft contains {want!r}: {line!r}")
    fails += not ok
ok = "[red]✗[/]" not in line
print(f"[{'OK ' if ok else 'XX '}] quota_line soft is not a red block: {line!r}")
fails += not ok

hard = {
    "claude": tc.Provider(
        "claude", False, None, [W("session", 100.0, 30.0, "exhausted"), W("weekly", 10.0, 50.0, "under-pace")]
    )
}
line = tc.quota_line(hard)
for want in ("[red]✗[/]", "session exhausted"):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] quota_line hard contains {want!r}: {line!r}")
    fails += not ok

# --- loop waiting display: report the immediate pacing bottleneck without changing control --------
idle_paced = prov(
    W("session", None, 0.0, "idle", "window reset; awaiting initialization", budget=0.0),
    W("weekly", 38.0, 33.0, "over-pace", budget=33.0),
)
codex_paced = tc.Provider("codex", False, None, [W("weekly", 32.0, 20.0, "over-pace", budget=20.0)])
line = tc._wait_quota_line({"codex": codex_paced, "claude": idle_paced})
for want in (
    "claude [yellow]~[/]",
    "weekly ahead of pace (33% elapsed: used 38% > 33% pace budget), 62% left",
    "session window reset — initialization deferred until pacing permits",
):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] wait line contains {want!r}: {line!r}")
    fails += not ok

# The yellow display is descriptive only: launch control must continue treating the unopened session
# as a hard block, so --ignore-quota cannot accidentally run a full round through it.
got = tc._ignore_quota_verdict(None, idle_paced)
ok = got == "wait"
print(f"[{'OK ' if ok else 'XX '}] idle+pacing display leaves control hard: got={got!r} want='wait'")
fails += not ok

idle_at_budget = prov(
    W("session", None, 0.0, "idle", "window reset; awaiting initialization", budget=0.0),
    W("weekly", 33.0, 33.0, "at-budget", budget=33.0),
)
line = tc._wait_quota_line({"claude": idle_at_budget})
for want in (
    "weekly at budget (33% elapsed: used 33% = 33% pace budget), 67% left",
    "session window reset — initialization deferred until pacing permits",
):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] at-budget wait line contains {want!r}: {line!r}")
    fails += not ok

# A genuine hard failure must not be cosmetically downgraded merely because a sibling is pacing-blocked.
malformed_and_paced = prov(
    W("session", None, None, "malformed", "reset timestamp invalid (five_hour)"),
    W("weekly", 38.0, 33.0, "over-pace", budget=33.0),
)
plain = tc.quota_line({"claude": malformed_and_paced})
displayed = tc._wait_quota_line({"claude": malformed_and_paced})
ok = displayed == plain and "[red]✗[/]" in displayed
print(f"[{'OK ' if ok else 'XX '}] genuine hard failure is unchanged: {displayed!r}")
fails += not ok

# --- markup=False: the same line for a destination that renders nothing ----------------------------
# The loop driver logs the quota summary every time it sleeps, and log() writes to stderr and a file,
# neither of which is a Rich console. A style tag left in is not a one-off blemish: it is on every line
# of an idle worker's log. Only the glyph wrapper goes; the reason text is identical.
for label, snap in (
    ("error", {"claude": tc.Provider("claude", False, None, error="usage HTTP 401")}),
    ("available", {"codex": tc.Provider("codex", True, "gpt-5")}),
    ("soft", soft),
    ("hard", hard),
):
    styled, bare = tc.quota_line(snap), tc.quota_line(snap, markup=False)
    ok = not any(tag in bare for tag in ("[yellow]", "[green]", "[red]", "[/]")) and styled == bare.replace(
        "?", "[yellow]?[/]"
    ).replace("✓", "[green]✓[/]").replace("~", "[yellow]~[/]").replace("✗", "[red]✗[/]")
    print(f"[{'OK ' if ok else 'XX '}] quota_line {label} drops only the glyph wrapper: {bare!r}")
    fails += not ok

bare_wait = tc._wait_quota_line({"codex": codex_paced, "claude": idle_paced}, markup=False)
for want in ("claude ~ (", "codex ~ (", "session window reset — initialization deferred until pacing permits"):
    ok = want in bare_wait
    print(f"[{'OK ' if ok else 'XX '}] plain wait line contains {want!r}: {bare_wait!r}")
    fails += not ok
ok = "[" not in bare_wait
print(f"[{'OK ' if ok else 'XX '}] plain wait line has no bracket at all: {bare_wait!r}")
fails += not ok

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

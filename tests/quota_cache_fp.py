#!/usr/bin/env python3
"""The quota cache must be keyed by the credentials it was fetched under.

When the operator rotates to a different account, the cached usage is for the WRONG account. Keying the
entry on a credential fingerprint makes a rotation read as a cache miss immediately, instead of serving
stale numbers until the (1-hour, for claude) TTL lapses, which used to leave workers sleeping on another
account's exhausted-looking quota. Dependency-free; no network.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import sys
import tempfile
import time
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


# A Quota with only its cache_dir wired up — enough to exercise the raw cache layer offline.
q = tc.Quota.__new__(tc.Quota)
q.cache_dir = Path(tempfile.mkdtemp())

# Distinct accounts ⇒ distinct fingerprints; same token ⇒ same fingerprint; no creds ⇒ None.
fp_gmail = tc.Quota._fingerprint("gmail-token")
fp_qim = tc.Quota._fingerprint("qim-token")
check("different tokens fingerprint differently", fp_gmail != fp_qim, True)
check("same token is stable", tc.Quota._fingerprint("qim-token"), fp_qim)
check("no creds ⇒ None", tc.Quota._fingerprint(None), None)
check("empty ⇒ None", tc.Quota._fingerprint("", None), None)

# Store under the gmail account.
q._store_raw("claude", {"five_hour": {"utilization": 36.0}}, fp_gmail)

# Same account, within TTL ⇒ hit.
check("same fp within TTL ⇒ hit", q._cached_raw("claude", fp_gmail), {"five_hour": {"utilization": 36.0}})

# Rotated to qim ⇒ the gmail entry is for the wrong account ⇒ miss (the bug this fixes).
check("rotated fp ⇒ miss (re-fetch)", q._cached_raw("claude", fp_qim), None)

# A None fingerprint (creds vanished) never matches a stored entry ⇒ miss.
check("None fp ⇒ miss", q._cached_raw("claude", None), None)

# Expired entry ⇒ miss even with the matching fingerprint.
stale = {"fetched_at": int(time.time()) - tc.QUOTA_TTL["claude"] - 1, "fp": fp_gmail, "payload": {"x": 1}}
(q.cache_dir / "quota-claude.json").write_text(tc.json.dumps(stale))
check("matching fp but past TTL ⇒ miss", q._cached_raw("claude", fp_gmail), None)

# A codex entry is served with the instant it describes, and only until the earliest window it reports
# rolls: its clocks are durations REMAINING, so a served entry that outlived its window would pace a
# fresh window against the old one's usage.
codex_payload = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 20, "limit_window_seconds": 5 * 3600, "reset_after_seconds": 600},
        "secondary_window": None,
    }
}
at = time.time() - 60
q._store_raw("codex", codex_payload, fp_gmail, tc._codex_valid_until(codex_payload, at), at)
served = q._cached_codex(fp_gmail)
check("a codex entry comes back with its fetch time", (served[0], round(served[1] - at)), (codex_payload, 0))
check(
    "the window it yields is anchored there, not re-anchored to now",
    round(q._codex_from_payload(*served).windows[0].resets_at - at),
    600,
)
# Wind the same entry past the window it describes: it must stop being served. The entry is still WELL
# inside codex's 10-minute TTL, so only the payload's own expiry can reject it.
short = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 20, "limit_window_seconds": 5 * 3600, "reset_after_seconds": 30},
        "secondary_window": None,
    }
}
rolled = time.time() - 60  # 60s ago, describing a window that had 30s left ⇒ rolled 30s ago
check("the entry is nowhere near the TTL", time.time() - rolled < tc.QUOTA_TTL["codex"], True)
q._store_raw("codex", short, fp_gmail, tc._codex_valid_until(short, rolled), rolled)
check("past its window's reset ⇒ miss", q._cached_codex(fp_gmail), None)
# ...and an entry written before this rule existed carries no expiry at all, which is exactly the
# population whose window may already have rolled. The expiry is re-derived, not trusted.
legacy = {"fetched_at": rolled, "fp": fp_gmail, "payload": short}
(q.cache_dir / "quota-codex.json").write_text(tc.json.dumps(legacy))
check("a legacy entry with no stored expiry is revalidated", q._cached_codex(fp_gmail), None)
check("...though the raw TTL layer would have served it", q._cached_raw("codex", fp_gmail) is not None, True)

# A payload whose window cannot be placed in time is never pinned, so there is nothing to serve — and
# the same clock check decides both, so a window too broken to cache is too broken to pace against.
for junk in (-1, True, float("nan"), "600", None, 5 * 3600 + 1):
    bad = {
        "rate_limit": {
            "primary_window": {"used_percent": 1, "limit_window_seconds": 5 * 3600, "reset_after_seconds": junk}
        }
    }
    check(f"reset_after_seconds={junk!r} ⇒ not cacheable", tc._codex_valid_until(bad, at), None)
    check("...and reads as unknown, not available", q._codex_from_payload(bad, at).windows[0].status, "unknown")
check("an unplaceable codex payload has no expiry to store", tc._codex_valid_until({"rate_limit": {}}, at), None)
# A reset that is happening RIGHT NOW is not a window a request can land in: it computes to 100% elapsed,
# where the budget is the whole quota, so any usage short of 100 would read as under-pace on a window
# already rolling. A window that just OPENED (ra == lim) is fine and the endpoint does report it.
zero = {
    "rate_limit": {"primary_window": {"used_percent": 20, "limit_window_seconds": 5 * 3600, "reset_after_seconds": 0}}
}
check("reset_after_seconds=0 ⇒ unknown, not under-pace", q._codex_from_payload(zero, at).windows[0].status, "unknown")
check("...and not cacheable either", tc._codex_valid_until(zero, at), None)
fresh_win = {
    "rate_limit": {
        "primary_window": {"used_percent": 0, "limit_window_seconds": 5 * 3600, "reset_after_seconds": 5 * 3600}
    }
}
check("a just-opened window is still read", q._codex_from_payload(fresh_win, at).windows[0].elapsed, 0.0)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

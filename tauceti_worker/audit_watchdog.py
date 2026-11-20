"""One cursor watchdog: the audit cursor is advancing, or the operator has a private DM.

Every audit round, every pool pass and the standalone `security_audit watchdog` command
observe the protected cursor. The clock starts when audit work is due at a cursor and resets
only when the cursor moves or no work is due. Once due work has held one cursor for an hour,
an "Operational stall" DM is delivered, then again every day the same cursor stays held.
Failing to observe the cursor is itself a stall: the last known cursor's clock keeps running.
The watchdog never changes audit blocks, recovery budgets or the cursor.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from .audit_alerts import deliver, delivery_pending, locked, save
from .config import log
from .constants import TAUCETI

STALL_SECONDS = 3600
REMIND_SECONDS = 86400


def admitted(work: Path, cursor: str) -> bool:
    """Work already started at this cursor stays due below the admission threshold.

    That is a held block there, or a pool range starting there: the pool's durable range list
    keeps a range until the cursor advances past it, so a passed range whose cursor update keeps
    failing is still due. A remainder nobody has started waits for the threshold.
    """
    if load(work / "blocked.json").get("from_sha") == cursor:
        return True
    path = work / "pool" / "ranges.json"
    ranges = load(path).get("ranges")
    if path.exists() and not isinstance(ranges, list):
        return True  # The pool holds on a list it cannot read: that stall is due work too.
    return isinstance(ranges, list) and any(isinstance(r, dict) and r.get("from_sha") == cursor for r in ranges)


def load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def check(work: Path, observe, *, min_commits: int) -> None:
    """`observe()` returns (cursor, pending first-parent commits) or raises. Never raises itself.

    Without a private alert configuration there is nobody to tell: skip the observation entirely.
    """
    if not os.environ.get("TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG"):
        return
    try:
        _check(work, observe, min_commits=min_commits)
    except Exception as exc:  # noqa: BLE001 - the watchdog must never stop audit or maintenance work.
        delivery_pending(exc)


def threshold(work: Path, default: int = 20) -> int:
    """The admission threshold the cursor's owner last used; the standalone check must agree."""
    value = load(work / "stall.json").get("min_commits")
    return value if type(value) is int and value > 0 else default


def _check(work: Path, observe, *, min_commits: int) -> None:
    work.mkdir(parents=True, exist_ok=True)
    with locked(work / "stall.lock"):
        _observe(work, observe, min_commits=min_commits)


def _observe(work: Path, observe, *, min_commits: int) -> None:
    path = work / "stall.json"
    state = load(path)
    now = time.time()
    since = state.get("since")
    if type(since) not in (int, float) or not math.isfinite(since) or since > now:
        since = None
    try:
        cursor, pending = observe()
        error = None
        due = pending > 0 and (pending >= min_commits or admitted(work, cursor))
    except Exception as exc:  # noqa: BLE001 - an unobservable cursor is a stall, never silence.
        cursor, pending = state.get("cursor"), state.get("pending")
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        due = True
    if not due or cursor != state.get("cursor"):
        since = None  # The clock runs only while due work holds one cursor.
    if due and since is None:
        since = now
    save(
        path,
        {
            "cursor": cursor,
            "since": since,
            "due": due,
            "pending": pending,
            "error": error,
            "checked": now,
            "min_commits": min_commits,
        },
    )
    held = now - since if due else 0.0
    if held < STALL_SECONDS:
        return
    occurrence = int((held - STALL_SECONDS) // REMIND_SECONDS)
    identity = {"kind": "Cursor stall", "cursor": cursor, "since": since, "occurrence": occurrence}

    def render(marker: str) -> str:
        hours = held / 3600
        cause = f"The watchdog cannot observe the audit: `{error}`" if error else f"{pending} commit(s) are pending."
        where = f"https://github.com/{TAUCETI}/commits/{cursor}" if cursor else f"https://github.com/{TAUCETI}"
        return (
            "**Tau Ceti security audit — Operational stall**\n\n"
            f"**Attention: the audit cursor has not advanced for {hours:.1f} hours while audit work is due.** "
            f"{cause}\n\nCursor: {where}\n\n"
            "Toolchain releases remain gated by that cursor. Any binding finding has its own alert. "
            f"Evidence on the worker host: `{work / 'stall.json'}`, `{work / 'blocked.json'}` and "
            f"`{work / 'ledger.jsonl'}`.\n\nAlert ID: {marker}"
        )

    if not deliver(work, identity, render):
        log(f"security-audit: cursor held {held / 3600:.1f}h; private stall alert not yet delivered")

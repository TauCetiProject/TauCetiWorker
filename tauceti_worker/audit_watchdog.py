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

from .audit_alerts import ALERT_ERRORS, deliver, delivery_pending, save
from .config import log
from .constants import TAUCETI

STALL_SECONDS = 3600
REMIND_SECONDS = 86400


def admitted(work: Path, cursor: str) -> bool:
    """An inspection was started from this cursor: admitted work stays due below the threshold."""
    ledger = work / "ledger.jsonl"
    if not ledger.exists():
        return False
    for line in ledger.read_text().splitlines():
        try:
            if json.loads(line).get("from_sha") == cursor:
                return True
        except (ValueError, AttributeError):
            continue
    return False


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
    except ALERT_ERRORS as exc:
        delivery_pending(exc)


def _check(work: Path, observe, *, min_commits: int) -> None:
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
    if cursor != state.get("cursor") or not due or since is None:
        since = now
    held = now - since
    save(path, {"cursor": cursor, "since": since, "pending": pending, "error": error, "checked": now})
    if not due or held < STALL_SECONDS:
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

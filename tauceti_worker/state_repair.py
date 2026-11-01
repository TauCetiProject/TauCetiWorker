"""Best-effort shared correction without a discovery-wide pause or PR error."""

from __future__ import annotations

import contextlib
import time

from . import github_state
from .config import log
from .constants import TAUCETI
from .runtime_status import report_runtime


def retry_key(pr: int, head: str) -> str:
    return f"state-refresh-{pr}-{head[:12]}-retry-at"


def repair_selected(w, candidate, observed: str = "") -> None:
    w.selection_changed = True
    report_runtime(failure_reason=None, failure_code=None, failure_log=None)
    key = retry_key(candidate.pr, candidate.head)
    if w.counters.read(key) > time.time():
        return
    # Persist a short retry first, so crashes and old servers cannot cause a
    # hot loop. This is a deadline, never ownership or permanent exclusion.
    with contextlib.suppress(OSError):
        w.counters.write(key, int(time.time()) + 30)
    result = github_state.refresh_pr(TAUCETI, candidate.pr, candidate.head, observed)
    retry = result.get("retry_at", time.time() + (1 if result.get("status") == "ok" else 30))
    with contextlib.suppress(OSError):
        w.counters.write(key, int(retry) + 1)
    if getattr(w, "rs", None) is not None:
        with contextlib.suppress(OSError):
            w.rs.bust(candidate.pr)
    log(
        f"  PR #{candidate.pr}: selected state changed; shared refresh {result.get('status', 'unavailable')}; selecting other work"
    )

"""tauceti_worker.identity — the halt a worker records when its GitHub identity is one no retry fixes.

`pin_github_account` raises `Halted` for a login other than the configured one, a rejected
credential, or a suspended account. The record written here stops every later start under the same
worker id until a person has read it and run `tauceti work --worker-id ID --clear-halt`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Halted, log, sanitize_wid, worker_state_root
from .runtime_status import atomic_json, read_json

IDENTITY_MISMATCH = "identity-mismatch"
INVALID_CREDENTIALS = "invalid-credentials"
ACCOUNT_HALT = "account-halt"


def halt_file() -> Path | None:
    """This worker's halt record, or None while it has no id (an auto-assigned slot is chosen later)."""
    wid = os.environ.get("TAUCETI_WORKER_ID")
    return worker_state_root() / sanitize_wid(wid) / "halt.json" if wid else None


def write_halt(path: Path | None, halt: Halted) -> None:
    if path is None:
        return
    record = {"reason": halt.reason, "detail": str(halt), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        atomic_json(path, record)
    except OSError as error:
        log(f"could not record the halt at {path}: {error}")


def refuse_if_halted(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    record = read_json(path)
    raise Halted(
        str(record.get("reason") or "unreadable"),
        f"halted at {record.get('at')}: {record.get('detail')} — resolve it, then run "
        f"`tauceti work --worker-id {path.parent.name} --clear-halt` ({path})",
    )


def clear_halt(path: Path) -> int:
    """Show the record before removing it, so clearing follows reading."""
    if not path.exists():
        print(f"no halt file at {path}")
        return 0
    print(path.read_text().rstrip("\n"))
    path.unlink()
    print(f"cleared {path}")
    return 0

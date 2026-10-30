"""Bounded, best-effort records of TauCeti's actual usage HTTP requests."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024


def record(directory: Path, **fields) -> None:
    """Never wait for another logger, and never make diagnostics a launch prerequisite.

    One active file and two backups retain at most about 6 MiB. Only callers' fixed,
    allow-listed metadata belongs here, never request headers, bodies, or credentials.
    """
    fd = lock = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(directory / "requests.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "requests.jsonl"
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            older = directory / "requests.jsonl.1"
            if older.exists():
                os.replace(older, directory / "requests.jsonl.2")
            os.replace(path, older)
        data = (json.dumps(dict(at=time.time(), pid=os.getpid(), **fields)) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, data)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if lock is not None:
            with contextlib.suppress(OSError):
                os.close(lock)

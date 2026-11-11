"""Local incident records: what a round concluded about a pull request that GitHub does not show.

Off unless TAUCETI_INCIDENTS_DIR names a directory, which the workers of a fleet share. The file
format, the directory layout and what a reader may do to a record are in docs/incidents.md.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .config import log
from .constants import TAUCETI

ENV = "TAUCETI_INCIDENTS_DIR"
HOLD_ENV = "TAUCETI_HOLD_DECLINED"
SCHEMA = "tauceti-incident/v1"
ACKED = "acked"
DECLINED = "declined"
BUDGET_SPENT = "budget-spent"
RULING = "ruling"  # written by decide.py; a `retry` one lifts the hold below
# The stages whose rounds leave these records: the ones that repair an existing pull request head.
STAGES = ("rebase", "fix", "fix-ci")
HELD = "an earlier round declined at this head; waiting for a new head"
LOCK = ".records.lock"
MAX_SUMMARY = 2000

PROMPT = (
    "\nIf this session ends with no push, closure or reply on this PR, its final message is recorded "
    "for the operator. Begin that message with what prevented each of those outcomes, and end it with "
    "one line `Subsumed-by: #N [#M ...]` naming the merged pull requests that replace this PR, or "
    "`Subsumed-by: none`.\n"
)

_TRAILER = re.compile(r"^[ \t>*_`-]*Subsumed-by\b[*_`]*:?[*_`]*[ \t]*(.*)$", re.I | re.M)
_PR_REF = re.compile(r"(?<![\w/])#(\d+)\b")


def directory() -> Path | None:
    raw = os.environ.get(ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def hold_declined() -> bool:
    return directory() is not None and os.environ.get(HOLD_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _name(kind: str, key: str) -> str:
    return f"{kind}-{''.join(c if c.isalnum() or c in '-_.' else '_' for c in key)}.json"


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def find(kind: str, key: str) -> list[dict]:
    """The records named `<kind>-<key>`: the live one, then the acknowledged one, where they exist."""
    root = directory()
    if root is None:
        return []
    found = (_load(folder / _name(kind, key)) for folder in (root, root / ACKED))
    return [value for value in found if value is not None]


def records(kind: str = "", *, acked: bool = True) -> list[tuple[Path, dict]]:
    """Every readable record, live ones first and by file name within each folder; `kind` narrows
    to one kind, and `acked=False` leaves out the acknowledged ones."""
    root = directory()
    if root is None:
        return []
    out: list[tuple[Path, dict]] = []
    for folder in (root, root / ACKED) if acked else (root,):
        try:
            paths = sorted(folder.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            value = _load(path)
            if value is not None and (not kind or value.get("kind") == kind):
                out.append((path, value))
    return out


_held = threading.local()


@contextlib.contextmanager
def _locked(root: Path):
    """Hold the directory's record lock: `record` takes it to replace a live record, and
    `acknowledge` to compare one before moving it. Where the lock cannot be opened, the caller goes
    on without it and meets the same error in its own write. A thread that holds it already goes on:
    a second `flock` from the same process would wait on itself."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(root / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _held.depth = 1
        yield
    finally:
        _held.depth = 0
        os.close(fd)


def record(kind: str, key: str, **fields) -> Path | None:
    """Write or refresh the live record `<kind>-<key>.json` and return its path. None when the
    directory is not configured or the write failed: a record is a report, never a step of the round."""
    root = directory()
    if root is None:
        return None
    with _locked(root):
        return _write(root, kind, key, fields)


def _write(root: Path, kind: str, key: str, fields: dict) -> Path | None:
    path = root / _name(kind, key)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    previous = _load(path) or {}
    value = {
        "schema": SCHEMA,
        "kind": kind,
        "key": key,
        "worker": os.environ.get("TAUCETI_WORKER_ID") or "default",
        "first_at": previous.get("first_at") or now,
        "last_at": now,
        "count": (previous.get("count") if type(previous.get("count")) is int else 0) + 1,
        **fields,
    }
    temporary = None
    try:
        text = json.dumps(value, indent=1, sort_keys=True) + "\n"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=root)
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as error:
        log(f"incident {path.name} could not be written under {root}: {error}")
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        return None
    return path


def _keep_displaced(path: Path) -> None:
    """Hard-link an acknowledged record about to be replaced as `<name>.<n>`, the first free `n`."""
    n = 1
    while True:
        try:
            os.link(path, path.with_name(f"{path.name}.{n}"))
            return
        except FileExistsError:
            n += 1
        except FileNotFoundError:
            return


def acknowledge(kind: str, key: str, version: dict | None = None) -> bool:
    """Move the live record `<kind>-<key>` into `acked/`; an earlier acknowledged one of that name
    is kept as `acked/<name>.<n>`. With `version`, only while the live record is exactly that one,
    as read earlier. False when there is no such live record or the move failed."""
    root = directory()
    if root is None:
        return False
    name = _name(kind, key)
    with _locked(root):
        if version is not None and _load(root / name) != version:
            return False
        try:
            (root / ACKED).mkdir(mode=0o700, exist_ok=True)
            if (root / name).exists():
                _keep_displaced(root / ACKED / name)
            os.replace(root / name, root / ACKED / name)
        except FileNotFoundError:
            return False
        except OSError as error:
            log(f"incident {name} could not be acknowledged under {root}: {error}")
            return False
    return True


def named_prs(message: str, pr: int) -> tuple[list[int], list[int]]:
    """(subsumed_by, mentions): the pull requests the last `Subsumed-by:` line of `message` names,
    and the other ones the message refers to as `#N`; `pr` itself is in neither."""
    trailers = _TRAILER.findall(message)
    declared = [int(n) for n in _PR_REF.findall(trailers[-1])] if trailers else []
    declared = [n for n in dict.fromkeys(declared) if n != pr]
    mentioned = dict.fromkeys(int(n) for n in _PR_REF.findall(message))
    return declared, [n for n in mentioned if n != pr and n not in declared]


def record_declined(stage: str, pr: int, head: str, *, reason: str = "", message: str = "", transcript: str = ""):
    """Record that a `stage` round on `pr` at `head` ended with nothing on GitHub, in the agent's words."""
    message = message.strip()
    subsumed_by, mentions = named_prs(message, pr)
    summary = message if len(message) <= MAX_SUMMARY else message[: MAX_SUMMARY - 2].rstrip() + " …"
    return record(
        DECLINED,
        f"{stage}-{pr}",
        repository=TAUCETI,
        stage=stage,
        pr=pr,
        head=head,
        reason=reason,
        summary=summary,
        subsumed_by=subsumed_by,
        mentions=mentions,
        transcript=transcript,
    )


def declined(stage: str, pr: int, head: str) -> bool:
    """Whether `stage` is held off `pr` at `head`: TAUCETI_HOLD_DECLINED is on, a `declined`
    record, live or acknowledged, names this head, and no `retry` ruling has been made on the
    latest of them."""
    if not hold_declined():
        return False
    key = f"{stage}-{pr}"
    written = [str(value.get("last_at") or "") for value in find(DECLINED, key) if value.get("head") == head]
    return bool(written) and not any(
        value.get("ruling") == "retry" and value.get("head") == head and value.get("incident_at") == max(written)
        for value in find(RULING, f"{DECLINED}-{key}")
    )


def note_budget_spent(stage: str, pr: int, head: str, detail: str) -> None:
    """Record that `stage` has no attempts left for `pr` at `head`. A survey sees a spent budget on
    every round, so a record that already names this head, acknowledged or not, is left as it is."""
    if any(value.get("head") == head for value in find(BUDGET_SPENT, f"{stage}-{pr}")):
        return
    record(BUDGET_SPENT, f"{stage}-{pr}", repository=TAUCETI, stage=stage, pr=pr, head=head, detail=detail)

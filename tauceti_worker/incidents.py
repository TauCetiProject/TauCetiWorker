"""Local incident records: what a round concluded about a pull request that GitHub does not show.

Off unless TAUCETI_INCIDENTS_DIR names a directory, which the workers of a fleet share. The file
format, the directory layout and what a reader may do to a record are in docs/incidents.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
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
# The stages whose rounds leave these records: the ones that repair an existing pull request head.
STAGES = ("rebase", "fix", "fix-ci")
HELD = "an earlier round declined at this head; waiting for a new head"
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


def record(kind: str, key: str, **fields) -> Path | None:
    """Write or refresh the live record `<kind>-<key>.json` and return its path. None when the
    directory is not configured or the write failed: a record is a report, never a step of the round."""
    root = directory()
    if root is None:
        return None
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
    """Whether `stage` is held off `pr` at `head`: TAUCETI_HOLD_DECLINED is on and a `declined`
    record, live or acknowledged, names this head."""
    return hold_declined() and any(value.get("head") == head for value in find(DECLINED, f"{stage}-{pr}"))


def note_budget_spent(stage: str, pr: int, head: str, detail: str) -> None:
    """Record that `stage` has no attempts left for `pr` at `head`. A survey sees a spent budget on
    every round, so a record that already names this head, acknowledged or not, is left as it is."""
    if any(value.get("head") == head for value in find(BUDGET_SPENT, f"{stage}-{pr}")):
        return
    record(BUDGET_SPENT, f"{stage}-{pr}", repository=TAUCETI, stage=stage, pr=pr, head=head, detail=detail)

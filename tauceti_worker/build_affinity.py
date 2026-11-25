"""Send a PR's next agent round to a worker on this host that has already built it.

A worker's private Lake artifact store (`LAKE_CACHE_DIR`) keeps the outputs of every module its
agents built, so a worker that built a PR restores that PR's modules on its next round instead of
recompiling them from the fallback cache. Today the fleet spreads a PR's rounds at random, so
almost every fix round's first verification is cold (decision 210).

After an agent round on the host checkout, the worker records itself as one of the PR's builders.
When a later fix, fix-ci or rebase candidate has builders on this host:

- a builder offers it first, ahead of its other candidates in the same stage;
- any other worker defers it for FIX_AFFINITY_GRACE_S, counted from the first time a peer deferred
  that exact head, so a builder that frees up in time takes it warm; after the grace it is shared.

Nothing here shares built outputs between workers: each worker only ever uses its own store, as it
does today, so one PR's outputs cannot reach another worker's verification. Like peer_claims, the
records are advisory hints under the host's shared read directory ($TAUCETI_GITHUB_SHARED_DIR):
deferral is bounded by the grace, and every read or write failure fails open (no deferral).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

from .constants import FIX_AFFINITY_GRACE_S

# A builder record outlives the fix/review cycles of one PR; older ones say little about the store.
BUILDER_TTL_S = 24 * 3600
STAGES = ("rebase", "fix-ci", "fix")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _directory(scope: str, pr: int) -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    if not shared or not pr:
        return None
    key = hashlib.sha256(json.dumps([scope, f"builders/{pr}"]).encode()).hexdigest()
    return Path(shared) / "pr-builders" / key


def note_builder(scope: str, pr: int, worker: str) -> None:
    """Record that `worker` ran an agent round on PR `pr` from its own checkout on this host."""
    directory = _directory(scope, pr)
    if directory is None or not _NAME.fullmatch(worker or ""):
        return
    try:
        (directory / "workers").mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "workers" / worker
        path.touch()
        os.utime(path)  # touch() leaves an existing file's mtime alone on some filesystems
        if random.random() < 0.02:
            _prune(directory.parent, time.time())
    except OSError:
        pass  # an unrecorded builder only means its next round on this PR may be cold


def builders(scope: str, pr: int, *, now: float | None = None) -> set[str]:
    """The workers on this host that built PR `pr` within BUILDER_TTL_S."""
    directory = _directory(scope, pr)
    if directory is None:
        return set()
    stamp = time.time() if now is None else now
    found = set()
    try:
        for path in (directory / "workers").iterdir():
            try:
                if 0 <= stamp - path.stat().st_mtime <= BUILDER_TTL_S:
                    found.add(path.name)
            except OSError:
                pass
    except OSError:
        pass
    return found


def _in_grace(scope: str, pr: int, head: str, now: float) -> bool:
    """Whether this head is in its first-refusal window: FIX_AFFINITY_GRACE_S from the first time a
    worker on the host considered it with builders recorded."""
    directory = _directory(scope, pr)
    if directory is None or not _NAME.fullmatch(head or ""):
        return False
    marker = directory / "deferred" / head
    try:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            os.utime(marker, (now, now))
        except FileExistsError:
            pass
        started = marker.stat().st_mtime
    except OSError:
        return False
    # A marker dated in the future (a clock we cannot place) does not defer.
    return 0 <= now - started < FIX_AFFINITY_GRACE_S


def prioritize(scope: str, worker: str, candidates: list, *, now: float | None = None) -> tuple[list, list]:
    """(this worker's builder candidates, then the shared ones), and the peers' deferred ones.

    Both the builders' priority and the peers' deferral last only for the head's grace, so a warm
    backlog cannot starve other PRs. Each group keeps the incoming order, so the stage's
    age-weighted spread still applies within it.
    """
    stamp = time.time() if now is None else now
    owned, shared, deferred = [], [], []
    for candidate in candidates:
        found = builders(scope, candidate.pr, now=stamp) if candidate.pr else set()
        if not found or not _in_grace(scope, candidate.pr, candidate.head, stamp):
            shared.append(candidate)
        elif worker in found:
            owned.append(candidate)
        else:
            deferred.append(candidate)
    return owned + shared, deferred


def built_since(state: Path, since: float) -> bool:
    """Whether this worker's verification receipts show a build started at or after `since`.

    The receipts are the agent's own `tauceti-verify` calls (verification.py); a round whose agent
    never built leaves its store as it was, and so earns no first refusal.
    """
    try:
        entries = list(os.scandir(Path(state) / "verification"))
    except OSError:
        return False
    for entry in entries:
        try:
            if entry.stat().st_mtime < since:
                continue
            receipt = json.loads((Path(entry.path) / "receipt.json").read_text())
        except (OSError, ValueError):
            continue
        started = receipt.get("started_at") if isinstance(receipt, dict) else None
        phases = receipt.get("phases") if isinstance(receipt, dict) else None
        if isinstance(started, (int, float)) and started >= since and isinstance(phases, dict) and "build" in phases:
            return True
    return False


def _prune(root: Path, now: float) -> None:
    """Delete records older than BUILDER_TTL_S, one file at a time, and then any empty directories.

    Each file is judged by its own age just before it goes, so a concurrent refresh can at worst
    lose one record (one cold round) or restart a day-old head's grace.
    """
    try:
        directories = list(root.iterdir())
    except OSError:
        return
    for directory in directories:
        for sub in ("workers", "deferred"):
            try:
                paths = list((directory / sub).iterdir())
            except OSError:
                continue
            for path in paths:
                try:
                    if now - path.stat().st_mtime > BUILDER_TTL_S:
                        path.unlink()
                except OSError:
                    pass
            try:
                (directory / sub).rmdir()  # only succeeds when empty
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

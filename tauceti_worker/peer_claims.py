"""Host-shared hints that a peer holds a PR's claim, read before any fresh PR read.

When a round finds a candidate claimed by a peer (a branch lease for rebase/fix work, or the review
engine's per-head lease), it records a short-lived hint here. Every worker on the host then skips
that candidate without the fresh PR reads that dispatch makes before trying the claim, instead of
each paying them to rediscover the same collision. A hint is advisory: it only ever skips work for
at most HINT_TTL_S, and the real lease stays authoritative for everything a round does.

The store lives under the explicitly configured shared read directory
($TAUCETI_GITHUB_SHARED_DIR, which the fleet's worker manager sets); without it no hint is
recorded or read.

The store also keeps, per PR, the heads that fix rounds on this host have repaired, so the host can
see a PR that fix and review keep handing back and forth (constants.MAX_FIX_ROUNDS_WITHOUT_APPROVAL).
One empty file per head makes concurrent records race-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from pathlib import Path

HINT_TTL_S = 600
# A selected PR whose source could not be fetched is skipped host-wide this long (the per-worker
# retry deadline, github_state.MAX_RETRY_AFTER), so one fetch failure does not draw every peer to it.
UNAVAILABLE_TTL_S = 300


def _root() -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    return Path(shared) / "peer-claims" if shared else None


def _key(scope: str, stage_key: str) -> str:
    return hashlib.sha256(json.dumps([scope, stage_key]).encode()).hexdigest()


def resource(stage: str, pr: int, head: str, *, unavailable: bool = False) -> str:
    """The lease a candidate needs: one branch lease per PR for branch-writing work, one review
    lease per PR head. An unavailable source is noted per stage and exact head instead."""
    if unavailable:
        return f"unavailable/{stage}/{pr}/{head}"
    return f"review/{pr}/{head}" if stage == "review" else f"branch/{pr}"


def _ttl(unavailable: bool) -> int:
    return UNAVAILABLE_TTL_S if unavailable else HINT_TTL_S


def note(scope: str, stage: str, pr: int, head: str, *, now: float | None = None, unavailable: bool = False) -> None:
    """Record a peer's claim, or with `unavailable` that this exact head's source could not be prepared."""
    root = _root()
    if root is None or not pr:
        return
    stamp = time.time() if now is None else now
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (_key(scope, resource(stage, pr, head, unavailable=unavailable)) + ".json")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"expires_at": stamp + _ttl(unavailable)}))
        os.replace(temporary, path)
        if random.random() < 0.02:
            _prune(root, stamp)
    except OSError:
        pass  # a hint we cannot write costs one more collision, nothing else


def hinted(scope: str, stage: str, pr: int, head: str, *, now: float | None = None, unavailable: bool = False) -> bool:
    root = _root()
    if root is None or not pr:
        return False
    try:
        value = json.loads(
            (root / (_key(scope, resource(stage, pr, head, unavailable=unavailable)) + ".json")).read_text()
        )
        expires = value.get("expires_at")
        stamp = time.time() if now is None else now
        # A hint dated beyond its TTL (a clock we cannot place) is ignored rather than trusted.
        return isinstance(expires, (int, float)) and stamp < expires <= stamp + _ttl(unavailable)
    except (OSError, ValueError, AttributeError):
        return False


def _prune(root: Path, now: float) -> None:
    for path in root.glob("*.json"):
        try:
            if now - path.stat().st_mtime > 2 * HINT_TTL_S:
                path.unlink()
        except OSError:
            pass


FIXED_HEAD_TTL_S = 14 * 86400


def _fixed_dir(scope: str, pr: int) -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    if not shared or not pr:
        return None
    return Path(shared) / "fixed-heads" / _key(scope, f"fixed/{pr}")


def note_fixed_head(scope: str, pr: int, head: str) -> None:
    """Record that a fix round on this host repaired `head` of PR `pr`."""
    directory = _fixed_dir(scope, pr)
    if directory is None or not head or "/" in head:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / head).touch()
    except OSError:
        pass  # an unrecorded head only delays the escalation by one round


def fixed_heads(scope: str, pr: int, *, now: float | None = None) -> set[str]:
    """The heads of PR `pr` that fix rounds on this host repaired in the last FIXED_HEAD_TTL_S."""
    directory = _fixed_dir(scope, pr)
    if directory is None:
        return set()
    stamp = time.time() if now is None else now
    heads = set()
    try:
        for path in directory.iterdir():
            try:
                if stamp - path.stat().st_mtime <= FIXED_HEAD_TTL_S:
                    heads.add(path.name)
            except OSError:
                pass
    except OSError:
        pass
    return heads

"""Host-shared hints that a peer holds a PR's claim, read before any fresh PR read.

When a round finds a candidate claimed by a peer (a branch lease for rebase/fix work, or the review
engine's per-head lease), it records a short-lived hint here. Every worker on the host then skips
that candidate without the fresh PR reads that dispatch makes before trying the claim, instead of
each paying them to rediscover the same collision. A hint is advisory: it only ever skips work for
at most HINT_TTL_S, and the real lease stays authoritative for everything a round does.

The store lives under the explicitly configured shared read directory
($TAUCETI_GITHUB_SHARED_DIR, which the fleet's worker manager sets); without it no hint is
recorded or read.
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


def _root() -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    return Path(shared) / "peer-claims" if shared else None


def _key(scope: str, stage_key: str) -> str:
    return hashlib.sha256(json.dumps([scope, stage_key]).encode()).hexdigest()


def resource(stage: str, pr: int, head: str) -> str:
    """The lease a candidate needs: one branch lease per PR for branch-writing work, one review
    lease per PR head."""
    return f"review/{pr}/{head}" if stage == "review" else f"branch/{pr}"


def note(scope: str, stage: str, pr: int, head: str, *, now: float | None = None) -> None:
    root = _root()
    if root is None or not pr:
        return
    stamp = time.time() if now is None else now
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (_key(scope, resource(stage, pr, head)) + ".json")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"expires_at": stamp + HINT_TTL_S}))
        os.replace(temporary, path)
        if random.random() < 0.02:
            _prune(root, stamp)
    except OSError:
        pass  # a hint we cannot write costs one more collision, nothing else


def hinted(scope: str, stage: str, pr: int, head: str, *, now: float | None = None) -> bool:
    root = _root()
    if root is None or not pr:
        return False
    try:
        value = json.loads((root / (_key(scope, resource(stage, pr, head)) + ".json")).read_text())
        expires = value.get("expires_at")
        stamp = time.time() if now is None else now
        # A hint dated beyond its TTL (a clock we cannot place) is ignored rather than trusted.
        return isinstance(expires, (int, float)) and stamp < expires <= stamp + HINT_TTL_S
    except (OSError, ValueError, AttributeError):
        return False


def _prune(root: Path, now: float) -> None:
    for path in root.glob("*.json"):
        try:
            if now - path.stat().st_mtime > 2 * HINT_TTL_S:
                path.unlink()
        except OSError:
            pass

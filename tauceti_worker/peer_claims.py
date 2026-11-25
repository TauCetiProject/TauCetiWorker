"""Host-shared hints that a peer holds a PR's claim, read before any fresh PR read.

When a round finds a candidate claimed by a peer (a branch lease for rebase/fix work, or the review
engine's per-head lease), it records a short-lived hint here. Every worker on the host then skips
that candidate without the fresh PR reads that dispatch makes before trying the claim, instead of
each paying them to rediscover the same collision. A hint is advisory: it only ever skips work for
at most HINT_TTL_S, and the real lease stays authoritative for everything a round does.

The store lives under the explicitly configured shared read directory
($TAUCETI_GITHUB_SHARED_DIR, which the fleet's worker manager sets); without it no hint is
recorded or read.

The store also keeps, per PR, the heads that review sent back for a fix on this host, so the host can
see a PR that fix and review keep handing back and forth (constants.MAX_FIX_ROUNDS_WITHOUT_APPROVAL).
One empty file per head makes concurrent records race-free.

A head that the review engine declined because its diff fails the PR-level guards (too large to
review, oversized new files) is recorded here too, for GUARD_TTL_S. The engine's verdict depends
on the head, its merge base and whether the head's build is green. Peers skip that head until a
push gives the PR a new one or its build turns green; the TTL covers a merge base that moves under
an unchanged head (a retargeted PR), at a few declined reviews a day rather than hundreds.

A head whose verdict the review engine found already published is recorded here as well, for
UNCHANGED_REVIEW_TTL_S. When the shared state service lacks a PR's review history, the worker
cannot see that verdict itself and hands the PR to the engine, which spends about five REST reads
to find it unchanged. Recorded per worker only, every worker on the host paid those reads once
per PR; recorded here, the host pays them once.
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
GUARD_TTL_S = 8 * 3600
# Also the per-worker survey.UNCHANGED_REVIEW_TTL, which explains the bound.
UNCHANGED_REVIEW_TTL_S = 6 * 3600


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


# Long enough to outlive any PR the fleet still works on; only bounds stale records.
FIXED_HEAD_TTL_S = 60 * 86400


def _fixed_dir(scope: str, pr: int) -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    if not shared or not pr:
        return None
    return Path(shared) / "fixed-heads" / _key(scope, f"fixed/{pr}")


def note_fixed_head(scope: str, pr: int, head: str) -> None:
    """Record that review sent `head` of PR `pr` back for a fix on this host."""
    directory = _fixed_dir(scope, pr)
    if directory is None or not head or "/" in head:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / head).touch()
    except OSError:
        pass  # an unrecorded head only delays the cap by one round


def fixed_heads(scope: str, pr: int, *, now: float | None = None) -> set[str]:
    """The heads of PR `pr` sent back for a fix on this host in the last FIXED_HEAD_TTL_S."""
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


def clear_fixed_heads(scope: str, pr: int) -> None:
    directory = _fixed_dir(scope, pr)
    if directory is None:
        return
    try:
        for path in directory.iterdir():
            path.unlink(missing_ok=True)
    except OSError:
        pass


_HEAD_RECORD_DIRS = {"diff-guard": "review-diff-guards", "unchanged": "review-unchanged"}


def _head_record_path(kind: str, scope: str, pr: int, head: str) -> Path | None:
    shared = os.environ.get("TAUCETI_GITHUB_SHARED_DIR")
    if not shared or not pr or not head:
        return None
    return Path(shared) / _HEAD_RECORD_DIRS[kind] / (_key(scope, f"{kind}/{pr}/{head}") + ".json")


def _note_head(kind: str, ttl: int, scope: str, pr: int, head: str, now: float | None) -> None:
    path = _head_record_path(kind, scope, pr, head)
    if path is None:
        return
    stamp = time.time() if now is None else now
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"expires_at": stamp + ttl}))
        os.replace(temporary, path)
        if random.random() < 0.02:
            for old in path.parent.glob("*.json"):
                try:
                    if stamp - old.stat().st_mtime > ttl:
                        old.unlink()
                except OSError:
                    pass
    except OSError:
        pass  # without the record, peers each pay one more engine check


def _head_noted_at(kind: str, ttl: int, scope: str, pr: int, head: str, now: float | None) -> float | None:
    """When the live record for this head was written, or None without one."""
    path = _head_record_path(kind, scope, pr, head)
    if path is None:
        return None
    try:
        expires = json.loads(path.read_text()).get("expires_at")
    except (OSError, ValueError, AttributeError):
        return None
    stamp = time.time() if now is None else now
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not stamp < expires <= stamp + ttl:
        return None
    return float(expires - ttl)


def _head_noted(kind: str, ttl: int, scope: str, pr: int, head: str, now: float | None) -> bool:
    return _head_noted_at(kind, ttl, scope, pr, head, now) is not None


def note_diff_guard(scope: str, pr: int, head: str, *, now: float | None = None) -> None:
    """Record that the review engine declined this exact head on the PR-level diff guards."""
    _note_head("diff-guard", GUARD_TTL_S, scope, pr, head, now)


def diff_guarded(scope: str, pr: int, head: str, *, now: float | None = None) -> bool:
    return _head_noted("diff-guard", GUARD_TTL_S, scope, pr, head, now)


def note_unchanged_review(scope: str, pr: int, head: str, *, now: float | None = None) -> None:
    """Record that the review engine found this exact head's verdict already published."""
    _note_head("unchanged", UNCHANGED_REVIEW_TTL_S, scope, pr, head, now)


def unchanged_review_noted(scope: str, pr: int, head: str, *, now: float | None = None) -> bool:
    return _head_noted("unchanged", UNCHANGED_REVIEW_TTL_S, scope, pr, head, now)


def unchanged_review_noted_at(scope: str, pr: int, head: str, *, now: float | None = None) -> float | None:
    """When a worker on this host found this head's verdict already published, or None."""
    return _head_noted_at("unchanged", UNCHANGED_REVIEW_TTL_S, scope, pr, head, now)


def scope_of(source) -> str:
    """The repository a source's leases and head records belong to, qualified by forge where it has one."""
    repository = getattr(source, "repository", None)
    key = getattr(repository, "key", None)
    return key if isinstance(key, str) else str(getattr(source, "repo", ""))

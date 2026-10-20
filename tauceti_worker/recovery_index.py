"""Disposable, atomic v1 lookup cache; recovery manifests remain the authority.

One fixed-size lookup per source identity, never an inventory read on a work-start path.
An interrupted/missing cache is a miss: the source is saved again, or background reconciliation
registers its descriptor as unverified. Only exact validation can upgrade that entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .runtime_status import atomic_json, read_json

VERSION = 1
PAYLOADS = {"manifest.json", "staged.patch", "unstaged.patch", "untracked.tar", "commits.bundle"}


def key(repository: str, fingerprint: str) -> str:
    return hashlib.sha256(f"{repository}\0{fingerprint}".encode()).hexdigest()


def location(state: Path, repository: str, fingerprint: str) -> Path:
    return state / "recovery-index" / f"{key(repository, fingerprint)}.json"


def stamps(record: Path, manifest: dict) -> dict:
    """Bounded metadata evidence, including inode replacement and same-size edits."""
    names = {"manifest.json", *manifest["files"]}
    if not names <= PAYLOADS:
        raise ValueError("unknown recovery payload")
    result = {}
    for name in sorted(names):
        st = (record / name).lstat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("recovery payload is not a regular file")
        result[name] = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    return result


def checksum(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lookup(state: Path, repository: str, fingerprint: str) -> dict:
    path = location(state, repository, fingerprint)
    try:
        if path.stat().st_size > 16384:
            return {}
    except OSError:
        return {}
    try:
        value = read_json(path)
    except RecursionError:
        return {}
    check = value.pop("checksum", None)
    try:
        expected = checksum(value)
    except (RecursionError, ValueError, TypeError):
        return {}
    if (
        check != expected
        or value.get("version") != VERSION
        or value.get("repository") != repository
        or value.get("fingerprint") != fingerprint
        or not isinstance(value.get("record"), str)
        or Path(value["record"]).name != value["record"]
        or value["record"].startswith(".")
    ):
        return {}
    return value


def remember(state: Path, record: Path, manifest: dict, *, verified: bool, evidence=None, fingerprint=None) -> None:
    # Serialize check-and-replace so reconciliation cannot downgrade a concurrent verification.
    with (state / "recovery-index.lock").open("a+") as lock:
        deadline = time.monotonic() + 0.025
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.001)
        _remember(state, record, manifest, verified=verified, evidence=evidence, fingerprint=fingerprint)


def _remember(state: Path, record: Path, manifest: dict, *, verified: bool, evidence=None, fingerprint=None) -> None:
    repository = manifest.get("repository", "")
    fingerprint = fingerprint or manifest["fingerprint"]
    # Reconciliation must not downgrade an existing verified entry or overwrite a newer copy.
    previous = lookup(state, repository, fingerprint)
    if not verified and previous:
        return
    value = dict(
        version=VERSION,
        repository=repository,
        fingerprint=fingerprint,
        source_fingerprint=manifest["fingerprint"],
        manifest_checksum=checksum(manifest),
        record=record.name,
        verified=verified,
        evidence=evidence if verified else None,
    )
    value["checksum"] = checksum(value)
    path = location(state, repository, fingerprint)
    atomic_json(path, value)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

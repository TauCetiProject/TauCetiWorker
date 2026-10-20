"""Compact identities for explicitly registered, read-only installations.

The publisher must replace an installation/provenance token, never mutate it through a writable
alias. Read-only mount checks are an accidental-mutation guard, not a security boundary.
"""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path


def mount_identity(root: Path):
    """Include nested mount topology; a writable filesystem never qualifies for the fast path."""
    if not os.statvfs(root).f_flag & os.ST_RDONLY:
        return None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None  # Unsupported hosts retain complete inventory validation.
    selected = []
    for line in lines:
        fields = line.split()
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if root == mount or root in mount.parents or mount in root.parents:
            if root == mount or root in mount.parents:
                if not os.statvfs(mount).f_flag & os.ST_RDONLY:
                    return None
            selected.append(line)
    return selected


class Installations:
    def __init__(self, state, *, digest, atomic, read_json, files_under, file_record, stamps, cache_directory=None):
        self.state = state
        self.cache_directory = cache_directory
        self.digest, self.atomic, self.read_json = digest, atomic, read_json
        self.files_under, self.file_record, self.stamps = files_under, file_record, stamps
        self.validated = {}
        self.walked = set()

    def inventory(self, root):
        root = root.resolve()
        registration = os.environ.get("TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS")
        manifest = self.read_json(Path(registration)) if registration else {}
        provenance = manifest.get(str(root))
        mount = mount_identity(root) if isinstance(provenance, str) and provenance else None
        if mount is None:
            self.walked.add(root)
            return {str(p.relative_to(root)): self.file_record(p) for p in self.files_under(root)}
        identity = dict(root=str(root), provenance=provenance, mount=mount, stamp=self.stamps([root]))
        key = self.digest(identity)
        try:
            directory = self.cache_directory() if self.cache_directory else self.state / "verification-installations"
            cache = directory / f"{key}.json"
            directory.mkdir(parents=True, exist_ok=True)
            with cache.with_suffix(".lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self.validated_inventory(root, identity, key, cache)
        except (OSError, ValueError):
            # An optional cache cannot prevent verification. Retain full input validation.
            self.walked.add(root)
            return {str(p.relative_to(root)): self.file_record(p) for p in self.files_under(root)}

    def validated_inventory(self, root, identity, key, cache):
        old = self.read_json(cache)
        if (
            self.digest(old.get("identity")) != self.digest(identity)
            or not isinstance(old.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", old["sha256"])
            or type(old.get("files")) is not int
            or old["files"] < 0
        ):
            # An external symlink could change independently of this installation. Fall back rather
            # than attest a mutable target. Internal symlinks are bound to the same mount identity.
            files = list(self.files_under(root))
            if any(p.is_symlink() and not p.resolve().is_relative_to(root) for p in files):
                self.walked.add(root)
                return {str(p.relative_to(root)): self.file_record(p) for p in files}
            contents = {str(p.relative_to(root)): self.file_record(p) for p in files}
            old = dict(identity=identity, sha256=self.digest(contents), files=len(contents))
            self.atomic(cache, old)
        self.validated[root] = identity
        return dict(immutable_installation=key, sha256=old["sha256"], files=old["files"])

    def validate(self):
        for root, previous in self.validated.items():
            registration = os.environ.get("TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS")
            manifest = self.read_json(Path(registration)) if registration else {}
            current = dict(
                root=str(root),
                provenance=manifest.get(str(root)),
                mount=mount_identity(root),
                stamp=self.stamps([root]),
            )
            if current != previous:
                raise ValueError(f"immutable installation provenance changed: {root}")

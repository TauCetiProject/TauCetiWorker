"""Read private native queue policy through checked, pinned POSIX descriptors.

No symlink component is followed. The final parent must be private and every
ancestor must be owned by this user or root, without untrusted write access.
Root-owned sticky temporary ancestors are permitted; the final parent is not.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

MAX_POLICY_BYTES = 128 * 1024


class ControlPolicyError(ValueError):
    """Trusted queue-control configuration could not be established."""


def _directory(info, *, uid: int, private=False) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}:
        raise ControlPolicyError("queue control policy directories must have a trusted owner")
    if private:
        if info.st_uid != uid or info.st_mode & 0o077:
            raise ControlPolicyError("queue control policy needs a private user-owned parent directory")
    elif info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        raise ControlPolicyError("queue control policy ancestor is writable by untrusted users")


@contextlib.contextmanager
def _parent(path: Path, uid: int):
    if not path.is_absolute() or ".." in path.parts:
        raise ControlPolicyError("queue control policy path must be absolute without parent traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    try:
        for part in path.parts[1:-1]:
            _directory(os.fstat(directory), uid=uid)
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        _directory(os.fstat(directory), uid=uid, private=True)
        yield directory, path.name
    finally:
        os.close(directory)


def _file(info, *, uid: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
        or not 0 < info.st_size <= MAX_POLICY_BYTES
    ):
        raise ControlPolicyError("queue control policy must be one bounded private user-owned regular file")


def _snapshot(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_control_policy(path: Path) -> bytes:
    """Take one immutable startup snapshot, never reopen or cache by path.

    The caller supplies a trusted configuration path, never an HTTP/task field.
    Detect most in-place edits; timestamp checks alone cannot detect every
    same-size rewrite on a coarse filesystem. Atomically replace policy files
    atomically and restart with one newly constructed server/controller authority.
    Errors contain no private file bytes or fingerprints.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ControlPolicyError("native control policy loading requires POSIX descriptor checks")
    try:
        uid = os.geteuid()
        with _parent(Path(path), uid) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(descriptor)
                _file(before, uid=uid)
                # Check type before fdopen (FileIO can reject a directory
                # without taking ownership). Always close the raw descriptor.
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    payload = stream.read(MAX_POLICY_BYTES + 1)
                stream = None
                after = os.fstat(descriptor)
                _file(after, uid=uid)
                if _snapshot(before) != _snapshot(after) or len(payload) != before.st_size:
                    raise ControlPolicyError("queue control policy changed while it was read")
            finally:
                os.close(descriptor)
        if len(payload) > MAX_POLICY_BYTES:
            raise ControlPolicyError("queue control policy exceeds its byte bound")
        return payload
    except ControlPolicyError:
        payload = None
        raise
    except (OSError, TypeError, ValueError):
        payload = None
        raise ControlPolicyError("private queue control policy could not be read safely") from None

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
            old, directory = directory, child
            os.close(old)
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


def _read(parent: int, name: str, uid: int) -> bytes:
    """Read one checked private file relative to an already checked parent descriptor."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    payload = stream = None
    try:
        before = os.fstat(descriptor)
        _file(before, uid=uid)
        # Check type before fdopen (FileIO can reject a directory
        # without taking ownership). Always close the raw descriptor.
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_POLICY_BYTES + 1)
        after = os.fstat(descriptor)
        _file(after, uid=uid)
        if _snapshot(before) != _snapshot(after) or len(payload) != before.st_size:
            raise ControlPolicyError("queue control policy changed while it was read")
        return payload
    except BaseException:
        # A traceback must not retain private bytes in this frame's locals.
        payload = stream = None
        raise
    finally:
        os.close(descriptor)


def read_control_policy(path: Path) -> bytes:
    """Take one immutable startup snapshot, never reopen or cache by path.

    The caller supplies a trusted configuration path, never an HTTP/task field.
    Detect most in-place edits; timestamp checks alone cannot detect every
    same-size rewrite on a coarse filesystem. Replace policy files atomically
    and restart with one newly constructed server/controller authority.
    Errors contain no private file bytes or fingerprints.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ControlPolicyError("native control policy loading requires POSIX descriptor checks")
    try:
        uid = os.geteuid()
        with _parent(Path(path), uid) as (parent, name):
            return _read(parent, name, uid)
    except ControlPolicyError:
        raise
    except (OSError, TypeError, ValueError):
        raise ControlPolicyError("private queue control policy could not be read safely") from None


def read_systemd_credential(name: str) -> bytes:
    """Read one credential that systemd's ``LoadCredential=`` placed in ``$CREDENTIALS_DIRECTORY``.

    systemd creates that directory for this unit, so its ancestors are trusted without the
    ownership walk; inside a sandboxed user unit they appear owned by the overflow UID. The
    directory itself must still be private to this user, and the file must pass the same
    checks as read_control_policy. This trusts the process environment: whoever sets both
    ``CREDENTIALS_DIRECTORY`` and the matching caller path skips only the ancestor walk, which
    in a unit is systemd itself.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ControlPolicyError("native control policy loading requires POSIX descriptor checks")
    root = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not os.path.isabs(root) or ".." in Path(root).parts:
        raise ControlPolicyError("systemd credentials directory is unavailable")
    if not name or name in (".", "..") or "/" in name:
        raise ControlPolicyError("systemd credential name must be a plain file name")
    try:
        uid = os.geteuid()
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _directory(os.fstat(directory), uid=uid, private=True)
            return _read(directory, name, uid)
        finally:
            os.close(directory)
    except ControlPolicyError:
        raise
    except (OSError, TypeError, ValueError):
        raise ControlPolicyError("systemd credential could not be read safely") from None

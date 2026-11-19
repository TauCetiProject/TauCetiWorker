"""Common bounded cleanup for disposable read caches; locks and outboxes stay intact."""

import os
import re
import stat
import time


def prune_read_cache(root, *, max_files=384, max_bytes=16 * 1024 * 1024):
    """Bound disposable read data across credential scopes under one root.

    Only owned regular files with review-state or shared-read cache names in digest directories
    are eligible. Outboxes, unrelated files and symlinks are never followed or
    removed. Concurrent readers tolerate disappearing cache files. This is
    best-effort cache maintenance; it cannot affect a fresh observation's result.
    """
    if type(max_files) is not int or type(max_bytes) is not int or min(max_files, max_bytes) < 1:
        raise ValueError("read cache bounds must be positive integers")
    directories, files = [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return
    try:
        for directory in os.listdir(root_fd):
            if not re.fullmatch(r"[0-9a-f]{64}", directory):
                continue
            try:
                fd = os.open(directory, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    continue
                directories.append(directory)
                for name in os.listdir(fd):
                    if not re.fullmatch(
                        r"(?:[1-9][0-9]*(?:\.(?:key|contest))?|[0-9a-f]{64})(?:\.json|\.[0-9a-f]{32}\.tmp)", name
                    ):
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                        files.append((info.st_mtime_ns, directory, name, info.st_size, info.st_dev, info.st_ino))
            finally:
                os.close(fd)
        files.sort()
        count, size = len(files), sum(row[3] for row in files)
        for _, directory, name, length, device, inode in files:
            if count <= max_files and size <= max_bytes:
                break
            try:
                fd = os.open(directory, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (device, inode) or not stat.S_ISREG(info.st_mode):
                    continue  # Another writer replaced it; leave the newer value.
                os.unlink(name, dir_fd=fd)
                count -= 1
                size -= length
            except FileNotFoundError:
                count -= 1
                size -= length
            finally:
                os.close(fd)
        for directory in directories:
            try:
                # Another process may have just made an empty namespace and be
                # about to open it for an atomic write. Leave recent ones alone.
                if time.time() - os.stat(directory, dir_fd=root_fd, follow_symlinks=False).st_mtime < 30:
                    continue
                os.rmdir(directory, dir_fd=root_fd)
            except OSError:
                pass
    except OSError:
        return
    finally:
        os.close(root_fd)

"""One shallow Git fetch per host/TTL and immutable, read-only per-commit checkouts.

No GitHub REST calls are needed to list roadmap directories. Writable progress clones stay separate.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import github_transport as transport

_HOLDERS = {}


def root() -> Path:
    return Path(os.environ.get("TAUCETI_SHARED_REPOSITORIES_DIR", transport.root() / "repositories"))


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=120)


@contextlib.contextmanager
def borrowed_snapshot(repo, *, ttl=3600):
    """Hold a snapshot only while the caller reads it; process exit also releases it."""
    leases = []
    try:
        yield snapshot(repo, ttl=ttl, _leases=leases)
    finally:
        for lease in leases:
            lease.close()


def _version_lock(base, version):
    directory = base / "locks-v3"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return (directory / (version + ".lock")).open("a+")


def snapshot(repo: str, *, ttl: int = 3600, _leases=None) -> Path | None:
    """Return a process-lifetime path; bounded readers use borrowed_snapshot."""
    key = hashlib.sha256(repo.encode()).hexdigest()
    base = root() / key
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with transport.locked(base / "fetch.lock", timeout=120):
        import json

        try:
            state = json.loads((base / "state.json").read_text())
        except (OSError, ValueError):
            state = {}
        mirror = base / "mirror.git"
        if not mirror.exists():
            temp = base / ("mirror-" + uuid.uuid4().hex)
            result = _git("clone", "--bare", "--depth=1", "--", "https://github.com/" + repo, temp)
            if result.returncode:
                shutil.rmtree(temp, ignore_errors=True)
                return None
            temp.rename(mirror)
            _git("--git-dir", mirror, "config", "remote.origin.pushurl", "no_push")
        if not state.get("sha") or time.time() - state.get("fetched", 0) >= ttl:
            try:
                fetched = _git("--git-dir", mirror, "fetch", "--depth=1", "origin", "HEAD")
            except (OSError, subprocess.SubprocessError):
                fetched = subprocess.CompletedProcess([], 1)
            sha = state.get("sha")
            if not fetched.returncode or not sha:
                rev = _git("--git-dir", mirror, "rev-parse", "FETCH_HEAD" if not fetched.returncode else "HEAD")
                if rev.returncode:
                    return None
                sha = rev.stdout.strip()
            if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
                return None
            state = {**state, "sha": sha, "fetched": time.time() if fetched.returncode == 0 else 0}
            transport.atomic(base / "state.json", state)
        # A linked worktree's .git points outside the mounted tree and breaks inside Bubble.
        # Use a self-contained shallow Git copy; all workers still share one immutable version.
        # Old deployed collectors only enumerate snapshots-v2 and PID JSON leases.
        # Keep file-lock lifetimes separate during live upgrades. Lock inodes stay
        # outside snapshots and are never removed while callers can open them.
        target = base / "snapshots-v3" / state["sha"]
        if target.exists() and state.get("snapshot_v3_ready") != state["sha"]:
            # Validate trees published by older clients which cloned in place.
            head = _git("-C", target, "rev-parse", "HEAD")
            clean = _git("-C", target, "diff", "--quiet", "HEAD", "--")
            if head.returncode or head.stdout.strip() != state["sha"] or clean.returncode:
                for directory, _, _ in os.walk(target):
                    Path(directory).chmod(0o700)
                shutil.rmtree(target)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            _git("--git-dir", mirror, "update-ref", "refs/heads/snapshot", state["sha"])
            staging = target.with_name(".staging-" + uuid.uuid4().hex)
            try:
                result = _git("clone", "--no-local", "--depth=1", "--branch=snapshot", "--", mirror, staging)
                if result.returncode:
                    return None
                _git("-C", staging, "config", "remote.origin.url", "https://github.com/" + repo)
                _git("-C", staging, "config", "remote.origin.pushurl", "no_push")
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            for directory, _, files in os.walk(target, topdown=False):
                for name in files:
                    item = Path(directory) / name
                    if not item.is_symlink():
                        item.chmod(0o555 if item.stat().st_mode & 0o111 else 0o444)
                Path(directory).chmod(0o555)
        if state.get("snapshot_v3_ready") != state["sha"]:
            state["snapshot_v3_ready"] = state["sha"]
            transport.atomic(base / "state.json", state)
        reader = _version_lock(base, state["sha"])
        try:
            # Acquisition and collection are serialized by fetch.lock, so this
            # cannot wait on a cooperating collector or publisher.
            fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            reader.close()
            raise
        if _leases is not None:
            _leases.append(reader)
        else:
            held = _HOLDERS.setdefault(("snapshot", str(target)), reader)
            if held is not reader:
                reader.close()
        try:
            for old in target.parent.iterdir():
                if old.name == state["sha"] or time.time() - old.stat().st_mtime <= 86400:
                    continue
                with _version_lock(base, old.name) as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    for directory, _, _ in os.walk(old):
                        Path(directory).chmod(0o700)
                    shutil.rmtree(old)
                    Path(lease.name).unlink(missing_ok=True)
        except OSError as error:
            from .config import log

            log(f"shared repository housekeeping deferred: {error}")
        return target


def attach(repo: str, destination: Path, *, ttl: int) -> bool:
    leases = []
    link = None
    try:
        target = snapshot(repo, ttl=ttl, _leases=leases)
        if target is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        link = destination.with_name(destination.name + "." + uuid.uuid4().hex)
        link.symlink_to(target, target_is_directory=True)
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        link.replace(destination)
        # Retarget only at the destination owner's existing preparation boundary.
        # A failed attach leaves the previous destination's lease intact.
        previous = _HOLDERS.get(("attach", str(destination.absolute())))
        _HOLDERS[("attach", str(destination.absolute()))] = leases.pop()
        if previous is not None:
            previous.close()
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        for lease in leases:
            lease.close()
        if link is not None:
            link.unlink(missing_ok=True)

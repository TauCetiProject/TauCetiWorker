"""Small host-local caches used only by the Mathlib overlap phases.

API reads are token-scoped and complete before caching. Fresh reads bypass caches
before publication; a generation change invalidates peer cursors after a CAS.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

_fresh = contextvars.ContextVar("overlap_fresh", default=False)
_revision = contextvars.ContextVar("overlap_revision", default="")
_cache_hit = contextvars.ContextVar("overlap_cache_hit", default=False)


def root() -> Path:
    from .github_transport import root as shared_root

    return shared_root()


def scope(env=None) -> str | None:
    env = os.environ if env is None else env
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    # A login is not a permission boundary: two tokens of one account can see different repos.
    if not token:
        return None
    return hashlib.sha256((env.get("GH_HOST", "github.com") + "\0" + token).encode()).hexdigest()


@contextlib.contextmanager
def read_context(*, fresh: bool = False, revision: str = ""):
    a = _fresh.set(_fresh.get() or fresh)
    b = _revision.set(revision or _revision.get())
    c = _cache_hit.set(False)
    try:
        yield
    finally:
        _fresh.reset(a)
        _revision.reset(b)
        _cache_hit.reset(c)


def cache_hit():
    return _cache_hit.get()


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 30):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("shared GitHub lock wait expired") from None
                time.sleep(0.05)
        yield


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def generation() -> str:
    try:
        return (root() / "generation").read_text()
    except OSError:
        return ""


def invalidate():
    """Invalidate every credential's view, including peers with different permissions."""
    try:
        atomic(root() / "generation", uuid.uuid4().hex)
    except OSError:
        pass


def shared_read(key, fetch, *, ttl: float = 30, identity=None, immutable: bool = False):
    """Coalesce a complete logical read under a per-key lock. Never cache failures.

    Network/parser exceptions propagate; unavailable storage merely disables caching. Generation
    checks prevent an overlapping write from publishing a pre-write result after invalidation.
    """
    identity = identity or scope()
    if not identity or _fresh.get() or os.environ.get("TAUCETI_GITHUB_CACHE_BYPASS") == "1":
        return fetch()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    path = root() / "cache" / identity / (digest + ".json")
    try:
        cm = locked(path.with_suffix(".lock"))
        cm.__enter__()
    except OSError:
        return fetch()
    try:
        epoch = generation()
        try:
            cached = json.loads(path.read_text())
            if (
                (immutable or cached["generation"] == epoch)
                and cached.get("revision", "") == _revision.get()
                and 0 <= time.time() - cached["time"] < ttl
            ):
                _cache_hit.set(True)
                return cached["value"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        value = fetch()
        if value is not None and (immutable or generation() == epoch):
            try:
                atomic(path, {"time": time.time(), "generation": epoch, "revision": _revision.get(), "value": value})
            except OSError:
                pass
        return value
    finally:
        cm.__exit__(None, None, None)


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=120)


def _valid_snapshot(target: Path, sha: str, repo: str) -> bool:
    if not target.is_dir():
        return False
    result = _git("-C", target, "rev-parse", "--verify", "HEAD")
    if result.returncode or result.stdout.strip() != sha:
        return False
    # The overlap source consumer requires this manifest to freeze its Mathlib pin.
    return repo != "TauCetiProject/TauCeti" or (target / "lake-manifest.json").is_file()


def _remove_snapshot(target: Path):
    if target.exists():
        for directory, _, _ in os.walk(target):
            Path(directory).chmod(0o700)
        shutil.rmtree(target)


def snapshot(repo: str, *, ttl: int = 3600) -> Path | None:
    key = hashlib.sha256(repo.encode()).hexdigest()
    base = root() / "overlap" / "repositories" / key
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(base / "fetch.lock", timeout=120):
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
            fetched = _git("--git-dir", mirror, "fetch", "--depth=1", "origin", "HEAD")
            if fetched.returncode:
                # A bare mirror's HEAD can remain at its original clone while FETCH_HEAD
                # has advanced. Keep the last successful state; never rewind to HEAD.
                sha = state.get("sha")
                if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
                    return None
            else:
                rev = _git("--git-dir", mirror, "rev-parse", "FETCH_HEAD")
                if rev.returncode:
                    return None
                sha = rev.stdout.strip()
                if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
                    return None
                state = {"sha": sha, "fetched": time.time()}
                atomic(base / "state.json", state)
        # A linked worktree's .git points outside the mounted tree and breaks inside Bubble.
        # Use a self-contained shallow Git copy; all workers still share one immutable version.
        target = base / "snapshots-v2" / state["sha"]
        if not _valid_snapshot(target, state["sha"], repo):
            # A pre-atomic implementation may have left a partial directory.
            _remove_snapshot(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
            try:
                _git("--git-dir", mirror, "update-ref", "refs/heads/snapshot", state["sha"])
                result = _git("clone", "--no-local", "--depth=1", "--branch=snapshot", "--", mirror, temporary)
                if result.returncode or not _valid_snapshot(temporary, state["sha"], repo):
                    return None
                for name, value in (
                    ("remote.origin.url", "https://github.com/" + repo),
                    ("remote.origin.pushurl", "no_push"),
                ):
                    if _git("-C", temporary, "config", name, value).returncode:
                        return None
                for directory, _, files in os.walk(temporary, topdown=False):
                    for name in files:
                        item = Path(directory) / name
                        if not item.is_symlink():
                            item.chmod(0o555 if item.stat().st_mode & 0o111 else 0o444)
                    Path(directory).chmod(0o555)
                temporary.rename(target)
            finally:
                _remove_snapshot(temporary)
        # Process leases pin old versions across a refresh. GC only removes unpinned day-old trees.
        atomic(base / "leases" / (str(os.getpid()) + ".json"), {"sha": state["sha"], "time": time.time()})
        pins = {state["sha"]}
        for lease in (base / "leases").glob("*.json"):
            try:
                os.kill(int(lease.stem), 0)
                pins.add(json.loads(lease.read_text())["sha"])
            except (ProcessLookupError, ValueError, OSError):
                lease.unlink(missing_ok=True)
        for old in target.parent.iterdir():
            if old.name not in pins and time.time() - old.stat().st_mtime > 86400:
                for directory, _, _ in os.walk(old):
                    Path(directory).chmod(0o700)
                shutil.rmtree(old)
        return target

"""One shallow Git fetch per host/TTL and immutable, read-only per-commit checkouts.

No GitHub REST calls are needed to list roadmap directories. Writable progress clones stay separate.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import github_transport as transport


def root() -> Path:
    return Path(os.environ.get("TAUCETI_SHARED_REPOSITORIES_DIR", transport.root() / "repositories"))


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=120)


def snapshot(repo: str, *, ttl: int = 3600) -> Path | None:
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
        target = base / "snapshots-v2" / state["sha"]
        if target.exists() and state.get("snapshot_ready") != state["sha"]:
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
        if state.get("snapshot_ready") != state["sha"]:
            state["snapshot_ready"] = state["sha"]
            transport.atomic(base / "state.json", state)
        # Process leases pin old versions across a refresh. GC only removes unpinned day-old trees.
        transport.atomic(base / "leases" / (str(os.getpid()) + ".json"), {"sha": state["sha"], "time": time.time()})
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


def attach(repo: str, destination: Path, *, ttl: int) -> bool:
    try:
        target = snapshot(repo, ttl=ttl)
        if target is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        link = destination.with_name(destination.name + "." + uuid.uuid4().hex)
        link.symlink_to(target, target_is_directory=True)
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        link.replace(destination)
        return True
    except (OSError, subprocess.SubprocessError):
        return False

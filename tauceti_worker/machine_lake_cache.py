"""Native Lake sharing, with private fallback and operator-selected reclamation targets."""

from __future__ import annotations

import math
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .runtime_status import atomic_json, read_json

_FALSE = {"0", "false", "off", "no", "n", "f"}
_IDENTITY = re.compile(r"[0-9a-f]{16,64}")
_ARTIFACT = re.compile(r"[0-9a-f]{16}\.[A-Za-z0-9_.-]+")
_TEMPORARY = re.compile(r"\.lake-cache-([0-9]+)-[0-9-]+\.tmp")


def root(env=None, *, home=None):
    env = os.environ if env is None else env
    home = Path(pwd.getpwuid(os.getuid()).pw_dir) if home is None else Path(home)
    value = env.get("TAUCETI_MACHINE_LAKE_CACHE_ROOT")
    if value:
        return home / value[2:] if value.startswith("~/") else Path(value)
    return home / ".cache/tauceti/lake-native"


def router_root(env=None, *, home=None):
    env = os.environ if env is None else env
    home = Path(pwd.getpwuid(os.getuid()).pw_dir) if home is None else Path(home)
    value = env.get("TAUCETI_MACHINE_LAKE_RUNTIME_ROOT")
    if value:
        return home / value[2:] if value.startswith("~/") else Path(value)
    return home / ".local/share/tauceti/native-lake"


def initialize(path):
    """Recognize a native store recreated by a running Lake after operator removal."""
    from .cache_storage import initialize as initialize_empty

    try:
        initialize_empty(path)
        return
    except ValueError:
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise
        if (path / "owner.json").exists() or (path / "owner.json").is_symlink():
            raise
        for entry in path.iterdir():
            if entry.name.startswith(".owner.json."):
                continue
            if entry.name != "toolchains" or entry.is_symlink() or not entry.is_dir():
                raise
            if entry.stat().st_uid != os.getuid():
                raise
            for namespace in entry.iterdir():
                if (
                    not _IDENTITY.fullmatch(namespace.name)
                    or namespace.is_symlink()
                    or not namespace.is_dir()
                    or namespace.stat().st_uid != os.getuid()
                ):
                    raise
        atomic_json(path / "owner.json", dict(version=1, uid=os.getuid()))


def configure(home, data_home, env):
    """Only fixed Lake understands the opt-in; old executables keep the private path."""
    private = str(Path(data_home) / ".cache/lake")
    if "LAKE_CACHE_DIR" in env and env["LAKE_CACHE_DIR"] != private:
        env.pop("LAKE_SHARED_CACHE_ROOT", None)
        env.pop("LAKE_SHARED_CACHE_FALLBACK", None)
        return
    env.setdefault("LAKE_CACHE_DIR", private)
    if env.get("TAUCETI_MACHINE_LAKE_CACHE", "1").lower() in _FALSE:
        env.pop("LAKE_SHARED_CACHE_ROOT", None)
        env.pop("LAKE_SHARED_CACHE_FALLBACK", None)
        return
    if env.get("LAKE_SHARED_CACHE_ROOT") == "":
        return
    try:
        path = root(env, home=home)
        if not path.is_absolute():
            return
        if env.get("LAKE_SHARED_CACHE_ROOT", str(path)) == str(path):
            initialize(path)
        env.setdefault("LAKE_SHARED_CACHE_ROOT", str(path))
        env["LAKE_SHARED_CACHE_FALLBACK"] = private
        router = router_root(env, home=home) / "bin"
        if (router / "lake").is_file() and str(router) not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = str(router) + os.pathsep + env.get("PATH", os.defpath)
    except (OSError, ValueError):
        env.pop("LAKE_SHARED_CACHE_ROOT", None)
        env.pop("LAKE_SHARED_CACHE_FALLBACK", None)


def selected(cwd, env, *, deadline=None):
    """Query the actual invocation without loading a project's configuration."""
    raw = env.get("LAKE_SHARED_CACHE_ROOT")
    if not raw:
        return False
    try:
        from . import processes
        from .agents import HOST_CACHE_TIMEOUT

        remaining = HOST_CACHE_TIMEOUT if deadline is None else max(0, deadline - time.monotonic())
        result = processes.run(
            ["lake", "--shared-cache-dir"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=remaining,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        selected_path = Path(result.stdout.strip())
        expected_root = Path(raw)
        if not selected_path.is_absolute():
            selected_path = Path(cwd) / selected_path
        if not expected_root.is_absolute():
            expected_root = Path(cwd) / expected_root
        return selected_path.resolve().is_relative_to((expected_root / "toolchains").resolve())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return False


def _option(env, name, scale):
    raw = env.get(name)
    if raw is None or raw == "":
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return value * scale


def _files(path):
    """Only native payload/index directories, without following directory aliases."""
    toolchains = path / "toolchains"
    if toolchains.is_symlink() or not toolchains.is_dir():
        return
    for entry in toolchains.iterdir():
        if not _IDENTITY.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        for name in ("artifacts", "outputs", "revisions"):
            base = entry / name
            if base.is_symlink() or not base.is_dir():
                continue
            for directory, dirs, files in os.walk(base, followlinks=False):
                parent = Path(directory)
                dirs[:] = [part for part in dirs if not (parent / part).is_symlink()]
                for filename in files:
                    candidate = parent / filename
                    try:
                        info = candidate.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                        yield candidate, info, name


def _owner_exited(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError, ValueError):
        return False
    return False


def collect(*, env=None, dry=False, now=None):
    """Reclaim unpinned files only for requested targets; never enforce admissions."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    path = root(env)
    result = dict(
        root=str(path),
        max_bytes=None,
        max_age_seconds=None,
        retained_bytes=None,
        pinned_bytes=None,
        removed_files=0,
        eligible_files=0,
        removed_allocated_bytes=0,
        observed_free_change_bytes=0,
        errors=[],
        dry_run=dry,
    )
    try:
        maximum = _option(env, "TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB", 1024**3)
        age = _option(env, "TAUCETI_MACHINE_LAKE_CACHE_MAX_AGE_HOURS", 3600)
    except ValueError as error:
        result.update(reason="invalid optional retention setting; leaving cache unchanged", errors=[str(error)])
        return result
    result.update(max_bytes=maximum, max_age_seconds=age)
    if path.is_symlink() or read_json(path / "owner.json") != dict(version=1, uid=os.getuid()):
        result["reason"] = "absent or unmanaged cache; leaving it unchanged"
        return result
    staging_only = maximum is None and age is None
    try:
        free_before = shutil.disk_usage(path).free
        result.update(retained_bytes=0, pinned_bytes=0)
        seen = set()
        candidates = []
        for candidate, info, kind in _files(path):
            identity = info.st_dev, info.st_ino
            if identity not in seen:
                result["retained_bytes"] += info.st_blocks * 512
                if info.st_nlink > 1:
                    result["pinned_bytes"] += info.st_blocks * 512
                seen.add(identity)
            temporary = _TEMPORARY.fullmatch(candidate.name)
            if temporary:
                eligible = _owner_exited(int(temporary[1]))
            elif staging_only:
                eligible = False
            elif kind == "artifacts":
                eligible = bool(_ARTIFACT.fullmatch(candidate.name)) and info.st_nlink == 1
            else:
                eligible = candidate.suffix in (".json", ".jsonl") and info.st_nlink == 1
            if eligible:
                candidates.append((info.st_ctime, candidate, info, temporary is not None))
        remaining = result["retained_bytes"] or 0
        for changed, candidate, original, temporary in sorted(candidates, key=lambda row: row[0]):
            expired = age is not None and now - changed >= age
            pressured = maximum is not None and remaining > maximum
            if not temporary and not expired and not pressured:
                continue
            try:
                current = candidate.lstat()
                if (current.st_dev, current.st_ino, current.st_ctime_ns, current.st_nlink) != (
                    original.st_dev,
                    original.st_ino,
                    original.st_ctime_ns,
                    original.st_nlink,
                ):
                    continue
                result["eligible_files"] += 1
                amount = current.st_blocks * 512
                if not dry:
                    candidate.unlink()
                    result["removed_files"] += 1
                    result["removed_allocated_bytes"] += amount
                remaining -= amount
            except FileNotFoundError:
                continue  # Another native writer or collector changed an optional entry.
            except OSError as error:
                result["errors"].append(str(error))
        if not dry:
            result["retained_bytes"] = max(0, remaining)
        result["observed_free_change_bytes"] = shutil.disk_usage(path).free - free_before
        result["reason"] = (
            "no retention targets selected; abandoned staging checked"
            if staging_only
            else "requested targets processed; active links may retain excess bytes"
        )
    except OSError as error:
        result["errors"].append(str(error))
        result["reason"] = "optional collection unavailable; leaving remaining entries alone"
    return result

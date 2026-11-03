#!/usr/bin/env python3
"""Wait for spare RAM/CPU before execing Lean. Standalone, also bundled by TauCetiFleet."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Measured on 2026-11-03: all 23,622 TauCeti files at 10418ac7ac, Lean 4.36.0-rc2.
# Worst: RepresentationTheory/CharacterTable/Dixon/ClassData/Alternating/Five.lean.
# See docs/evidence/lean-peak-rss-2026-11-03.json for the full measurement.
WORST_FILE_RSS_BYTES = 9_431_461_888
MEMORY_FLOOR_PERCENT = 60
CPU_LIMIT_PERCENT = 90
MARKER = ".tauceti-lean.json"


def memory():
    """Available host RAM, bounded by enclosing cgroups' reclaimable headroom."""
    fields = {
        k: int(v.split()[0]) * 1024
        for k, v in (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    }
    total, available = fields["MemTotal"], fields["MemAvailable"]
    relative = Path("/proc/self/cgroup").read_text().split("0::", 1)[1].strip()
    root = Path("/sys/fs/cgroup")
    group = root / relative.lstrip("/")
    while group.is_relative_to(root):
        maximum = group / "memory.max"
        if maximum.exists() and (value := maximum.read_text().strip()) != "max":
            limit = int(value)
            stats = dict(line.split() for line in (group / "memory.stat").read_text().splitlines())
            # Active clean file pages are reclaimable too. Counting them as
            # permanently used could stall every waiter on a warm artifact cache
            # until a new allocation triggers reclaim, which none would start.
            reclaimable = max(
                0,
                int(stats["file"])
                - int(stats.get("shmem", 0))
                - int(stats.get("file_dirty", 0))
                - int(stats.get("file_writeback", 0)),
            )
            used = int((group / "memory.current").read_text()) - reclaimable
            total, available = min(total, limit), min(available, max(0, limit - used))
        group = group.parent
    if total <= 0:
        raise ValueError("memory capacity is unavailable")
    return total, min(total, available)


def memory_limit(total):
    if WORST_FILE_RSS_BYTES <= 0:
        raise ValueError("worst-file RSS measurement has not been installed")
    # Round down and leave an extra percentage point: strictly more than two files.
    return max(MEMORY_FLOOR_PERCENT, math.floor(100 - 200 * WORST_FILE_RSS_BYTES / total) - 1)


def cpu_sample():
    # guest/guest_nice are already included in user/nice. Do not count them twice.
    values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def cpu_percent(before, after):
    elapsed = after[0] - before[0]
    if elapsed <= 0:
        raise ValueError("CPU counters did not advance")
    return max(0, min(100, 100 * (1 - (after[1] - before[1]) / elapsed)))


def wait_for_capacity():
    before = cpu_sample()
    time.sleep(random.uniform(0.5, 1.5))
    waiting = False
    # Successful compiler output is a build failure under `lake build --iofail`.
    debug = os.environ.get("TAUCETI_LEAN_PRESSURE_DEBUG") == "1"
    while True:
        after = cpu_sample()
        cpu = cpu_percent(before, after)
        total, available = memory()
        used = 100 * (1 - available / total)
        limit = memory_limit(total)
        if cpu <= CPU_LIMIT_PERCENT and used <= limit:
            if waiting and debug:
                print("tauceti-lean: capacity available; starting Lean", file=sys.stderr, flush=True)
            return
        if not waiting and debug:
            print(
                f"tauceti-lean: waiting (CPU {cpu:.1f}% / {CPU_LIMIT_PERCENT}%, "
                f"memory {used:.1f}% / {limit}%); retrying with random 1–5s sleeps",
                file=sys.stderr,
                flush=True,
            )
        waiting = True
        before = after
        time.sleep(random.uniform(1, 5))


def real_lean():
    # An overlay invocation knows its real compiler even after `lake env` adds its
    # bin directory to PATH. Never ask that PATH's `lean` and recurse into ourselves.
    marker = Path(sys.argv[0]).resolve().parent.parent / MARKER
    if marker.is_file():
        return Path(json.loads(marker.read_text())["lean"])
    clean = dict(os.environ)
    clean.pop("LEAN_SYSROOT", None)
    if elan := shutil.which("elan"):
        result = subprocess.run([elan, "which", "lean"], env=clean, text=True, capture_output=True, check=True)
        binary = result.stdout.strip()
    else:
        binary = None
        source = Path(__file__).read_bytes()
        for directory in os.get_exec_path():
            candidate = Path(directory) / "lean"
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            marker = candidate.resolve().parent.parent / MARKER
            if marker.is_file():
                return Path(json.loads(marker.read_text())["lean"])
            # The `lean` PATH alias may be a wheel-materialized copy of this
            # helper rather than a symlink. Skip both forms without recursion.
            if candidate.stat().st_size == len(source) and candidate.read_bytes() == source:
                continue
            binary = str(candidate)
            break
    if not binary or Path(binary).resolve() == Path(__file__).resolve():
        raise ValueError("cannot find the real Lean compiler (install elan or put Lean on PATH)")
    binary = Path(binary).resolve()
    marker = binary.parent.parent / MARKER
    if marker.is_file():
        return Path(json.loads(marker.read_text())["lean"])
    return binary


def install_toolchain(binary):
    """Wrap an installed CI compiler, including calls from env-clearing sandboxes."""
    binary = Path(binary).absolute()
    root = binary.parent.parent
    source = Path(__file__).read_bytes()
    with (root / ".tauceti-lean.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        real = binary.with_name("lean-unthrottled")
        current = binary.read_bytes()
        if real.exists() and (root / MARKER).is_file() and current == source:
            return
        wrapped = current.startswith(source.split(b"\n\n", 1)[0])
        if not real.exists() and wrapped:
            raise ValueError(f"wrapped compiler has no saved original: {binary}")
        if not real.exists():
            # Keep bin/lean present throughout a live installation.
            os.link(binary, real)
        elif not wrapped:
            # Elan or a local Lean build can replace the compiler in the same
            # prefix. Preserve that new compiler instead of restoring an old one.
            fd, temporary = tempfile.mkstemp(dir=binary.parent)
            os.close(fd)
            Path(temporary).unlink()
            try:
                os.link(binary, temporary)
                os.replace(temporary, real)
            finally:
                Path(temporary).unlink(missing_ok=True)
        fd, temporary = tempfile.mkstemp(dir=root)
        with os.fdopen(fd, "w") as stream:
            json.dump(dict(lean=str(real), installed=True), stream)
        os.replace(temporary, root / MARKER)
        fd, temporary = tempfile.mkstemp(dir=binary.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(source)
            os.chmod(temporary, 0o755)
            os.replace(temporary, binary)
        finally:
            Path(temporary).unlink(missing_ok=True)


def install_available():
    """One interpreter per shell startup, even when many toolchains are cached."""
    home = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
    binaries = {p.resolve() for p in (home / "toolchains").glob("*/bin/lean") if p.is_file()}
    if elan := shutil.which("elan"):
        selected = subprocess.run([elan, "which", "lean"], text=True, capture_output=True)
        if selected.returncode == 0:
            binaries.add(Path(selected.stdout.strip()).resolve())
    for binary in sorted(binaries):
        install_toolchain(binary)


def overlay(lean):
    """Lake invokes <prefix>/bin/lean, so LEAN alone cannot wrap compilation."""
    source = Path(__file__).resolve()
    # Include the helper identity so upgrades never redirect another running build.
    key = hashlib.sha256(str(lean).encode() + source.read_bytes()).hexdigest()[:24]
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tauceti/lean-pressure"
    base.mkdir(parents=True, exist_ok=True)
    target = base / key
    with (base / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            with tempfile.TemporaryDirectory(dir=base) as temporary:
                work = Path(temporary) / "toolchain"
                work.mkdir()
                original = lean.parent.parent
                for path in original.iterdir():
                    if path.name not in {"bin", MARKER, ".tauceti-lean.lock"}:
                        (work / path.name).symlink_to(path, target_is_directory=path.is_dir())
                (work / "bin").mkdir()
                for path in lean.parent.iterdir():
                    if path.name != "lean":
                        (work / "bin" / path.name).symlink_to(path)
                # Copy the standalone helper so already-running environments survive
                # a wheel/checkout upgrade or deletion of its original source path.
                shutil.copyfile(source, work / "bin/lean")
                (work / "bin/lean").chmod(0o755)
                (work / MARKER).write_text(json.dumps(dict(lean=str(lean))))
                work.rename(target)
    return target


def main(args=None):
    args = sys.argv[1:] if args is None else args
    try:
        if sys.platform != "linux":
            # scripts/ is also on macOS agents' PATH for the other helpers.
            # Keep the lean alias transparent where /proc observations do not exist.
            lean = real_lean()
            os.execv(str(lean), [str(lean), *args])
        if args == ["--install-available"]:
            install_available()
            return 0
        if len(args) == 2 and args[0] == "--install-toolchain":
            install_toolchain(args[1])
            return 0
        lean = real_lean()
        if args == ["--print-prefix"]:
            marker = Path(sys.argv[0]).resolve().parent.parent / MARKER
            print(marker.parent if marker.is_file() else overlay(lean))
            return 0
        # Lake probes these during discovery. Keep them fast and stdout pristine.
        if args not in (["--githash"], ["--version"], ["-v"], ["--help"], ["-h"], ["--print-libdir"]):
            wait_for_capacity()
        os.execv(str(lean), [str(lean), *args])
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        print(f"tauceti-lean: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

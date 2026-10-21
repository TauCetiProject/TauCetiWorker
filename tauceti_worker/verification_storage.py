"""Host-local storage observation and bounded, restartable cache reclamation.

Source, Git history, dependency sources and receipts never expire. Only untracked Lake build
directories are disposable. Size targets trigger idle-cache cleanup, never work admission or
cancellation. Only actual filesystem headroom can defer or interrupt verification.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

from .runtime_status import atomic_json, read_json

GIB = 1024**3
FLOOR = 16 * 1024
_SCANS = {}
_CLEANUP_SETTINGS = {
    "STORAGE_BYTES": 128 * GIB,
    "CANDIDATE_BYTES": 32 * GIB,
    "COMPLETED_CACHE_SECONDS": 900,
    "IDLE_CACHE_SECONDS": 21600,
    "OWNERSHIP_SECONDS": 2,
}


class Pressure(Exception):
    """Retain the source/job and retry after ordinary storage housekeeping."""

    def __init__(self, message, *, reason="capacity"):
        super().__init__(message)
        self.reason = reason


class Limit(Pressure):
    """Legacy result type retained for callers installed before cleanup-only targets."""

    def __init__(self, message):
        super().__init__(message, reason="limit")


class Configuration(Pressure):
    def __init__(self, message):
        super().__init__(message, reason="configuration")


def setting(name, default):
    try:
        value = int(os.environ.get("TAUCETI_VERIFY_" + name, default))
    except ValueError:
        if name in _CLEANUP_SETTINGS:
            return default
        raise Configuration(f"TAUCETI_VERIFY_{name} must be a positive integer") from None
    if value <= 0:
        if name in _CLEANUP_SETTINGS:
            return default
        raise Configuration(f"TAUCETI_VERIFY_{name} must be a positive integer")
    return value


def validate_settings():
    for name, default in (
        ("STORAGE_BYTES", 128 * GIB),
        ("CANDIDATE_BYTES", 32 * GIB),
        ("MIN_FREE_BYTES", 20 * GIB),
        ("OWNERSHIP_SECONDS", 2),
        ("COMPLETED_CACHE_SECONDS", 900),
        ("IDLE_CACHE_SECONDS", 21600),
    ):
        setting(name, default)
    try:
        percent = int(os.environ.get("TAUCETI_VERIFY_MIN_FREE_PERCENT", "10"))
    except ValueError:
        raise Configuration("TAUCETI_VERIFY_MIN_FREE_PERCENT must be an integer from 0 through 99") from None
    if not 0 <= percent < 100:
        raise Configuration("TAUCETI_VERIFY_MIN_FREE_PERCENT must be an integer from 0 through 99")


def root():
    # Agent HOME overrides must not split one user's host budget into per-worker budgets.
    if value := os.environ.get("TAUCETI_VERIFY_STORAGE_DIR"):
        return Path(value)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        import socket

        machine = socket.gethostname()
    return home / ".local/state/tauceti/verification-storage" / hashlib.sha256(machine.encode()).hexdigest()[:16]


@contextlib.contextmanager
def _ledger_path():
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a+") as lock:
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Pressure(
                        "verification storage accounting is busy; retry after housekeeping", reason="busy"
                    ) from None
                time.sleep(0.005)
        yield directory / "candidates.json"


def _read_ledger(path):
    if not path.exists():
        return dict(version=1, candidates={})
    data = read_json(path)
    if data.get("version") != 1 or not isinstance(data.get("candidates"), dict):
        raise Pressure("verification storage ledger is unreadable", reason="accounting")
    for identity, row in data["candidates"].items():
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("state"), str)
            or not Path(row["state"]).is_absolute()
            or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}" if row.get("kind") == "checkpoint" else r"[0-9a-f]{32}", row["id"])
            or identity != stored_key(row["state"], row["id"])
            or type(row.get("reserved")) is not int
            or row["reserved"] <= 0
            or type(row.get("measured", 0)) is not int
            or row.get("measured", 0) < 0
            or not isinstance(row.get("last_used"), (float, int))
            or not math.isfinite(row["last_used"])
        ):
            raise Pressure("verification storage ledger has invalid ownership/accounting metadata", reason="accounting")
    return data


@contextlib.contextmanager
def ledger(*, write=True):
    with _ledger_path() as path:
        data = _read_ledger(path)
        before = json.dumps(data) if write else None
        yield data
        if write and (not path.exists() or json.dumps(data) != before):
            atomic_json(path, data)


def recover(states):
    """A corrupt disposable index never requires deleting source or human marker repair."""
    with _ledger_path() as path:
        try:
            _read_ledger(path)
        except Pressure as error:
            if error.reason != "accounting":
                raise
            path.rename(path.with_name("candidates.corrupt-" + uuid.uuid4().hex + ".json"))
            atomic_json(path, dict(version=1, candidates={}))
    for state in states:
        discover_family(state)
        discover(state)


def prepare(state):
    try:
        discover_family(state)
        discover(state)
    except Pressure as error:
        if error.reason != "accounting":
            raise
        recover((state,))


def canonical_state(state):
    if str(state.resolve()) == "/opt/verification-state" and os.environ.get("TAUCETI_VERIFY_HOST_STATE"):
        return Path(os.environ["TAUCETI_VERIFY_HOST_STATE"])
    return state.resolve()


def key(state, identifier):
    return stored_key(str(canonical_state(state)), identifier)


def stored_key(state, identifier):
    return hashlib.sha256(state.encode()).hexdigest() + ":" + identifier


def candidate(state, identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid verification candidate identifier")
    state = state.resolve()
    parent = state / "verification-candidates"
    target = parent / identifier
    if parent.is_symlink() or target.is_symlink():
        raise ValueError("verification candidate path is a symlink")
    return target


def free_space(path, *, additional=0):
    while not path.exists():
        path = path.parent
    usage = shutil.disk_usage(path)
    try:
        percent = int(os.environ.get("TAUCETI_VERIFY_MIN_FREE_PERCENT", "10"))
    except ValueError:
        raise Configuration("TAUCETI_VERIFY_MIN_FREE_PERCENT must be an integer from 0 through 99") from None
    if not 0 <= percent < 100:
        raise Configuration("TAUCETI_VERIFY_MIN_FREE_PERCENT must be an integer from 0 through 99")
    reserve = max(setting("MIN_FREE_BYTES", 20 * GIB), usage.total * percent // 100)
    if usage.free - additional < reserve:
        raise Pressure(
            f"verification disk reserve: {usage.free} bytes free; need {reserve + additional}",
            reason="filesystem-headroom",
        )
    return usage


def discover(state):
    """Shallow discovery observes mixed-runtime candidates without recursive inventory walks."""
    state = state.resolve()
    parent = state / "verification-candidates"
    if parent.is_symlink():
        raise ValueError("verification candidate parent is a symlink")
    checkpoints = state / "verification-checkpoints"
    if checkpoints.is_symlink():
        raise ValueError("verification checkpoint parent is a symlink")
    if not parent.is_dir() and not checkpoints.is_dir():
        return
    with ledger() as data:
        for directory, kind, length in ((parent, "candidate", 32), (checkpoints, "checkpoint", 64)):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not re.fullmatch(r"[0-9a-f]{%d}" % length, path.name) or path.is_symlink() or not path.is_dir():
                    continue
                row = data["candidates"].setdefault(
                    key(state, path.name),
                    dict(
                        state=str(canonical_state(state)),
                        id=path.name,
                        kind=kind,
                        reserved=FLOOR,
                        last_used=path.stat().st_mtime,
                        legacy=True,
                    ),
                )
                if kind == "checkpoint" and row.get("legacy") and (path / "checkpoint.json").is_file():
                    # Completed checkpoints have a tiny, fixed layout. A metadata-only bounded
                    # migration replaces historical estimates without reading source payloads.
                    size = 0
                    with contextlib.closing(_sizes(path)) as inventory:
                        for index, amount in enumerate(inventory):
                            size += amount
                            if index >= 63:
                                break
                        else:
                            row.update(
                                reserved=max(FLOOR, size), measured=size, compact=True, settled=True, legacy=False
                            )


def discover_family(state):
    """Account legacy sibling workers before admitting a new candidate, without tree walks."""
    if str(state.resolve()) == "/opt/verification-state":
        discover(state)
        return  # A bubble sees only its mounted state, never the host's worker siblings.
    base = state.parent.parent if state.name == "verification-state" else state.parent
    for sibling in base.iterdir():
        if sibling.is_symlink() or not sibling.is_dir():
            continue
        for member in (sibling, sibling / "verification-state"):
            if member.resolve() == state.resolve():
                discover(member)
            else:
                try:
                    discover(member)
                except (OSError, ValueError):
                    continue  # Unsupported sibling aliases never retire this worker's job.


def reserve(state, identifier, *, source_only=False):
    """Register activity without preallocating a candidate's cleanup target.

    Keep the helper's existing call signature for installed callers. Neither the returned
    target nor source_only grants or withholds permission to consume logical bytes.
    """
    validate_settings()
    target = candidate(state, identifier)
    if _has_removal(state, identifier):
        raise Pressure("candidate cache removal is incomplete; supervisor must finish its owned cleanup", reason="busy")
    _register(state, identifier, target, 0, "candidate")
    return setting("CANDIDATE_BYTES", 32 * GIB)


def _has_removal(state, identifier):
    progress = read_json(state / "verification-storage" / f"{identifier}.json")
    return any(isinstance(value, dict) and "stack" in value for value in progress.values())


def reserve_checkpoint(state, identifier, *, amount=None):
    validate_settings()
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid verification checkpoint identifier")
    target = state / "verification-checkpoints" / identifier
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("verification checkpoint path is a symlink")
    _register(state, identifier, target, amount or 0, "checkpoint")
    return amount or 0


def _register(state, identifier, target, amount, kind):
    free_space(target.parent)
    try:
        prepare(state)
        _reserve(state, identifier, target, amount, kind)
    except Pressure as error:
        if error.reason not in {"busy", "accounting"}:
            raise
        # The observation ledger is optional for verification. Source/job/receipt leases
        # still own cleanup safety, and ordinary discovery reconstructs missing records.
    except OSError:
        pass  # Optional observation state cannot refuse otherwise valid verification.


def _reserve(state, identifier, target, amount, kind):
    with ledger() as data:
        entries = data["candidates"]
        identity = key(state, identifier)
        old = entries.get(identity, {})
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        device = parent.stat().st_dev
        measured = old.get("measured", 0)
        try:
            free_space(target.parent)
        except Pressure:
            with contextlib.suppress(OSError):
                atomic_json(
                    root() / "pressure.json",
                    dict(at=time.time(), kind=kind, requested=amount, reason="filesystem-headroom"),
                )
            raise
        from .work_recovery import process_owner

        owner = None if os.environ.get("TAUCETI_VERIFY_BUBBLE") == "1" else process_owner()
        entries[identity] = dict(
            state=str(canonical_state(state)),
            id=identifier,
            kind=kind,
            reserved=max(FLOOR, measured, amount),
            last_used=time.time(),
            compact=kind == "checkpoint",
            settled=False,
            measured=measured,
            device=device,
            growing=True,
            owner=owner,
        )
    return amount


def settle(state, identifier, size, *, compact=False, running=False):
    """Store measured bytes for cleanup, never a speculative growth reservation."""
    with ledger() as data:
        row = data["candidates"].get(key(state, identifier))
        if row is not None:
            row.update(
                reserved=max(FLOOR, size),
                measured=size,
                compact=compact,
                legacy=False,
                settled=True,
                growing=running,
                overflow=row.get("kind") != "checkpoint" and size > setting("CANDIDATE_BYTES", 32 * GIB),
            )
            row.pop("reclamation_blocked", None)
            if not running:
                row["last_used"] = time.time()


def observed(row):
    # Older live runtimes also write non-legacy rows with speculative reserved=32 GiB.
    # Only measured payloads count, regardless of which runtime last touched the row.
    return max(FLOOR, row.get("measured", 0))


def release(state, identifier):
    with ledger() as data:
        data["candidates"].pop(key(state, identifier), None)


def cache_generation(state, tree):
    if tree.parent == state.resolve() / "verification-candidates" and re.fullmatch(r"[0-9a-f]{32}", tree.name):
        return read_json(state / "verification-storage-generations" / f"{tree.name}.json").get("generation")
    return None


class SpaceWriter:
    def __init__(self, stream, target):
        self.stream, self.target, self.next_check = stream, target, 0

    def tell(self):
        return self.stream.tell()

    def write(self, data):
        if time.monotonic() >= self.next_check:
            free_space(self.target, additional=len(data))
            self.next_check = time.monotonic() + 1
        return self.stream.write(data)


class CopyBudget:
    """Observe logical copies and real free space; legacy limit argument is advisory only."""

    def __init__(self, limit, target):
        self.target, self.bytes, self.next_check = target, 0, 0

    def charge(self, path):
        info = path.stat()
        if stat.S_ISREG(info.st_mode):
            self.bytes += info.st_size
        if time.monotonic() >= self.next_check:
            free_space(self.target)
            self.next_check = time.monotonic() + 1


def _sizes(tree):
    """Metadata only, with no directory FD held across collection ticks.

    os.walk closes each scandir before yielding names. Retaining an open scandir across ticks
    would make another collector mistake this read-only observer for a live workspace user.
    """
    device = tree.stat().st_dev

    def failed(error):
        if not isinstance(error, FileNotFoundError):
            raise error

    for directory, dirs, files in os.walk(tree, followlinks=False, onerror=failed):
        for name in dirs + files:
            path = Path(directory) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # builds replace generated files concurrently with observations
            if info.st_dev != device:
                raise ValueError("candidate inventory crosses a filesystem")
            yield info.st_size if stat.S_ISREG(info.st_mode) else 0


class Monitor:
    def __init__(self, state, identifier):
        self.state, self.identifier = state, identifier
        self.tree = candidate(state, identifier)
        self.next_check, self.scan, self.bytes = 0, None, 0

    def check(self, *, resources=True):
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 1
        if resources:
            free_space(self.tree)
        try:
            if self.scan is None:
                self.scan, self.bytes = _sizes(self.tree), 0
            # Bound optional observation work independently of successful verification.
            for _ in range(4096):
                try:
                    self.bytes += next(self.scan)
                except StopIteration:
                    self.scan = None
                    settle(self.state, self.identifier, self.bytes, running=True)
                    break
        except (OSError, ValueError, Pressure):
            self.close()

    def close(self):
        if self.scan is not None:
            self.scan.close()
            self.scan = None

    def finish(self):
        """Leave complete size accounting to bounded housekeeping, off the success path."""
        self.settle_idle()

    def settle_idle(self):
        if getattr(self, "idled", False):
            return
        self.idled = True
        self.close()
        with contextlib.suppress(OSError, ValueError, Pressure):
            with ledger() as data:
                row = data["candidates"].get(key(self.state, self.identifier))
                if row is not None:
                    row.update(legacy=True, settled=False, growing=False, compact=False, last_used=time.time())


def _builds(tree, work, *, dependencies=True):
    """Discover generated build roots, preserving dependency checkouts and their Git state."""
    yield tree, ".lake/build"
    yield tree, ".lake/build-before-sharing"
    yield tree, "build"
    packages = tree / ".lake/packages"
    if dependencies and not (tree / ".lake").is_symlink() and not packages.is_symlink() and packages.is_dir():
        with os.scandir(packages) as entries:
            for entry in entries:
                work.charge(entries=1)
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path), ".lake/build"
                    yield Path(entry.path), ".lake/build-before-sharing"
                    yield Path(entry.path), "build"


def _observe_users(tree, work):
    from . import work_recovery as wr

    # One /proc pass per collection slice, with its own bounded allowance. A busy host's
    # reference inspection must not consume the entire subsequent deletion allowance forever.
    probe = wr.Work(seconds=setting("OWNERSHIP_SECONDS", 2))
    token = wr._WORK.set(probe)
    started = time.monotonic()
    work.ownership_probed = True
    try:
        return wr.live_users(tree)
    finally:
        elapsed = time.monotonic() - started
        work.started += elapsed
        work.ownership_seconds = getattr(work, "ownership_seconds", 0) + elapsed
        wr._WORK.reset(token)


def _compact(state, identifier, work, *, now, row, drop_dependencies=False, evict=False):
    from . import recovery_maintenance as maintenance
    from . import work_recovery as wr
    from .retention import locked, mount_points

    tree = candidate(state, identifier)
    journal = state / "verification-storage" / f"{identifier}.json"
    pending = read_json(state / "verification-pending" / f"{identifier}.json")
    terminal = read_json(state / "verification-jobs" / f"{identifier}.json")
    job = pending or terminal
    if job and (job.get("schema") != "tauceti-verification-job/v1" or job.get("id") != identifier):
        job = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(locked(state / "verification-jobs" / f"{identifier}.lock"))
        stack.enter_context(locked(state / "verification-storage" / f"{identifier}.lock"))
        if lease := job.get("lease"):
            lease = Path(lease)
            if lease.is_relative_to("/opt/verification-state"):
                lease = state / lease.relative_to("/opt/verification-state")
            if lease.exists():
                stack.enter_context(locked(lease))
        if not tree.exists():
            with ledger() as data:
                data["candidates"].pop(key(state, identifier), None)
            return "absent"
        if not job or not evict and (row.get("legacy") or not row.get("settled")):
            return _measure(state, row, tree, work, compact=not bool(job))
        if _observe_users(tree, work):
            return "active"
        mounts = mount_points()
        if mounts is None or any(point == tree or point.is_relative_to(tree) for point in mounts):
            return "mounted"
        copy_marker = state / "verification-copy-incomplete" / f"{identifier}.json"
        construction = read_json(copy_marker)
        if (
            construction.get("version") == 1
            and not job.get("isolated")
            and construction.get("source") == job.get("root")
            and construction.get("source_digest") == job.get("source_digest")
        ):
            progress = read_json(journal)
            info = tree.stat()
            identity = [info.st_dev, info.st_ino]
            if progress.get("discard-copy", {}).get("tree_identity") != identity:
                progress["discard-copy"] = dict(tree_identity=identity, stack=[""])
            deletion = progress["discard-copy"]
            try:
                atomic_json(journal, progress)
                if maintenance._remove(tree, deletion, work):
                    release(state, identifier)
                    copy_marker.unlink(missing_ok=True)
                    journal.unlink(missing_ok=True)
                    return "failed-copy-removed"
                return "resume-failed-copy-removal"
            finally:
                if tree.exists():
                    atomic_json(journal, progress)
        with ledger(write=False) as data:
            current = data["candidates"].get(key(state, identifier))
        if current is None or current["last_used"] != row["last_used"]:
            return "recently-used"
        if row.get("reclamation_blocked"):
            with ledger() as data:
                data["candidates"][key(state, identifier)].pop("reclamation_blocked", None)
        progress = read_json(journal)
        unfinished = {name for name, value in progress.items() if isinstance(value, dict) and "stack" in value}
        resume_dependencies = any(name.startswith(".lake/packages/") for name in unfinished)
        for repo, name in _builds(tree, work, dependencies=bool(terminal) or drop_dependencies or resume_dependencies):
            cache = repo / name
            if (
                repo != tree
                and not terminal
                and not drop_dependencies
                and str(cache.relative_to(tree)) not in unfinished
            ):
                continue
            if cache.is_symlink() or not cache.is_dir():
                continue
            # Never remove a tracked build directory, nor a symlinked intermediate .lake.
            if (repo / ".lake").is_symlink():
                continue
            top = wr._git(repo, "rev-parse", "--show-toplevel", text=True).strip()
            if (
                Path(top).resolve() != repo.resolve()
                or wr._git(repo, "ls-files", "-z", "--", name)
                or wr._git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", name)
            ):
                continue
            # Removing outputs invalidates cached build-stage reuse in this workspace. Keep
            # the actual receipt as historical evidence; no scan of old receipts is needed.
            receipt_path = Path(job.get("receipt", ""))
            if receipt_path.is_relative_to("/opt/verification-state"):
                receipt_path = state / receipt_path.relative_to("/opt/verification-state")
            fingerprint = read_json(receipt_path).get("fingerprint") if receipt_path.is_file() else None
            if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                index = state / "verification-index" / f"{fingerprint}.json"
                with locked(index.with_suffix(".lock")):
                    evidence = read_json(index)
                    evidence.pop("build", None)
                    atomic_json(index, evidence)
            relative = str(cache.relative_to(tree))
            info = cache.stat()
            subjob = progress.setdefault(relative, dict(tree_identity=[info.st_dev, info.st_ino], stack=[""]))
            removed_before = work.removed_bytes
            try:
                if "_generation" not in progress:
                    progress["_generation"] = uuid.uuid4().hex
                if (
                    read_json(state / "verification-storage-generations" / f"{identifier}.json").get("generation")
                    != progress["_generation"]
                ):
                    atomic_json(
                        state / "verification-storage-generations" / f"{identifier}.json",
                        dict(generation=progress["_generation"]),
                    )
                # Persist intent before the first deletion, including truncation of large files.
                atomic_json(journal, progress)
                if maintenance._remove(cache, subjob, work):
                    progress.pop(relative, None)
                else:
                    return "resume-cache-removal"
            finally:
                atomic_json(journal, progress)
                if row.get("settled") and work.removed_bytes > removed_before:
                    # This quiescent snapshot has a known upper size. Release reclaimed bytes
                    # immediately; an interrupted/unmeasured tool retains its earlier estimate
                    # until a complete inventory establishes its size. Neither gates work.
                    with contextlib.suppress(OSError, Pressure):
                        with ledger() as data:
                            current = data["candidates"].get(key(state, identifier))
                            if current:
                                removed = work.removed_bytes - removed_before
                                current["reserved"] = max(FLOOR, current["reserved"] - removed)
                                current["measured"] = max(0, current.get("measured", 0) - removed)
        # Replace the estimate after a complete post-eviction inventory. The generator
        # resumes between ticks; a restart discards it and starts a fresh read-only count.
        with ledger(write=False) as data:
            generation = data["candidates"][key(state, identifier)]["last_used"]
        identity = (root().resolve(), state, identifier, generation)
        for old in list(_SCANS):
            if old[:3] == identity[:3] and old != identity:
                _SCANS.pop(old)[0].close()
        scan, size = _SCANS.get(identity, (None, 0))
        if scan is None:
            if len(_SCANS) >= 4:
                return "await-source-inventory"
            scan = _sizes(tree)
        try:
            while True:
                work.charge(entries=1)
                size += next(scan)
        except StopIteration:
            _SCANS.pop(identity, None)
            with ledger() as data:
                row = data["candidates"].get(key(state, identifier))
                if row:
                    row.update(
                        reserved=max(FLOOR, size),
                        measured=size,
                        compact=True,
                        compacted_at=now,
                        legacy=False,
                        dependency_cache_evicted=bool(terminal) or drop_dependencies,
                        overflow=size > setting("CANDIDATE_BYTES", 32 * GIB),
                        growing=False,
                    )
                    row.pop("reclamation_blocked", None)
            journal.unlink(missing_ok=True)
            return "source-retained"
        finally:
            if scan.gi_frame is not None:
                _SCANS[identity] = scan, size
            else:
                _SCANS.pop(identity, None)


def _measure_checkpoint(state, row, work):
    from .retention import locked

    target = state / "verification-checkpoints" / row["id"]
    if target.is_symlink() or target.parent.is_symlink():
        return "source-checkpoint-retained"
    with locked(state / "verification-checkpoint-locks" / f"{row['id']}.lock"):
        if not target.exists():
            release(state, row["id"])
            return "absent"
        return _measure(state, row, target, work, compact=True)


def _measure(state, row, target, work, *, compact=False):
    identity = (root().resolve(), state, row["id"], row["last_used"])
    for old in list(_SCANS):
        if old[:3] == identity[:3] and old != identity:
            _SCANS.pop(old)[0].close()
    scan, size = _SCANS.get(identity, (None, 0))
    if scan is None:
        if len(_SCANS) >= 4:
            return "await-source-inventory"
        scan = _sizes(target)
    try:
        while True:
            work.charge(entries=1)
            size += next(scan)
    except StopIteration:
        _SCANS.pop(identity, None)
        with ledger() as data:
            current = data["candidates"].get(key(state, row["id"]))
            if current is not None:
                current.update(
                    reserved=max(FLOOR, size),
                    measured=size,
                    compact=compact,
                    legacy=False,
                    settled=True,
                    growing=False,
                    overflow=row.get("kind") != "checkpoint" and size > setting("CANDIDATE_BYTES", 32 * GIB),
                )
        return "source-retained"
    finally:
        if scan.gi_frame is not None:
            _SCANS[identity] = scan, size
        else:
            _SCANS.pop(identity, None)


def _prune_scans(entries):
    directory = root().resolve()
    generations = {(Path(row["state"]), row["id"]): row["last_used"] for row in entries.values()}
    for identity in list(_SCANS):
        if identity[0] != directory or generations.get(identity[1:3]) != identity[3]:
            _SCANS.pop(identity)[0].close()


def _housekeep(states=(), *, seconds=0.25, entries=2048, removal_bytes=256 * 1024**2, now=None):
    """Ordinary supervisor/activity ticks own cleanup. No timer, model or GitHub reads."""
    from . import work_recovery as wr

    now = time.time() if now is None else now
    wall_started = time.monotonic()
    work = wr.Work(seconds=seconds, entries=entries, removal_bytes=removal_bytes)
    report = dict(at=now, candidates={}, errors=[])
    cursor_path, last_processed = root() / "cursor.json", None
    excess, rows = 0, []
    token = wr._WORK.set(work)
    try:
        for state in states:
            try:
                discover(state)
            except (OSError, ValueError) as error:
                if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise
                report["errors"].append(str(error)[:300])
        with ledger(write=False) as data:
            rows = list(data["candidates"].items())
        _prune_scans(data["candidates"])
        pending_order = []
        metadata = {}
        other_reclaimable = False
        for identity, row in rows:
            if row.get("kind") == "checkpoint":
                continue
            pending_path = Path(row["state"]) / "verification-pending" / f"{row['id']}.json"
            pending = read_json(pending_path)
            if pending.get("status") in {"pending", "blocked-storage-limit"} and isinstance(
                pending.get("priority"), int
            ):
                pending_order.append((pending["priority"], identity))
            terminal_path = Path(row["state"]) / "verification-jobs" / f"{row['id']}.json"
            terminal = terminal_path.is_file()
            last_used = max(row["last_used"], terminal_path.stat().st_mtime) if terminal else row["last_used"]
            if pending and not terminal:
                last_used = max(last_used, pending_path.stat().st_mtime)
            metadata[identity] = terminal, last_used
            if not row.get("compact") and not row.get("reclamation_blocked"):
                other_reclaimable = True
        protected = min(pending_order)[1] if pending_order else None
        allowed = {str(canonical_state(state)) for state in states}
        rows = [(identity, row) for identity, row in rows if row["state"] in allowed or not Path(row["state"]).exists()]
        try:
            devices = {}
            for _, row in rows:
                state = Path(row["state"])
                if state.exists():
                    devices[state.stat().st_dev] = state
            for target in devices.values():
                free_space(target)
            disk_pressure = False
        except Pressure:
            disk_pressure = True
        # Active work, checkpoints, compacted source-only trees and recent pending progress
        # cannot relieve a soft target. Do not flush warm caches to compensate for them.
        reclaimable = sum(
            observed(row)
            for identity, row in data["candidates"].items()
            if identity in metadata
            and not row.get("compact")
            and not row.get("growing")
            and not row.get("reclamation_blocked")
            and (metadata[identity][0] or now - metadata[identity][1] >= setting("IDLE_CACHE_SECONDS", 21600))
        )
        excess = max(0, reclaimable - setting("STORAGE_BYTES", 128 * GIB))
        # Start each sweep terminal/oldest first, then rotate past blocked owners fairly.
        cursor = read_json(cursor_path)
        last = cursor.get("last", "")
        if not isinstance(last, str):
            last = ""
        rows.sort(
            key=lambda item: (not metadata.get(item[0], (False, 0))[0], metadata.get(item[0], (False, 0))[1], item[0])
        )
        positions = [i for i, item in enumerate(rows) if item[0] == last]
        if positions:
            position = positions[0] + 1
            rows = rows[position:] + rows[:position]
        continuing = {identity for identity, row in rows if _has_removal(Path(row["state"]), row["id"])}
        rows.sort(
            key=lambda item: (
                item[0] not in continuing or bool(item[1].get("reclamation_blocked")),
                metadata.get(item[0], (False, 0))[1]
                if item[0] in continuing and not item[1].get("reclamation_blocked")
                else 0,
            )
        )
        for identity, row in rows:
            work.charge(entries=1)
            if work.removal_remaining <= 0 or getattr(work, "ownership_probed", False):
                raise wr.BudgetExhausted("deletion byte slice exhausted; rotate to the next candidate")
            last_processed = identity
            state, identifier = Path(row["state"]), row["id"]
            directory = "verification-checkpoints" if row.get("kind") == "checkpoint" else "verification-candidates"
            if not state.exists() or row.get("compact") and not (state / directory / identifier).exists():
                with ledger() as data:
                    if data["candidates"].get(identity) == row:
                        data["candidates"].pop(identity, None)
                continue
            if row.get("kind") == "checkpoint":
                if row.get("legacy") or not row.get("settled"):
                    try:
                        report["candidates"][identity] = _measure_checkpoint(state, row, work)
                    except BlockingIOError:
                        report["candidates"][identity] = "active"
                continue
            # Prefer terminal/own-build reclamation. If that cannot make room, release older
            # idle dependency caches from other jobs while keeping the oldest runnable job
            # warm. This avoids both a protected-cache deadlock and self-eviction/rebuild loops.
            drop_dependencies = (
                disk_pressure
                and not other_reclaimable
                and identity != protected
                and row.get("compact")
                and not row.get("dependency_cache_evicted")
                and (state / "verification-pending" / f"{identifier}.json").exists()
            )
            if row.get("compact") and not drop_dependencies and identity not in continuing:
                continue
            terminal, last_used = metadata[identity]
            ttl = setting("COMPLETED_CACHE_SECONDS", 900) if terminal else setting("IDLE_CACHE_SECONDS", 21600)
            early_terminal_cleanup = terminal and (
                excess > work.removed_bytes or observed(row) > setting("CANDIDATE_BYTES", 32 * GIB)
            )
            evict = disk_pressure or early_terminal_cleanup or now - last_used >= ttl or identity in continuing
            if (
                not evict
                and not row.get("legacy")
                and row.get("settled")
                and (row.get("compact") or now - last_used < ttl)
            ):
                continue
            try:
                result = _compact(
                    state, identifier, work, now=now, row=row, drop_dependencies=drop_dependencies, evict=evict
                )
                report["candidates"][identity] = result
                if result in {"active", "mounted", "unknown-owner", "invalid-owner"}:
                    with ledger() as data:
                        current = data["candidates"].get(identity)
                        if current is not None:
                            current["reclamation_blocked"] = result
            except BlockingIOError:
                report["candidates"][identity] = "active"
                with contextlib.suppress(OSError, Pressure):
                    with ledger() as current_data:
                        if current := current_data["candidates"].get(identity):
                            current["reclamation_blocked"] = "active"
            except (OSError, ValueError, wr.RecordError) as error:
                if isinstance(error, wr.BudgetExhausted):
                    raise
                report["errors"].append(str(error)[:300])
                if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
                    report["enospc"] = True
                with contextlib.suppress(OSError, Pressure):
                    with ledger() as data:
                        current = data["candidates"].get(identity)
                        if current is not None:
                            current["reclamation_blocked"] = "cleanup-error"
    except wr.BudgetExhausted:
        report["next_action"] = "resume-verification-storage-housekeeping"
    except (OSError, ValueError, Pressure) as error:
        report["errors"].append(str(error)[:300])
        if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
            report["enospc"] = True
        if isinstance(error, Pressure) and error.reason == "accounting":
            with contextlib.suppress(OSError, ValueError, Pressure):
                recover(states)
                report["next_action"] = "resume-after-verification-storage-index-rebuild"
    finally:
        wr._WORK.reset(token)
        if last_processed is not None:
            with contextlib.suppress(OSError):
                atomic_json(cursor_path, dict(last=last_processed))
    report["work"] = work.report()
    report["work"]["ownership_seconds"] = getattr(work, "ownership_seconds", 0)
    report["work"]["wall_seconds"] = time.monotonic() - wall_started
    with contextlib.suppress(OSError, Pressure):
        with ledger(write=False) as data:
            report["reserved_bytes"] = sum(observed(row) for row in data["candidates"].values())
            report["candidate_count"] = len(data["candidates"])
            report["compacted_count"] = sum(bool(row.get("compact")) for row in data["candidates"].values())
            report["measured_bytes"] = sum(row.get("measured", 0) for row in data["candidates"].values())
            report["observed_bytes"] = report["reserved_bytes"]
            report["unmeasured_count"] = sum(bool(row.get("legacy")) for row in data["candidates"].values())
        report["limit_bytes"] = setting("STORAGE_BYTES", 128 * GIB)
        report["free_bytes"] = shutil.disk_usage(root()).free
        report["target_bytes"] = report["limit_bytes"]  # Preserve older status readers.
        report["admission_limited"] = False
        report["configuration_warnings"] = [
            "TAUCETI_VERIFY_" + name + " is invalid; using cleanup default " + str(default)
            for name, default in _CLEANUP_SETTINGS.items()
            if "TAUCETI_VERIFY_" + name in os.environ
            and (not os.environ["TAUCETI_VERIFY_" + name].isdigit() or int(os.environ["TAUCETI_VERIFY_" + name]) <= 0)
        ]
        report["cleanup_excess_bytes"] = max(0, excess - work.removed_bytes)
        if report["cleanup_excess_bytes"]:
            report["attention"] = "verification-storage-cleanup-target-exceeded"
            report["next_action"] = (
                "reclaim idle build caches; retain active work and unpublished source; verification admission continues"
            )
    # Prepared data only: status commands never traverse retained candidates.
    with contextlib.suppress(OSError):
        atomic_json(root() / "status.json", report)
    return report


def _scheduled(states=(), **budgets):
    """One host collector per ten seconds; overlapping supervisors never repeat the work."""
    states = tuple(states)
    if not any(
        (state / directory).is_dir()
        for state in states
        for directory in ("verification-candidates", "verification-checkpoints")
    ):
        if not root().exists():
            return {}
        with ledger(write=False) as data:
            if not any(
                row["state"] in {str(canonical_state(state)) for state in states} for row in data["candidates"].values()
            ):
                return {}
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "housekeeping.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(busy=True)
        schedule = directory / "housekeeping.json"
        next_at = read_json(schedule).get("next_at", 0)
        if not isinstance(next_at, (int, float)) or not math.isfinite(next_at):
            next_at = 0
        if budgets.get("now") is None and time.time() < next_at <= time.time() + 10:
            return read_json(directory / "status.json")
        result = _housekeep(states, **budgets)
        with contextlib.suppress(OSError):
            atomic_json(schedule, dict(next_at=time.time() + 10))
        return result


def housekeep(states=(), **budgets):
    """Storage failures cannot take down the ordinary inference/verification supervisor."""
    states = tuple(states)
    try:
        validate_settings()
        result = _scheduled(states, **budgets)
    except (OSError, ValueError, KeyError, TypeError, Pressure) as error:
        result = dict(errors=[str(error)[:300]], next_action="retry-verification-storage-housekeeping")
        result["enospc"] = isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}
    if result.get("enospc"):
        # A full filesystem may prevent writing the deletion journal itself. Discard only
        # optional derived hash metadata, under its ordinary writer lock, to make room for
        # durable cleanup intent. Receipts, indexes, source and checkpoints remain intact.
        for state in states:
            cache = state / "verification-hashes-v2.json"
            lock = cache.with_suffix(".lock")
            try:
                if state.is_symlink() or not lock.is_file():
                    continue
                with lock.open("a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    info = cache.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                        continue
                    cache.unlink()
                    retry = _scheduled(states, **{**budgets, "now": budgets.get("now") or time.time()})
                    retry["emergency_hash_cache_reclaimed"] = str(cache)
                    return retry
            except (OSError, ValueError, KeyError, TypeError, Pressure):
                continue
    return result

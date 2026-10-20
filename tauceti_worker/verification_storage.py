"""Host-local candidate storage admission and bounded, restartable cache reclamation.

Source, Git history, dependency sources and receipts never expire. Only untracked Lake build
directories are disposable. Reservations limit admitted work; ordinary filesystems cannot provide
a hard write quota, so running tools additionally observe free space and incremental size checks.
"""

from __future__ import annotations

import contextlib
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


class Pressure(Exception):
    """Retain the source/job and retry after ordinary storage housekeeping."""

    def __init__(self, message, *, reason="capacity"):
        super().__init__(message)
        self.reason = reason


class Limit(Pressure):
    """A deterministic per-item limit needs a configuration/input change, not a retry loop."""

    def __init__(self, message):
        super().__init__(message, reason="limit")


class Configuration(Pressure):
    def __init__(self, message):
        super().__init__(message, reason="configuration")


class CopyLimit(Limit):
    """Retry a warm snapshot without rebuildable build output."""


def setting(name, default):
    try:
        value = int(os.environ.get("TAUCETI_VERIFY_" + name, default))
    except ValueError:
        raise Configuration(f"TAUCETI_VERIFY_{name} must be a positive integer") from None
    if value <= 0:
        raise Configuration(f"TAUCETI_VERIFY_{name} must be a positive integer")
    return value


def checkpoint_pool():
    total, running = setting("STORAGE_BYTES", 128 * GIB), setting("CANDIDATE_BYTES", 32 * GIB)
    # Reserve a small part of the total for source preservation during legacy migration.
    default = min(GIB, total // 128, max(0, total - running))
    return (
        setting("CHECKPOINT_POOL_BYTES", default) if "TAUCETI_VERIFY_CHECKPOINT_POOL_BYTES" in os.environ else default
    )


def validate_settings():
    for name, default in (
        ("STORAGE_BYTES", 128 * GIB),
        ("CANDIDATE_BYTES", 32 * GIB),
        ("SOURCE_CANDIDATE_BYTES", 2 * GIB),
        ("CHECKPOINT_BYTES", 256 * 1024**2),
        ("MIN_FREE_BYTES", 20 * GIB),
        ("OWNERSHIP_SECONDS", 2),
        ("COMPLETED_CACHE_SECONDS", 3600),
        ("IDLE_CACHE_SECONDS", 86400),
    ):
        setting(name, default)
    checkpoint_pool()
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
        raise Pressure(f"verification disk reserve: {usage.free} bytes free; need {reserve + additional}")
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
                        reserved=setting("CANDIDATE_BYTES", 32 * GIB)
                        if kind == "candidate"
                        else setting("CHECKPOINT_BYTES", 256 * 1024**2),
                        last_used=0,
                        legacy=True,
                    ),
                )
                if kind == "checkpoint" and row.get("legacy") and (path / "checkpoint.json").is_file():
                    # Completed checkpoints have a tiny, fixed layout. A metadata-only bounded
                    # migration must not book 256 MiB each and block new source preservation.
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
        discover(sibling)
        discover(sibling / "verification-state")


def reserve(state, identifier, *, source_only=False):
    """Acquire a byte reservation before copying caches or starting candidate tools."""
    prepare(state)
    target = candidate(state, identifier)
    amount = setting("CANDIDATE_BYTES", 32 * GIB)
    if source_only:
        amount = min(amount, setting("SOURCE_CANDIDATE_BYTES", 2 * GIB))
    return _reserve(state, identifier, target, amount, "candidate")


def reserve_checkpoint(state, identifier, *, amount=None):
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid verification checkpoint identifier")
    prepare(state)
    target = state / "verification-checkpoints" / identifier
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("verification checkpoint path is a symlink")
    limit = setting("CHECKPOINT_BYTES", 256 * 1024**2)
    return _reserve(state, identifier, target, min(amount, limit) if amount is not None else limit, "checkpoint")


def _reserve(state, identifier, target, amount, kind):
    with ledger() as data:
        entries = data["candidates"]
        identity = key(state, identifier)
        old = entries.get(identity, {})
        pool = checkpoint_pool()
        relevant = [row for row in entries.values() if (row.get("kind") == "checkpoint") == (kind == "checkpoint")]
        total = sum(row["reserved"] for row in relevant) - old.get("reserved", 0) + amount
        allowance = pool if kind == "checkpoint" else setting("STORAGE_BYTES", 128 * GIB) - pool
        if total > allowance:
            with contextlib.suppress(OSError):
                atomic_json(
                    root() / "pressure.json",
                    dict(at=time.time(), kind=kind, requested=amount, reserved=total - amount, allowance=allowance),
                )
            raise Pressure("host verification candidate byte budget exhausted; awaiting cache reclamation")
        parent = target.parent
        while not parent.exists():
            parent = parent.parent
        device = parent.stat().st_dev
        # Actual written bytes have already reduced statvfs.free; reserve only future growth
        # and only on the same filesystem.
        outstanding = sum(
            max(0, row["reserved"] - row.get("measured", 0))
            for k, row in entries.items()
            if k != identity and row.get("device") == device
        )
        measured = old.get("measured", 0)
        free_space(target.parent, additional=outstanding + max(0, amount - measured))
        entries[identity] = dict(
            state=str(canonical_state(state)),
            id=identifier,
            kind=kind,
            reserved=amount,
            last_used=time.time(),
            compact=kind == "checkpoint",
            settled=False,
            measured=measured,
            device=device,
        )
    return amount


def settle(state, identifier, size, *, compact=False):
    """Completed snapshots/checkpoints reserve their measured bytes, not a whole build allowance."""
    with ledger() as data:
        row = data["candidates"].get(key(state, identifier))
        if row is not None:
            row.update(reserved=max(FLOOR, size), measured=size, compact=compact, legacy=False, settled=True)


def release(state, identifier):
    with ledger() as data:
        data["candidates"].pop(key(state, identifier), None)


def overflow(state, identifier, observed):
    with ledger() as data:
        row = data["candidates"].get(key(state, identifier))
        if row:
            row.update(reserved=max(row["reserved"], observed), overflow=True, settled=False)


def cache_generation(state, tree):
    if tree.parent == state.resolve() / "verification-candidates" and re.fullmatch(r"[0-9a-f]{32}", tree.name):
        return read_json(state / "verification-storage-generations" / f"{tree.name}.json").get("generation")
    return None


class LimitedWriter:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    def tell(self):
        return self.stream.tell()

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise Limit("source checkpoint archive exceeds its reserved byte budget")
        return self.stream.write(data)


class CopyBudget:
    """Count logical copied bytes even for reflinks; sharing must not hide future CoW growth."""

    def __init__(self, limit, target):
        self.limit, self.target, self.bytes, self.next_check = limit, target, 0, 0

    def charge(self, path):
        info = path.stat()
        if stat.S_ISREG(info.st_mode):
            self.bytes += info.st_size
        if self.bytes > self.limit:
            raise CopyLimit("candidate copy exceeds its logical byte reservation")
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
        self.limit = setting("CANDIDATE_BYTES", 32 * GIB)
        self.next_check, self.scan, self.bytes = 0, None, 0

    def check(self):
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 1
        free_space(self.tree)
        if self.scan is None:
            self.scan, self.bytes = _sizes(self.tree), 0
        # Bound each observation. On large candidates the size observation spans several ticks.
        for _ in range(4096):
            try:
                self.bytes += next(self.scan)
            except StopIteration:
                self.scan = None
                break
            if self.bytes > self.limit:
                self.close()
                with contextlib.suppress(OSError, Pressure):
                    overflow(self.state, self.identifier, self.bytes)
                raise Limit("running verification candidate exceeded its byte reservation")

    def close(self):
        if self.scan is not None:
            self.scan.close()
            self.scan = None

    def finish(self):
        """A fast writer cannot finish successfully between incremental observations."""
        self.close()
        free_space(self.tree)
        size = 0
        with contextlib.closing(_sizes(self.tree)) as inventory:
            for amount in inventory:
                size += amount
                if size > self.limit:
                    with contextlib.suppress(OSError, Pressure):
                        overflow(self.state, self.identifier, size)
                    raise Limit("completed candidate exceeds its logical byte reservation")
        settle(self.tree.parent.parent, self.tree.name, size)


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


def _compact(state, identifier, work, *, now, row, drop_dependencies=False):
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
            deletion = progress.setdefault("discard-copy", dict(tree_identity=[info.st_dev, info.st_ino], stack=[""]))
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
        if row.get("legacy") and not job:
            return _measure(state, row, tree, work)
        if not job:
            return "unknown-owner"  # measured source remains protected; no blind deletion
        with ledger(write=False) as data:
            current = data["candidates"].get(key(state, identifier))
        if current is None or current["last_used"] != row["last_used"]:
            return "recently-used"
        progress = read_json(journal)
        for repo, name in _builds(tree, work, dependencies=bool(terminal) or drop_dependencies):
            cache = repo / name
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
                    # immediately; an interrupted/unmeasured tool retains its growth allowance
                    # until a complete inventory can establish a safe size.
                    with contextlib.suppress(OSError, Pressure):
                        with ledger() as data:
                            current = data["candidates"].get(key(state, identifier))
                            if current:
                                removed = work.removed_bytes - removed_before
                                current["reserved"] = max(FLOOR, current["reserved"] - removed)
                                current["measured"] = max(0, current.get("measured", 0) - removed)
        # Shrink a reservation only after a complete post-eviction inventory. The generator
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
                    )
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
                current.update(reserved=max(FLOOR, size), measured=size, compact=compact, legacy=False, settled=True)
        return "source-retained"
    finally:
        if scan.gi_frame is not None:
            _SCANS[identity] = scan, size
        else:
            _SCANS.pop(identity, None)


def _housekeep(states=(), *, seconds=0.25, entries=2048, removal_bytes=256 * 1024**2, now=None):
    """Ordinary supervisor/activity ticks own cleanup. No timer, model or GitHub reads."""
    from . import work_recovery as wr

    now = time.time() if now is None else now
    wall_started = time.monotonic()
    work = wr.Work(seconds=seconds, entries=entries, removal_bytes=removal_bytes)
    report = dict(at=now, candidates={}, errors=[])
    cursor_path, last_processed = root() / "cursor.json", None
    token = wr._WORK.set(work)
    try:
        for state in states:
            discover(state)
        with ledger(write=False) as data:
            rows = list(data["candidates"].items())
        pending_order = []
        other_reclaimable = False
        for identity, row in rows:
            if row.get("kind") == "checkpoint":
                continue
            pending = read_json(Path(row["state"]) / "verification-pending" / f"{row['id']}.json")
            if pending.get("status") == "pending" and isinstance(pending.get("priority"), int):
                pending_order.append((pending["priority"], identity))
            if not row.get("compact") and not row.get("reclamation_blocked"):
                other_reclaimable = True
        protected = min(pending_order)[1] if pending_order else None
        allowed = {str(canonical_state(state)) for state in states}
        rows = [(identity, row) for identity, row in rows if row["state"] in allowed or not Path(row["state"]).exists()]
        try:
            free_space(root())
            candidate_reserved = sum(row["reserved"] for _, row in rows if row.get("kind") != "checkpoint")
            pressure = (
                candidate_reserved + setting("CANDIDATE_BYTES", 32 * GIB)
                > setting("STORAGE_BYTES", 128 * GIB) - checkpoint_pool()
            )
        except Pressure:
            pressure = True
        # Cursor rotation prevents an active or very large oldest candidate starving others.
        last = read_json(cursor_path).get("last", "")
        if not isinstance(last, str):
            last = ""
        inventories = {key(identity[1], identity[2]) for identity in _SCANS if identity[0] == root().resolve()}
        rows.sort(
            key=lambda item: (
                item[0] not in inventories,
                not (Path(item[1]["state"]) / "verification-jobs" / f"{item[1]['id']}.json").exists(),
                item[0] <= last,
                item[0],
            )
        )
        for identity, row in rows:
            work.charge(entries=1)
            if work.removal_remaining <= 0 or getattr(work, "ownership_probed", False):
                raise wr.BudgetExhausted("deletion byte slice exhausted; rotate to the next candidate")
            last_processed = identity
            state, identifier = Path(row["state"]), row["id"]
            directory = "verification-checkpoints" if row.get("kind") == "checkpoint" else "verification-candidates"
            if row.get("compact") and not (state / directory / identifier).exists():
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
                pressure
                and not other_reclaimable
                and identity != protected
                and row.get("compact")
                and not row.get("dependency_cache_evicted")
                and (state / "verification-pending" / f"{identifier}.json").exists()
            )
            if row.get("compact") and not drop_dependencies:
                continue
            terminal_path = state / "verification-jobs" / f"{identifier}.json"
            terminal = terminal_path.exists()
            ttl = setting("COMPLETED_CACHE_SECONDS", 3600) if terminal else setting("IDLE_CACHE_SECONDS", 86400)
            last_used = max(row["last_used"], terminal_path.stat().st_mtime) if terminal else row["last_used"]
            if not pressure and not row.get("overflow") and (row.get("compact") or now - last_used < ttl):
                continue
            try:
                result = _compact(state, identifier, work, now=now, row=row, drop_dependencies=drop_dependencies)
                report["candidates"][identity] = result
                if result in {"active", "mounted", "unknown-owner", "invalid-owner"}:
                    with ledger() as data:
                        current = data["candidates"].get(identity)
                        if current is not None:
                            current["reclamation_blocked"] = result
            except BlockingIOError:
                report["candidates"][identity] = "active"
            except (OSError, ValueError, wr.RecordError) as error:
                if isinstance(error, wr.BudgetExhausted):
                    raise
                report["errors"].append(str(error)[:300])
                with contextlib.suppress(OSError, Pressure):
                    with ledger() as data:
                        current = data["candidates"].get(identity)
                        if current is not None:
                            current["reclamation_blocked"] = "cleanup-error"
    except wr.BudgetExhausted:
        report["next_action"] = "resume-verification-storage-housekeeping"
    except (OSError, ValueError, Pressure) as error:
        report["errors"].append(str(error)[:300])
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
            report["reserved_bytes"] = sum(row["reserved"] for row in data["candidates"].values())
            report["candidate_count"] = len(data["candidates"])
            report["compacted_count"] = sum(bool(row.get("compact")) for row in data["candidates"].values())
            report["measured_bytes"] = sum(row.get("measured", 0) for row in data["candidates"].values())
            report["unmeasured_count"] = sum(bool(row.get("legacy")) for row in data["candidates"].values())
        report["limit_bytes"] = setting("STORAGE_BYTES", 128 * GIB)
        report["free_bytes"] = shutil.disk_usage(root()).free
        refusal = read_json(root() / "pressure.json")
        if (isinstance(refusal.get("at"), (float, int)) and refusal["at"] > time.time() - 300) or report[
            "reserved_bytes"
        ] + min(setting("SOURCE_CANDIDATE_BYTES", 2 * GIB), setting("CANDIDATE_BYTES", 32 * GIB)) > report[
            "limit_bytes"
        ]:
            report["attention"] = "verification-storage-allowance-exhausted"
            report["next_action"] = (
                "reclaim terminal caches; archive/publish unique source or explicitly adjust the storage allowance"
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
        return _scheduled(states, **budgets)
    except (OSError, ValueError, KeyError, TypeError, Pressure) as error:
        return dict(errors=[str(error)[:300]], next_action="retry-verification-storage-housekeeping")

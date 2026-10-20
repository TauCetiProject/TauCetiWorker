"""Host-local candidate storage admission and bounded, restartable cache reclamation.

Source, Git history, dependency sources and receipts never expire. Only untracked Lake build
directories are disposable. Reservations limit admitted work; ordinary filesystems cannot provide
a hard write quota, so running tools additionally observe free space and incremental size checks.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
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
FLOOR = 64 * 1024**2
_SCANS = {}


class Pressure(Exception):
    """Retain the source/job and retry after ordinary storage housekeeping."""


class CopyLimit(Pressure):
    """Retry a warm snapshot without rebuildable build output."""


def setting(name, default):
    value = int(os.environ.get("TAUCETI_VERIFY_" + name, default))
    if value <= 0:
        raise ValueError(f"TAUCETI_VERIFY_{name} must be a positive integer")
    return value


def root():
    # Agent HOME overrides must not split one user's host budget into per-worker budgets.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if value := os.environ.get("TAUCETI_VERIFY_STORAGE_DIR"):
        return Path(value)
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        import socket

        machine = socket.gethostname()
    return home / ".local/state/tauceti/verification-storage" / hashlib.sha256(machine.encode()).hexdigest()[:16]


@contextlib.contextmanager
def ledger():
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Pressure("verification storage accounting is busy; retry after housekeeping") from None
        path = directory / "candidates.json"
        if path.exists():
            data = read_json(path)
            if data.get("version") != 1 or not isinstance(data.get("candidates"), dict):
                raise Pressure("verification storage ledger is unreadable; retaining existing candidates")
        else:
            data = dict(version=1, candidates={})
        yield data
        atomic_json(path, data)


def canonical_state(state):
    if str(state.resolve()) == "/opt/verification-state" and os.environ.get("TAUCETI_VERIFY_HOST_STATE"):
        return Path(os.environ["TAUCETI_VERIFY_HOST_STATE"])
    return state.resolve()


def key(state, identifier):
    return hashlib.sha256(str(canonical_state(state)).encode()).hexdigest() + ":" + identifier


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
    reserve = max(setting("MIN_FREE_BYTES", 20 * GIB), usage.total // 10)
    if usage.free - additional < reserve:
        raise Pressure(f"verification disk reserve: {usage.free} bytes free; need {reserve + additional}")
    return usage


def discover(state):
    """One shallow migration per state; old trees count conservatively until compacted."""
    state = state.resolve()
    parent = state / "verification-candidates"
    if parent.is_symlink():
        raise ValueError("verification candidate parent is a symlink")
    checkpoints = state / "verification-checkpoints"
    if checkpoints.is_symlink():
        raise ValueError("verification checkpoint parent is a symlink")
    if not parent.is_dir() and not checkpoints.is_dir():
        return
    marker = state / "verification-storage-discovered.json"
    if read_json(marker).get("storage") == str(root().resolve()):
        return
    with ledger() as data:
        for directory, kind, length in ((parent, "candidate", 32), (checkpoints, "checkpoint", 64)):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not re.fullmatch(r"[0-9a-f]{%d}" % length, path.name) or path.is_symlink() or not path.is_dir():
                    continue
                data["candidates"].setdefault(
                    key(state, path.name),
                    dict(
                        state=str(canonical_state(state)),
                        id=path.name,
                        kind=kind,
                        reserved=setting("STORAGE_BYTES", 128 * GIB),
                        last_used=0,
                        legacy=True,
                    ),
                )
    atomic_json(marker, dict(storage=str(root().resolve())))


def discover_family(state):
    """Account legacy sibling workers before admitting a new candidate, without tree walks."""
    base = state.parent.parent if state.name == "verification-state" else state.parent
    for sibling in base.iterdir():
        if sibling.is_symlink() or not sibling.is_dir():
            continue
        discover(sibling)
        discover(sibling / "verification-state")


def reserve(state, identifier):
    """Acquire a byte reservation before copying caches or starting candidate tools."""
    discover_family(state)
    discover(state)
    target = candidate(state, identifier)
    amount = setting("CANDIDATE_BYTES", 32 * GIB)
    return _reserve(state, identifier, target, amount, "candidate")


def reserve_checkpoint(state, identifier):
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid verification checkpoint identifier")
    discover_family(state)
    target = state / "verification-checkpoints" / identifier
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("verification checkpoint path is a symlink")
    return _reserve(state, identifier, target, setting("CHECKPOINT_BYTES", 256 * 1024**2), "checkpoint")


def _reserve(state, identifier, target, amount, kind):
    with ledger() as data:
        entries = data["candidates"]
        identity = key(state, identifier)
        old = entries.get(identity, {})
        total = sum(row["reserved"] for row in entries.values()) - old.get("reserved", 0) + amount
        if total > setting("STORAGE_BYTES", 128 * GIB):
            raise Pressure("host verification candidate byte budget exhausted; awaiting cache reclamation")
        # Existing reservations already protect capacity promised to other candidates.
        outstanding = sum(row["reserved"] for k, row in entries.items() if k != identity and not row.get("compact"))
        free_space(target.parent, additional=outstanding + amount)
        entries[identity] = dict(
            state=str(canonical_state(state)),
            id=identifier,
            kind=kind,
            reserved=amount,
            last_used=time.time(),
            compact=kind == "checkpoint",
        )
    return amount


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
            raise Pressure("source checkpoint archive exceeds its reserved byte budget")
        return self.stream.write(data)


class CopyBudget:
    """Count logical copied bytes even for reflinks; sharing must not hide future CoW growth."""

    def __init__(self, limit):
        self.limit, self.bytes, self.next_check = limit, 0, 0

    def charge(self, path):
        info = path.stat()
        if stat.S_ISREG(info.st_mode):
            self.bytes += info.st_size
        if self.bytes > self.limit:
            raise CopyLimit("candidate copy exceeds its logical byte reservation")
        if time.monotonic() >= self.next_check:
            free_space(path)
            self.next_check = time.monotonic() + 1


def _sizes(tree):
    """Streaming metadata only; never follow a dependency symlink or cross a filesystem."""
    device = tree.stat().st_dev
    stack = [os.scandir(tree)]
    try:
        while stack:
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop().close()
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != device:
                raise ValueError("candidate inventory crosses a filesystem")
            if stat.S_ISDIR(info.st_mode):
                stack.append(os.scandir(entry.path))
            yield info.st_size if stat.S_ISREG(info.st_mode) else 0
    finally:
        for stream in stack:
            stream.close()


class Monitor:
    def __init__(self, state, identifier):
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
                raise Pressure("running verification candidate exceeded its byte reservation")

    def close(self):
        if self.scan is not None:
            self.scan.close()
            self.scan = None


def _builds(tree, work):
    """Discover generated build roots, preserving dependency checkouts and their Git state."""
    yield tree, ".lake/build"
    yield tree, "build"
    packages = tree / ".lake/packages"
    if not (tree / ".lake").is_symlink() and not packages.is_symlink() and packages.is_dir():
        with os.scandir(packages) as entries:
            for entry in entries:
                work.charge(entries=1)
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path), ".lake/build"
                    yield Path(entry.path), "build"


def _compact(state, identifier, work, *, now):
    from . import recovery_maintenance as maintenance
    from . import work_recovery as wr
    from .retention import locked, mount_points

    tree = candidate(state, identifier)
    journal = state / "verification-storage" / f"{identifier}.json"
    pending = read_json(state / "verification-pending" / f"{identifier}.json")
    terminal = read_json(state / "verification-jobs" / f"{identifier}.json")
    job = pending or terminal
    with contextlib.ExitStack() as stack:
        stack.enter_context(locked(state / "verification-jobs" / f"{identifier}.lock"))
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
        if not job:
            return "unknown-owner"  # interrupted copies need reconciliation, never blind deletion
        if wr.live_users(tree):
            return "active"
        mounts = mount_points()
        if mounts is None or any(point == tree or point.is_relative_to(tree) for point in mounts):
            return "mounted"
        progress = read_json(journal)
        for repo, name in _builds(tree, work):
            cache = repo / name
            if cache.is_symlink() or not cache.is_dir():
                continue
            # Never remove a tracked build directory, nor a symlinked intermediate .lake.
            if (repo / ".lake").is_symlink():
                continue
            top = wr._git(repo, "rev-parse", "--show-toplevel", text=True).strip()
            if Path(top).resolve() != repo.resolve() or wr._git(repo, "ls-files", "-z", "--", name):
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
        # Shrink a reservation only after a complete post-eviction inventory. The generator
        # resumes between ticks; a restart discards it and starts a fresh read-only count.
        with ledger() as data:
            generation = data["candidates"][key(state, identifier)]["last_used"]
        identity = (root().resolve(), state, identifier, generation)
        for old in list(_SCANS):
            if old[:3] == identity[:3] and old != identity:
                _SCANS.pop(old)[0].close()
        scan, size = _SCANS.get(identity, (None, 0))
        if scan is None:
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
                    row.update(reserved=max(FLOOR, size), compact=True, compacted_at=now)
            journal.unlink(missing_ok=True)
            return "source-retained"
        finally:
            if scan.gi_frame is not None:
                _SCANS[identity] = scan, size


def _measure_checkpoint(state, row, work):
    target = state / "verification-checkpoints" / row["id"]
    if target.is_symlink() or target.parent.is_symlink() or not (target / "checkpoint.json").is_file():
        return "source-checkpoint-retained"
    identity = (root().resolve(), state, row["id"], "checkpoint")
    scan, size = _SCANS.get(identity, (None, 0))
    if scan is None:
        scan = _sizes(target)
    try:
        while True:
            work.charge(entries=1)
            size += next(scan)
    except StopIteration:
        _SCANS.pop(identity, None)
        with ledger() as data:
            data["candidates"][key(state, row["id"])].update(reserved=max(FLOOR, size), compact=True, legacy=False)
        return "source-checkpoint-retained"
    finally:
        if scan.gi_frame is not None:
            _SCANS[identity] = scan, size


def _housekeep(states=(), *, seconds=0.25, entries=2048, removal_bytes=256 * 1024**2, now=None):
    """Ordinary supervisor/activity ticks own cleanup. No timer, model or GitHub reads."""
    from . import work_recovery as wr

    now = time.time() if now is None else now
    work = wr.Work(seconds=seconds, entries=entries, removal_bytes=removal_bytes)
    report = dict(at=now, candidates={}, errors=[])
    token = wr._WORK.set(work)
    try:
        for state in states:
            discover(state)
        with ledger() as data:
            rows = list(data["candidates"].items())
        try:
            free_space(root())
            pressure = sum(row["reserved"] for _, row in rows) >= setting("STORAGE_BYTES", 128 * GIB)
        except Pressure:
            pressure = True
        # Cursor rotation prevents an active or very large oldest candidate starving others.
        cursor_path = root() / "cursor.json"
        last = read_json(cursor_path).get("last", "")
        rows.sort(key=lambda item: (item[0] <= last, item[1].get("last_used", 0), item[0]))
        for identity, row in rows:
            work.charge(entries=1)
            atomic_json(cursor_path, dict(last=identity))
            state, identifier = Path(row["state"]), row["id"]
            if row.get("kind") == "checkpoint":
                if row.get("legacy"):
                    report["candidates"][identity] = _measure_checkpoint(state, row, work)
                continue
            terminal_path = state / "verification-jobs" / f"{identifier}.json"
            terminal = terminal_path.exists()
            ttl = setting("COMPLETED_CACHE_SECONDS", 3600) if terminal else setting("IDLE_CACHE_SECONDS", 86400)
            last_used = max(row["last_used"], terminal_path.stat().st_mtime) if terminal else row["last_used"]
            if not pressure and (row.get("compact") or now - last_used < ttl):
                continue
            try:
                report["candidates"][identity] = _compact(state, identifier, work, now=now)
            except BlockingIOError:
                report["candidates"][identity] = "active"
            except (OSError, ValueError, wr.RecordError) as error:
                if isinstance(error, wr.BudgetExhausted):
                    raise
                report["errors"].append(str(error)[:300])
    except wr.BudgetExhausted:
        report["next_action"] = "resume-verification-storage-housekeeping"
    except (OSError, ValueError, Pressure) as error:
        report["errors"].append(str(error)[:300])
    finally:
        wr._WORK.reset(token)
    report["work"] = work.report()
    with contextlib.suppress(OSError, Pressure):
        with ledger() as data:
            report["reserved_bytes"] = sum(row["reserved"] for row in data["candidates"].values())
            report["candidate_count"] = len(data["candidates"])
            report["source_only_count"] = sum(bool(row.get("compact")) for row in data["candidates"].values())
        report["limit_bytes"] = setting("STORAGE_BYTES", 128 * GIB)
        report["free_bytes"] = shutil.disk_usage(root()).free
    # Prepared data only: status commands never traverse retained candidates.
    with contextlib.suppress(OSError):
        atomic_json(root() / "status.json", report)
    return report


def housekeep(states=(), **budgets):
    """One host collector per ten seconds; overlapping supervisors never repeat the work."""
    states = tuple(states)
    if not root().exists() and not any((state / "verification-candidates").is_dir() for state in states):
        return {}
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "housekeeping.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(busy=True)
        schedule = directory / "housekeeping.json"
        if budgets.get("now") is None and read_json(schedule).get("next_at", 0) > time.time():
            return read_json(directory / "status.json")
        atomic_json(schedule, dict(next_at=time.time() + 10))
        return _housekeep(states, **budgets)

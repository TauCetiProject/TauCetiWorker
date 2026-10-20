"""Bounded activity housekeeping for recovery descriptors and retained trees.

Jobs survive process death. Directory enumerators and inventory counts resume between ordinary
activity ticks; a preparer restart restarts the read-only inventory sweep. Destructive traversal
has a durable depth-first cursor and never follows links or crosses mounts. No GitHub access.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from . import recovery_index
from .runtime_status import atomic_json, read_json

CONSUMER_STATES = (
    "retained",
    "eligible",
    "resuming",
    "verified",
    "publication_pending",
    "published",
    "obsolete",
    "blocked",
)
COUNTS = (
    "records",
    "retained",
    "blocked",
    "preserved",
    "reset",
    "superseded",
    "incomplete",
    "corrupt",
    "errors",
) + tuple("recovery_" + state for state in CONSUMER_STATES)
_SWEEPS = {}


def _metadata(path, limit=256 * 1024):
    from .work_recovery import _charge

    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            _charge(entries=1)
            if size > limit:
                return {}
            _charge(read=size)
            value = json.loads(handle.read(size))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _name(path):
    return hashlib.sha256(str(path).encode()).hexdigest()


def register(state, parent, label, repository):
    """Register before moving a checkout: a crash cannot orphan its discovery root."""
    parent = parent.resolve()
    path = state / "recovery-cleanup" / "roots" / f"{_name(parent)}.json"
    expected = dict(version=1, parent=str(parent), label=label, repository=repository)
    if _metadata(path) == expected:
        return
    atomic_json(
        path,
        expected,
    )


def defer_caches(state, record):
    """Terminal recovery transitions queue metadata only; housekeeping owns recursive eviction."""
    from .activity import current_round_id
    from .work_recovery import process_owner

    path = state / "recovery-cleanup" / "cache-roots" / f"{_name(record)}.json"
    previous = _metadata(path)
    journal = _metadata(record / "recovery.json")
    generation = {
        key: journal.get(key)
        for key in ("content_started", "source_checkpoint", "content_attempts", "external", "attempts")
    }
    generation["cache_use"] = _metadata(record / "cache-use.json").get("generation")
    if previous.get("version") == 1 and previous.get("generation") == generation:
        return
    owner = dict(round_id=current_round_id(), process=process_owner())
    atomic_json(
        path,
        dict(
            version=1,
            record=str(record),
            owner=owner,
            generation=generation,
            completed={},
            created_at=previous.get("created_at", time.time()),
        ),
    )


def _cleanup_cache_tree(state, tree, registry, work, observation):
    from . import work_recovery as wr

    path = Path(registry["registry"])
    # Reload the small registry: earlier trees in this sweep may have completed.
    root = _metadata(path)
    generation = root.get("generation")
    # A resumed consumer invalidates prior eviction before touching its checkout. Hold its
    # recovery.lock while checking the generation and removing caches.
    if not isinstance(generation, dict) or generation.get("cache_use") != _metadata(tree.parent / "cache-use.json").get(
        "generation"
    ):
        observation["next_action"] = "await-cache-generation"
        return
    if not isinstance(root.get("completed"), dict):
        root["completed"] = {}
    info = tree.stat()
    identity = [info.st_dev, info.st_ino]
    if root["completed"].get(tree.name) == identity:
        observation["done"] = True
        return
    job_path = state / "recovery-cleanup" / "cache-jobs" / f"{_name(tree)}.json"
    job = _metadata(job_path)
    if job.get("generation") != generation:
        job = dict(generation=generation)
    job.setdefault("created_at", root.get("created_at", time.time()))
    try:
        if reason := _safe(state, tree, root.get("owner", {})):
            job["next_action"] = reason
            return
        if _evict_caches(tree, job, work):
            root.setdefault("completed", {})[tree.name] = identity
            atomic_json(path, root)
            job_path.unlink(missing_ok=True)
            job["done"] = True
        else:
            job["next_action"] = "resume-cache-removal"
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, wr.RecordError) as error:
        job["next_action"] = "retry-cache-removal"
        job["error"] = str(error)[:300]
        if isinstance(error, wr.BudgetExhausted):
            raise
    finally:
        observation.update(job)
        if not job.get("done"):
            atomic_json(job_path, job)


def _referenced(state, tree):
    """Honor pending verification references with the same ordinary housekeeping budget."""
    from .work_recovery import _charge

    targets = {tree.resolve()}
    if tree.name.endswith(".deleting"):
        targets.add(tree.with_name(tree.name.removesuffix(".deleting")).resolve())
    for source in (state, state / "verification-state"):
        directory = source / "verification-pending"
        if not directory.is_dir():
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                _charge(entries=1)
                if entry.name.startswith(".") or not entry.name.endswith(".json"):
                    continue  # interrupted atomic writes never became pending jobs
                job = _metadata(Path(entry.path))
                if not job:
                    return True  # malformed reference withholds only this destructive candidate
                for key in ("lease", "receipt", "lint_modules", "root"):
                    value = job.get(key)
                    if isinstance(value, str):
                        reference = Path(value)
                        if reference.is_relative_to("/opt/verification-state"):
                            reference = source / reference.relative_to("/opt/verification-state")
                        if not reference.is_absolute():
                            reference = source / reference
                        if any(reference.resolve().is_relative_to(target) for target in targets):
                            return True
    return False


def _safe(state, tree, owner):
    """Ordinary stopped-owner and live-reference checks; uncertainty skips only this tree."""
    from . import work_recovery as wr

    birth = owner.get("process") if isinstance(owner, dict) else None
    if not wr.owner_stopped(birth):
        return "await-owner"
    if _referenced(state, tree):
        return "await-verification-reference"
    session = birth.get("session")
    if session is not None:
        if not isinstance(session, int) or session <= 0:
            return "await-owner"
        try:
            if birth["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                session = None
            else:
                try:
                    fields = Path(f"/proc/{session}/stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[3]) == session and fields[19] != birth["start"]:
                        session = None  # a new session reused the old leader's PID
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (OSError, ValueError, IndexError):
            return "await-owner"
    if tree.is_symlink() or wr.live_users(tree, session=session):
        return "await-live-users"
    # Protect visible mounts, including bind mounts on the same device. An unavailable
    # mount table withholds this candidate, without affecting work admission.
    try:
        raw = Path("/proc/self/mountinfo").read_text()
        wr._charge(read=len(raw))
        target = os.path.realpath(tree)
        for line in raw.splitlines():
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
            if mount == target or mount.startswith(target + "/"):
                return "await-mount-lease"
    except (OSError, IndexError):
        return "await-mount-evidence"
    return None


def _job_path(state, tree):
    return state / "recovery-cleanup" / "jobs" / f"{_name(tree)}.json"


def _write_job(path, job):
    job["version"] = 1
    job.pop("checksum", None)
    job["checksum"] = recovery_index.checksum(job)
    atomic_json(path, job)


def _read_job(path):
    job = _metadata(path)
    checksum = job.pop("checksum", None)
    if (
        job.get("version") != 1
        or checksum != recovery_index.checksum(job)
        or job.get("phase") not in {"save", "delete"}
        or not isinstance(job.get("stack", []), list)
        or any(not isinstance(part, str) for part in job.get("stack", []))
    ):
        return {}
    return job


def _open_owned_at(parent, name, flags):
    try:
        return os.open(name, flags, dir_fd=parent)
    except PermissionError:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_dev != os.fstat(parent).st_dev:
            raise
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
        return os.open(name, flags, dir_fd=parent)


def _open_dir(root, relative):
    """Open each component O_NOFOLLOW; a swapped ancestor cannot redirect traversal."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags)
    except PermissionError:
        parent = os.open(root.parent, flags)
        try:
            fd = _open_owned_at(parent, root.name, flags)
        finally:
            os.close(parent)
    try:
        for part in Path(relative).parts:
            if part in {"..", "/"}:
                raise ValueError("unsafe cleanup cursor")
            child = _open_owned_at(fd, part, flags)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove(tree, job, work):
    """Resume after each unlink/truncation. The saved descriptor is never modified.

    Large single files are shortened in byte-budget slices before unlink, only with a unique
    regular inode. This avoids turning one multi-GB cache file into an unbounded deletion unit.
    """
    stack = job.setdefault("stack", [""])
    dirty = set()
    root_stat = tree.lstat()
    if [root_stat.st_dev, root_stat.st_ino] != job["tree_identity"] or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("cleanup tree identity changed")
    try:
        return _remove_slice(tree, job, work, stack, root_stat, dirty)
    finally:
        for relative in dirty:
            with contextlib.suppress(FileNotFoundError):
                fd = _open_dir(tree, relative)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)


class _DirectoryReader:
    def __init__(self, tree):
        self.tree, self.relative, self.fd = tree, None, None

    def open(self, relative):
        if self.fd is None or relative != self.relative:
            self.close()
            self.fd = _open_dir(self.tree, relative)
            self.relative = relative
        os.lseek(self.fd, 0, os.SEEK_SET)
        return os.dup(self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _remove_slice(tree, job, work, stack, root_stat, dirty):
    reader = _DirectoryReader(tree)
    try:
        while stack:
            work.charge(entries=1)
            relative = stack[-1]
            try:
                fd = reader.open(relative)
            except FileNotFoundError:
                stack.pop()  # crash after rmdir but before saving the cursor
                continue
            try:
                directory = os.fstat(fd)
                if directory.st_dev != root_stat.st_dev:
                    raise ValueError("cleanup reached another filesystem")
                if directory.st_uid == os.geteuid() and directory.st_mode & 0o300 != 0o300:
                    # Immutable private copies may have readable but unwritable directories.
                    # Operate on the already opened directory, never a replacement/symlink.
                    os.fchmod(fd, directory.st_mode | 0o300)
                with os.scandir(fd) as entries:
                    entry = next(entries, None)
                if entry is None:
                    if relative:
                        parent = str(Path(relative).parent)
                        parent_fd = _open_dir(tree, "" if parent == "." else parent)
                        try:
                            os.rmdir(Path(relative).name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                    else:
                        tree.rmdir()
                        from .work_recovery import _fsync_dir

                        _fsync_dir(tree.parent)
                    stack.pop()
                    continue
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(str(Path(relative) / entry.name))
                    continue
                # Unlinking one of several hardlinks changes no payload bytes. Never truncate the
                # shared inode: the other link may be live source outside this retained tree.
                size = info.st_size if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else 0
                remaining = work.removal_remaining
                if size > remaining:
                    if remaining <= 0:
                        return False
                    try:
                        child = os.open(entry.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    except PermissionError:
                        # Private copies of Git packs and immutable cache payloads can be
                        # read-only. Change only an opened, owned, unique inode; a hardlink
                        # or replaced path must never change another workspace's permissions.
                        control = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        try:
                            current = os.fstat(control)
                            if (current.st_dev, current.st_ino, current.st_nlink, current.st_uid) != (
                                info.st_dev,
                                info.st_ino,
                                1,
                                os.geteuid(),
                            ):
                                raise ValueError("cleanup read-only inode changed or is shared") from None
                            os.fchmod(control, current.st_mode | stat.S_IWUSR)
                        finally:
                            os.close(control)
                        child = os.open(entry.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        current = os.fstat(child)
                        if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1):
                            raise ValueError("cleanup inode changed")
                        work.charge(removed=remaining)
                        os.ftruncate(child, size - remaining)
                        os.fsync(child)
                    finally:
                        os.close(child)
                    return False
                work.charge(removed=size)
                os.unlink(entry.name, dir_fd=fd)
                dirty.add(relative)
            finally:
                os.close(fd)
        return True

    finally:
        reader.close()


def _evict_caches(tree, job, work):
    """Only known top-level, untracked caches. Unknown Git metadata never permits eviction."""
    from . import work_recovery as wr

    top = wr._git(tree, "rev-parse", "--show-toplevel", text=True).strip()
    if os.path.realpath(top) != os.path.realpath(tree):
        raise wr.RecordError("cache candidate is not its own Git checkout")
    for name in sorted(wr.CACHE_DIRS | wr.CACHE_TOP):
        work.charge(entries=1)
        cache = tree / name
        if cache.is_symlink() or not cache.is_dir():
            continue
        if wr._git(tree, "ls-files", "-z", "--", name):
            continue  # tracked bytes remain source regardless of their directory's name
        jobs = job.setdefault("caches", {})
        info = cache.stat()
        subjob = jobs.setdefault(name, dict(tree_identity=[info.st_dev, info.st_ino], stack=[""]))
        if _remove(cache, subjob, work):
            jobs.pop(name, None)
        else:
            return False
    return True


def _promote(job, work):
    """One occasionally larger source candidate, with persisted backoff and hard ceilings."""
    if not work.adaptive or not job.get("allowance_level"):
        return True
    if time.time() < job.get("retry_at", 0) or work.promoted:
        return False
    level = min(job["allowance_level"], 6)
    work.seconds = time.monotonic() - work.started + min(30, 0.25 * 4**level)
    work.entries = work.metadata_entries + min(262144, 4096 * 4**level)
    work.bytes = work.read_bytes + min(1024**3, 16 * 1024**2 * 4**level)
    work.promoted = True
    return True


def _unarchived_state(tree, snap):
    """v1 captures branch source, but not credentials, native operations or extra Git history."""
    from . import work_recovery as wr

    if not (tree / ".git").is_dir() or (tree / ".git").is_symlink():
        return True
    if snap.operations or any("credential" in reason for _, reason in snap.excluded):
        return True
    if wr._git(tree, "for-each-ref", "--format=%(refname)", "refs/stash", "refs/notes").strip():
        return True
    # The history-aware snapshot includes extra ref/reflog tips in this descriptor's v1 bundle.
    return False


def _park_signature(tree, sidecar):
    """Constant-size metadata probe; no traversal of parked source or historical payloads."""
    from .work_recovery import _charge

    paths = [
        tree,
        sidecar,
        *(
            tree / ".git" / name
            for name in (
                "",
                "HEAD",
                "packed-refs",
                "refs",
                "refs/tags",
                "refs/notes",
                "refs/stash",
                "logs",
                "logs/HEAD",
                "refs/heads",
                "logs/refs",
                "info/exclude",
            )
        ),
    ]
    result = []
    for path in paths:
        _charge(entries=1)
        try:
            info = path.lstat()
            result.append([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        except FileNotFoundError:
            result.append(None)
    return result


def _park(job, tree, sidecar):
    job.setdefault("allowance_level", 0)
    job.update(
        next_action="retain-unarchived-metadata",
        parked=_park_signature(tree, sidecar),
        retry_at=time.time() + 86400,
    )


def _cleanup(state, tree, root, work, observation=None):
    from . import work_recovery as wr

    # A naked *.deleting marker is not permission to destroy anything. Its original job must
    # carry a durable completed descriptor and owner evidence, including after a restart.
    original = tree.with_name(tree.name.removesuffix(".deleting"))
    path = _job_path(state, original)
    job = _read_job(path)
    if not job:
        job = dict(
            tree=str(original), phase="save", created_at=tree.stat().st_mtime, next_action="preserve-source", stack=[""]
        )
    if job.get("tree") != str(original):
        return
    sidecar = original.with_name(original.name + ".owner.json")
    owner = _metadata(sidecar)
    saving_source = False
    validating_source = False
    try:
        if (
            job.get("parked")
            and job["parked"] == _park_signature(tree, sidecar)
            and time.time() < job.get("retry_at", 0)
        ):
            return
        if job.pop("parked", None) is not None:
            job["retry_at"] = 0  # a metadata change can retry immediately at its last successful allowance
        # Reject failed/incomplete repositories before any process or mount traversal.
        if job["phase"] == "save" or not tree.name.endswith(".deleting"):
            valid = (tree / ".git").is_dir() and not (tree / ".git").is_symlink()
            if valid:
                try:
                    top = wr._git(tree, "rev-parse", "--show-toplevel", text=True).strip()
                    valid = os.path.realpath(top) == os.path.realpath(tree)
                except wr.BudgetExhausted:
                    raise
                except (OSError, wr.RecordError, subprocess.SubprocessError):
                    valid = False
            if not valid:
                _park(job, tree, sidecar)
                return
        reason = _safe(state, tree, owner)
        if reason:
            job["next_action"] = reason
            return
        if not _promote(job, work):
            job["next_action"] = "await-preservation-allowance"
            return
        just_saved = False
        if job["phase"] == "save":
            cfg = SimpleNamespace(state=state, wid=state.name)
            if not _evict_caches(tree, job, work):
                job["next_action"] = "resume-cache-removal"
                return
            saving_source = True
            snap = wr.inspect(tree, history=True)
            if snap.published:
                wr._handoff_source_receipts(cfg, owner, None, checkout=tree)
                owner = {**owner, "recovery_handoffs": []}
            record = wr._record(
                cfg,
                tree,
                snap,
                owner.get("label", root["label"]),
                owner.get("repository", root["repository"]),
                owner=owner,
            )
            if _unarchived_state(tree, snap):
                _park(job, tree, sidecar)
                return  # v1 records do not preserve credentials or native operation metadata
            if _safe(state, tree, owner) or wr.inspect(tree, history=True).fingerprint != snap.fingerprint:
                job["next_action"] = "retry-changing-source"
                return
            manifest = wr._complete(record)
            if manifest is None:
                job["next_action"] = "retry-invalid-preservation"
                return
            job.update(
                phase="delete",
                record=record.name,
                evidence=recovery_index.stamps(record, manifest),
                tree_identity=[tree.stat().st_dev, tree.stat().st_ino],
                next_action="resume-removal",
                allowance_level=0,
                retry_at=0,
            )
            just_saved = True
            _write_job(path, job)  # durable BEFORE rename, including its directory chain
            wr._fsync_dir(path.parent)
        else:
            validating_source = True
            record_id = job.get("record", "")
            if not record_id or Path(record_id).name != record_id or record_id.startswith("."):
                raise ValueError("invalid cleanup descriptor")
            record = state / "recovery" / record_id
            manifest = wr._read_manifest(record)
            if manifest is None:
                job["next_action"] = "retry-invalid-preservation"
                return
            evidence = recovery_index.stamps(record, manifest)
            if evidence != job.get("evidence"):
                if wr._complete(record) is None or recovery_index.stamps(record, manifest) != evidence:
                    job["next_action"] = "retry-invalid-preservation"
                    return
                job["evidence"] = evidence
        if not tree.name.endswith(".deleting"):
            # Crash before rename: the source may have changed since the deletion job was written.
            # Compare again because a late source change invalidates the deletion receipt.
            validating_source = not just_saved
            if not just_saved:
                snap = wr.inspect(tree, history=True)
                if snap.fingerprint != manifest["fingerprint"]:
                    job.update(phase="save", next_action="preserve-changed-source", stack=[""])
                    return
                if _unarchived_state(tree, snap):
                    _park(job, tree, sidecar)
                    return
        saving_source = validating_source = False
        job.update(allowance_level=0, retry_at=0)
        if reason := _safe(state, tree, owner):
            job["next_action"] = reason
            return
        if not tree.name.endswith(".deleting"):
            dest = tree.with_name(tree.name + ".deleting")
            os.rename(tree, dest)
            wr._fsync_dir(tree.parent)
            tree = dest
        if _remove(tree, job, work):
            if not wr._set_status(record, "checkout-reset", "retained checkout saved and removed by housekeeping"):
                job["next_action"] = "retry-finalization"
                _write_job(path, job)
                return
            original.with_name(original.name + ".owner.json").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return
        job["next_action"] = "resume-removal"
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, wr.RecordError) as error:
        job["next_action"] = "resume-removal" if job["phase"] == "delete" else "retry-preservation"
        job["error"] = str(error)[:300]
        if isinstance(error, wr.HistoryLimit):
            _park(job, tree, sidecar)
            return
        if isinstance(error, wr.BudgetExhausted):
            if work.adaptive and (
                saving_source or validating_source or (job["phase"] == "save" and not work.removed_bytes)
            ):
                job["allowance_level"] = min(6, job.get("allowance_level", 0) + 1)
                job["retry_at"] = time.time() + min(1800, 60 * 2 ** (job["allowance_level"] - 1))
            raise
    finally:
        if observation is not None:
            observation.update(job)
        if tree.exists():
            _write_job(path, job)


class Sweep:
    def __init__(self, base):
        self.base = base
        self.rows = {}
        self.items = self.scan()
        self.pending = None
        self.current_registration = {}
        self.registered_roots = {}

    def scan(self):
        if not self.base.is_dir():
            return
        with os.scandir(self.base) as workers:
            for entry in workers:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield from self.scan_worker(Path(entry.path))
                except (OSError, ValueError, TypeError, KeyError) as error:
                    previous = read_json(Path(entry.path) / "recovery-prepared.json")
                    previous.setdefault("reasons", []).append(f"inventory incomplete: {str(error)[:200]}")
                    previous["reasons"] = previous["reasons"][-8:]
                    with contextlib.suppress(OSError):
                        atomic_json(Path(entry.path) / "recovery-prepared.json", previous)
                    continue  # one unreadable worker must not starve later workers

    def scan_worker(self, state):
        legacy_parent = self.base.parent / "checkouts" / state.name / "retained-checkouts"
        known = (
            (legacy_parent, "TauCeti"),
            (state / "retained-checkouts", "TauCeti"),
            (state / "progress" / "retained-checkouts", "progress-roadmap"),
        )
        from .constants import ROADMAP, TAUCETI

        for parent, label in known:
            if parent.is_dir() and not parent.is_symlink():
                register(state, parent, label, ROADMAP if label == "progress-roadmap" else TAUCETI)
        if not (
            (state / "recovery").is_dir()
            or (state / "recovery-cleanup").is_dir()
            or (state / "recovery-preparation.json").exists()
        ):
            return
        self.registered_roots[state] = set()
        self.rows[state] = dict.fromkeys(COUNTS, 0) | {
            "reasons": [],
            "cleanup_backlog": 0,
            "cleanup_oldest_at": None,
            "cleanup_actions": {},
        }
        for category, directory in (("cache", "cache-roots"), ("retained", "roots")):
            registrations = state / "recovery-cleanup" / directory
            if registrations.is_dir():
                with os.scandir(registrations) as registered:
                    for descriptor in registered:
                        yield state, "registration", Path(descriptor.path), dict(category=category)
                        registry = self.current_registration
                        if not registry:
                            continue
                        parent = Path(registry["record"] if category == "cache" else registry["parent"])
                        with contextlib.suppress(OSError):
                            with os.scandir(parent) as trees:
                                for tree in trees:
                                    yield state, category + "-entry", Path(tree.path), registry
        jobs = state / "recovery-cleanup" / "jobs"
        if jobs.is_dir():
            with os.scandir(jobs) as queued:
                for path in queued:
                    yield state, "job", Path(path.path), None
        yield state, "cleanup-finished", None, None
        root = state / "recovery"
        if root.is_dir():
            with os.scandir(root) as records:
                for record in records:
                    yield state, "record", Path(record.path), None
        yield state, "finished", None, None

    def process(self, state, kind, path, root, work):
        from . import work_recovery as wr

        row = self.rows[state]
        if kind == "registration":
            self.current_registration = {}
            registry = _metadata(path)
            if registry.get("version") != 1:
                return
            if root["category"] == "cache":
                name = registry.get("record")
                if not isinstance(name, str) or Path(name).parent != state / "recovery" or Path(name).is_symlink():
                    return
                registry["registry"] = str(path)
            else:
                name = registry.get("parent")
                if (
                    not isinstance(name, str)
                    or not Path(name).is_absolute()
                    or Path(name).name != "retained-checkouts"
                    or Path(name).is_symlink()
                    or not all(k in registry for k in ("label", "repository"))
                ):
                    return
            # Older index roots may use different aliases for the same directory. Enumerate
            # its inode only once so deletion has one journal/cursor after a partial rename.
            parent = Path(name).resolve()
            try:
                info = parent.stat()
            except FileNotFoundError:
                return
            identity = (root["category"], info.st_dev, info.st_ino)
            if identity in self.registered_roots[state]:
                return
            self.registered_roots[state].add(identity)
            registry["record" if root["category"] == "cache" else "parent"] = str(parent)
            self.current_registration = registry
            return
        if kind in {"cache-entry", "retained-entry"}:
            if path.is_symlink() or not path.is_dir():
                return
            if kind == "cache-entry":
                if path.name != "checkout" and not path.name.startswith(("restore-", "interrupted-restore-")):
                    return
                kind = "cache"
            else:
                kind = "tree"
        if kind == "cleanup-finished":
            previous = read_json(state / "recovery-prepared.json")
            previous.update({key: value for key, value in row.items() if key.startswith("cleanup_")})
            previous["cleanup_at"] = time.time()
            atomic_json(state / "recovery-prepared.json", previous)
            return
        if kind == "finished":
            row["inventory_at"] = time.time()
            atomic_json(state / "recovery-prepared.json", row)
            return
        if kind == "job":
            job = _read_job(path)
            original = Path(job.get("tree", "."))
            if (
                not original.is_absolute()
                or original.parent.name != "retained-checkouts"
                or path != _job_path(state, original)
            ):
                return
            if not original.exists() and not original.with_name(original.name + ".deleting").exists():
                if job.get("phase") == "save":
                    path.unlink(missing_ok=True)
                    return
                record_id = job.get("record", "")
                if not record_id or Path(record_id).name != record_id or record_id.startswith("."):
                    return
                record = state / "recovery" / record_id
                manifest = wr._complete(record, digests=False)
                valid = manifest is not None
                if valid:
                    evidence = recovery_index.stamps(record, manifest)
                    if evidence != job.get("evidence"):
                        valid = wr._complete(record) is not None and recovery_index.stamps(record, manifest) == evidence
                if valid and wr._set_status(
                    record, "checkout-reset", "retained checkout saved and removed by housekeeping"
                ):
                    original.with_name(original.name + ".owner.json").unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                else:
                    job["next_action"] = "retry-invalid-preservation"
                    _write_job(path, job)
                    row["cleanup_backlog"] += 1
                    oldest = job.get("created_at", time.time())
                    row["cleanup_oldest_at"] = min(row["cleanup_oldest_at"] or oldest, oldest)
                    row["cleanup_actions"][job["next_action"]] = row["cleanup_actions"].get(job["next_action"], 0) + 1
            return
        if kind == "record":
            if not path.is_dir() or path.is_symlink():
                return
            if path.name.startswith(".tmp-"):
                row["incomplete"] += 1
                return
            manifest = wr._complete(path, digests=False)
            if manifest is None:
                row["corrupt"] += 1
                return
            with contextlib.suppress(OSError):
                recovery_index.remember(state, path, manifest, verified=False)
            checkout = Path(manifest["checkout"])
            if checkout.is_absolute() and (checkout.parent / wr.RETAINED_DIR).is_dir():
                label = "progress-roadmap" if checkout.name == "roadmap" else "TauCeti"
                register(state, checkout.parent / wr.RETAINED_DIR, label, manifest.get("repository", ""))
            row["records"] += 1
            consumer = read_json(path / "recovery.json")
            if consumer.get("schema") == "tauceti-work-recovery-consumer/v1":
                key = "recovery_" + str(consumer.get("state")).replace("-", "_")
                if key in row:
                    row[key] += 1
                if consumer.get("state") == "blocked":
                    row["blocked"] += 1
                if consumer.get("reason") and len(row["reasons"]) < 8:
                    row["reasons"].append(f"{state.name}/{path.name}: {wr.describe_reason(consumer['reason'])}")
                return
            status = read_json(path / "status.json")
            disposition = {
                "retained": "retained",
                "recovery-blocked": "blocked",
                "checkout-reset": "reset",
                "superseded": "superseded",
            }.get(status.get("state"), "preserved")
            row[disposition] += 1
            if disposition in {"retained", "blocked"} and len(row["reasons"]) < 8:
                row["reasons"].append(f"{state.name}: {status.get('reason', 'checkout moved aside')}")
            return
        observation = {}
        try:
            try:
                if kind == "cache":
                    # Share the consumer's existing per-record lock. Cleanup never acquires
                    # round.lock or reserves the admission path for unrelated work.
                    with (path.parent / "recovery.lock").open("a+") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        _cleanup_cache_tree(state, path, root, work, observation)
                else:
                    _cleanup(state, path, root, work, observation)
            except BlockingIOError:
                observation["next_action"] = "await-recovery-lock"
        finally:
            if (kind == "cache" and not observation.get("done")) or (
                kind != "cache" and (path.exists() or path.with_name(path.name + ".deleting").exists())
            ):
                row["cleanup_backlog"] += 1
                job = observation
                oldest = job.get("created_at", time.time())
                row["cleanup_oldest_at"] = min(row["cleanup_oldest_at"] or oldest, oldest)
                action = job.get("next_action", "retry-cleanup")
                row["cleanup_actions"][action] = row["cleanup_actions"].get(action, 0) + 1


def prepare(base, *, seconds=0.25, entries=4096, bytes=16 * 1024 * 1024):
    from . import work_recovery as wr

    if os.environ.get("TAUCETI_WORK_RECOVERY", "").lower() == "off":
        return dict(disabled=True)
    adaptive = (seconds, entries, bytes) == (0.25, 4096, 16 * 1024 * 1024)
    work = wr.Work(
        seconds=seconds,
        entries=entries,
        bytes=bytes,
        adaptive=adaptive,
        removal_bytes=256 * 1024 * 1024 if adaptive else None,
    )
    token = wr._WORK.set(work)
    base = Path(base)
    try:
        base.mkdir(parents=True, exist_ok=True)
        if adaptive:
            usage = shutil.disk_usage(base)
            if usage.free < max(5 * 1024**3, usage.total // 20):
                work.removal_bytes = 1024**3
                work.entries = 8192
        with (base / ".recovery-housekeeping.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(busy=True)
            sweep = _SWEEPS.setdefault(base, Sweep(base))
            while True:
                work.charge(entries=1)
                try:
                    item = sweep.pending or next(sweep.items)
                    sweep.pending = item
                except StopIteration:
                    _SWEEPS.pop(base, None)
                    break
                try:
                    sweep.process(*item, work)
                except wr.BudgetExhausted:
                    raise
                except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, wr.RecordError) as error:
                    # A broken item is visible, but cannot freeze this worker's prepared inventory.
                    row = sweep.rows.get(item[0])
                    if row is not None:
                        row["errors"] += 1
                        if len(row["reasons"]) < 8:
                            row["reasons"].append(f"{item[0].name}/{item[2]}: {str(error)[:200]}")
                    sweep.pending = None
                    continue
                finally:
                    # Tree jobs have durable cursors; yield to unrelated jobs on the next tick.
                    if item[1] in {"tree", "cache", "retained-entry", "cache-entry"}:
                        sweep.pending = None
                sweep.pending = None
    except wr.BudgetExhausted:
        pass
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, wr.RecordError):
        _SWEEPS.pop(base, None)  # retry automatically; no marker-removal/operator action
    finally:
        wr._WORK.reset(token)
    report = work.report()
    with contextlib.suppress(OSError):
        atomic_json(base / ".recovery-housekeeping.json", dict(at=time.time(), **report))
    return report


def cached_summary(base, *, workers=None):
    result = dict.fromkeys(COUNTS, 0) | dict(
        reasons=[],
        cleanup_backlog=0,
        cleanup_oldest_age_seconds=0,
        preparation_seconds=0,
        preparation_read_bytes=0,
        preparation_hash_bytes=0,
        preparation_metadata_entries=0,
        cleanup_actions={},
    )
    if not base.is_dir():
        return result
    for state in base.iterdir():
        if not state.is_dir() or (workers is not None and state.name not in workers):
            continue
        row = read_json(state / "recovery-prepared.json")
        for name in COUNTS:
            result[name] += row.get(name, 0)
        result["reasons"].extend(row.get("reasons", []))
        result["cleanup_backlog"] += row.get("cleanup_backlog", 0)
        for action, count in row.get("cleanup_actions", {}).items():
            result["cleanup_actions"][action] = result["cleanup_actions"].get(action, 0) + count
        if oldest := row.get("cleanup_oldest_at"):
            result["cleanup_oldest_age_seconds"] = max(
                result["cleanup_oldest_age_seconds"], max(0, time.time() - oldest)
            )
        prep = read_json(state / "recovery-preparation.json")
        for name in ("seconds", "read_bytes", "hash_bytes", "metadata_entries"):
            result[f"preparation_{name}"] += prep.get(name, 0)
    result["cleanup"] = read_json(base / ".recovery-housekeeping.json")
    return result

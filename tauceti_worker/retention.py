"""Three-day lifetimes for disposable worker data. No GitHub reads or cache service."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import Config, Die, legacy_project, log, worker_state_root
from .paths import HERE, canonical_candidate, legacy_data_root, self_env
from .runtime_status import atomic_json, read_json

LIFETIME = 3 * 86400
# Agent transcripts are the per-turn record of a round; keep a month of them. Claude prunes its own
# (cleanupPeriodDays in the isolated settings); Codex keeps every session forever.
TRANSCRIPT_LIFETIME = 30 * 86400


def safe(path, root):
    """Only managed paths; aliases are resolved at the root, never inside it."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return False
    return bool(relative.parts)


def open_files():
    """One /proc pass per daily sweep, rather than one pass per old log file."""
    if not Path("/proc/self/fd").is_dir():
        return _open_files_psutil() if sys.platform == "darwin" else None
    opened = set()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            for descriptor in (process / "fd").iterdir():
                with contextlib.suppress(OSError):
                    info = descriptor.stat()
                    opened.add((info.st_dev, info.st_ino))
        except OSError:
            continue
    return opened


# macOS system agents that run as the user yet refuse inspection, and cannot use worker data.
# Not /usr/bin or /bin in general: git, sh and python live there.
_PLATFORM_EXECUTABLES = ("/System/", "/usr/libexec/", "/usr/bin/login")


def _open_files_psutil():
    """macOS has no /proc: observe this user's processes through psutil. Only this user can hold
    this user's private data open. A process that refuses inspection makes the observation
    unavailable, unless it is an operating-system binary that cannot be one of ours."""
    try:
        import psutil
    except ImportError:
        return None
    uid = os.getuid()
    opened = set()
    for process in psutil.process_iter():
        try:
            if process.uids().real != uid:
                continue
            paths = [item.path for item in process.open_files()]
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            try:
                if process.exe().startswith(_PLATFORM_EXECUTABLES):
                    continue
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError):
                pass
            return None  # Missing observations never mean that diagnostics are unused.
        with contextlib.suppress(psutil.Error, OSError, TypeError):
            if cwd := process.cwd():
                paths.append(cwd)
        for path in paths:
            with contextlib.suppress(OSError):
                info = os.stat(path)
                opened.add((info.st_dev, info.st_ino))
    return opened


def workspace_idle(cfg, opened):
    if opened is None:
        return False
    try:
        info = (cfg.state / "instance.lock").stat()
    except FileNotFoundError:
        return True
    # FD and path stats use the same device numbering, including subvolumes/overlay filesystems.
    # Merely opening the instance lock counts as live; no collector ever takes that lock.
    return (info.st_dev, info.st_ino) not in opened


def children(path, report):
    try:
        yield from path.iterdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        note(report, "errors", f"{path}: {error}")


def note(report, key, value):
    report[key + "_count"] = report.get(key + "_count", 0) + 1
    if len(report[key]) < 100:
        report[key].append(value)


@contextlib.contextmanager
def locked(path, *, dry=False):
    if dry and not path.exists():
        yield None
        return
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("r" if dry else "a+") as stream:
        fcntl.flock(stream, (fcntl.LOCK_SH if dry else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        if not dry:
            yield stream
            return
    yield None  # observation probes never keep a lease while scanning


def remove(path, root, report, **kwargs):
    try:
        _remove(path, root, report, **kwargs)
    except OSError as error:
        note(report, "errors", f"{path}: {error}")


def mount_points():
    """All visible mounts, including pseudo filesystems; None means unavailable evidence."""
    try:
        if sys.platform == "darwin":
            import psutil

            # getfsstat supplies kernel paths without touching each mounted filesystem.
            return [Path(item.mountpoint) for item in psutil.disk_partitions(all=True)] or None
        if sys.platform != "linux":
            return None

        def unescape(value):
            for escaped, literal in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
                value = value.replace(escaped, literal)
            return value

        return [
            Path(unescape(line.split()[4])) for line in Path("/proc/self/mountinfo").read_text().splitlines()
        ] or None
    except (ImportError, OSError, IndexError, ValueError):
        return None


def _remove(path, root, report, *, dry=False, check_users=True, opened=None, mounts=...):
    """Rename first: a crash leaves an explicitly owned, resumable deletion."""
    if not safe(path, root) or not path.exists():
        return
    from .work_recovery import live_users

    info = path.stat()
    in_use = ((info.st_dev, info.st_ino) in opened) if opened is not None else check_users and live_users(path)
    if mounts is ...:
        mounts = mount_points()
    target = canonical_candidate(path)
    mounted = mounts is None or any(point == target or point.is_relative_to(target) for point in mounts or ())
    if mounted or path.is_mount() or in_use:
        note(report, "protected", str(path))
        return
    if not dry:
        if path.is_file():
            path.unlink()
            note(report, "removed", str(path))
            return
        if path.name.endswith(".retention-deleting"):
            from .shared_mathlib import remove_tree

            remove_tree(path)
            note(report, "removed", str(path))
            return
        trash = path.with_name(path.name + ".retention-deleting")
        if not safe(trash, root):
            raise OSError(f"unsafe deletion staging path: {trash}")
        if trash.exists():
            remove(trash, root, report, dry=dry)
            if trash.exists():
                return  # a protected/failed old deletion must not be bypassed by its replacement
        path.rename(trash)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        from .shared_mathlib import remove_tree

        remove_tree(trash)
    note(report, "removed", str(path))


def fresh_report(now=None):
    return dict(at=now if now is not None else time.time(), removed=[], protected=[], errors=[])


def worker_home(cfg):
    """(isolated home, anchor) of a worker, or None. Never the login home itself."""
    home, anchor = cfg.state / "home", cfg.state
    if sys.platform == "darwin" and legacy_project():
        from .agents import _worker_iso_home

        home = _worker_iso_home(cfg.wid, _base=cfg.home)
        if not safe(home, cfg.home):
            return None
        anchor = home
    return home, anchor


def cache_roots(cfg):
    # Never traverse arbitrary caches in the login home or operator-supplied cache overrides.
    located = worker_home(cfg)
    if located is None:
        return []
    home, anchor = located
    return [(home / ".cache" / name, anchor) for name in ("lake", "mathlib")]


def expire_transcripts(cfg, report, *, now, dry=False, opened=None, mounts=...):
    """Codex sessions in a worker's isolated home older than TRANSCRIPT_LIFETIME."""
    located = worker_home(cfg)
    if located is None:
        return
    home, anchor = located
    sessions = home / ".codex" / "sessions"
    try:
        if sessions.is_symlink() or not sessions.is_dir() or not safe(sessions, anchor):
            return
    except OSError as error:
        note(report, "errors", f"{sessions}: {error}")
        return
    for base, _dirs, files in os.walk(
        sessions, topdown=False, onerror=lambda error: note(report, "errors", str(error))
    ):
        directory = Path(base)
        for name in files:
            path = directory / name
            try:
                if path.is_symlink() or now - path.lstat().st_mtime < TRANSCRIPT_LIFETIME:
                    continue
                remove(path, sessions, report, dry=dry, opened=opened, mounts=mounts)
            except FileNotFoundError:
                continue
            except OSError as error:
                note(report, "errors", f"{path}: {error}")
        if directory != sessions and not dry:
            with contextlib.suppress(OSError):
                directory.rmdir()  # only once empty: the date directories Codex leaves behind


def merge_report(report, result):
    for key in ("removed", "protected", "errors"):
        report[key].extend(result[key])
        report[key + "_count"] = report.get(key + "_count", 0) + result.get(key + "_count", len(result[key]))
        del report[key][100:]


def evict_tree(cfg, tree, anchor, report, **kwargs):
    try:
        _evict_tree(cfg, tree, anchor, report, **kwargs)
    except OSError as error:
        note(report, "errors", f"{tree}: {error}")


def _evict_tree(cfg, tree, anchor, report, *, dry=False, discard=False, owner=None):
    from . import work_recovery as recovery

    if not safe(tree, anchor) or not tree.is_dir() or tree.name.endswith(".retention-deleting"):
        return
    if referenced_attempt(cfg, tree) or recovery.live_users(tree):
        note(report, "protected", str(tree))
        return
    decision = None
    if discard:
        try:
            decision = recovery.save_for_cleanup(cfg, tree, owner=owner, dry_run=dry)
            if not decision.ok:
                note(report, "protected" if decision.state == "protected" else "errors", f"{tree}: {decision.reason}")
        except Exception as error:
            note(report, "errors", f"{tree}: source preservation failed: {error}")
    for base, dirs, _ in os.walk(tree, followlinks=False, onerror=lambda error: note(report, "errors", str(error))):
        dirs[:] = [name for name in dirs if name != ".git" and not (Path(base) / name).is_symlink()]
        caches = [
            name for name in dirs if name in recovery.CACHE_DIRS or (Path(base) == tree and name in recovery.CACHE_TOP)
        ]
        for name in caches:
            remove(Path(base) / name, anchor, report, dry=dry)
        dirs[:] = [name for name in dirs if name not in caches]
    if discard and decision is not None and decision.ok:
        remove(tree, anchor, report, dry=dry)
        if not dry and not tree.exists() and decision.record is not None:
            recovery._set_status(decision.record, "checkout-reset", "saved before retention removed the checkout")


def evict_workspace(cfg, report, *, dry=False):
    """Only the active generation; unrelated retained history never enters a round-start walk."""
    anchor = cfg.checkout.parent.parent if legacy_project() else cfg.state
    evict_tree(cfg, cfg.checkout, anchor, report, dry=dry)
    for path, root in cache_roots(cfg):
        remove(path.with_name(path.name + ".retention-deleting"), root, report, dry=dry)
        remove(path, root, report, dry=dry)


def collect_retired(cfg, report, *, now, dry=False, discard_checkout=False):
    from . import work_recovery as recovery

    anchor = cfg.checkout.parent.parent if legacy_project() else cfg.state
    if discard_checkout:
        evict_tree(cfg, cfg.checkout, anchor, report, dry=dry, discard=True)
    # Retained roots have exactly one recursive collector: bounded recovery housekeeping.
    # Registration is constant metadata work and never inspects history on this timer path.
    parent = cfg.checkout.parent / recovery.RETAINED_DIR
    if not parent.is_dir():
        return
    if dry:
        note(report, "protected", f"{parent}: bounded recovery housekeeping owns retained trees")
        return
    from .constants import TAUCETI
    from .recovery_maintenance import register

    register(cfg.state, parent, "TauCeti", TAUCETI)
    note(report, "protected", f"{parent}: queued for bounded recovery housekeeping")


def collect_review_scratch(cfg, report, *, dry=False):
    """Only worker-owned review inputs; review results and outboxes live elsewhere."""
    from . import work_recovery as recovery

    parent = cfg.state / "review-work"
    for path in children(parent, report):
        if not path.name.startswith("review-") or path.is_symlink() or not path.is_dir():
            continue
        try:
            with locked(path / "live.lock", dry=dry):
                owner = read_json(path / "owner.json")
                if not (owner.get("finished") or recovery.owner_stopped(owner.get("process"))) or recovery.live_users(
                    path
                ):
                    note(report, "protected", str(path))
                    continue
                publication = read_json(path / "publication.json")
                if (
                    (path / "publication.json").exists() and publication.get("state") not in {"published", "superseded"}
                ) or (
                    not publication
                    and any(
                        (path / name).is_file() and read_json(path / name).get("review_changed") is not False
                        for name in ("post_plan.json", "plan.json")
                    )
                ):
                    note(report, "protected", f"{path}: rendered review output awaits reconciliation")
                    continue
                mounts = mount_points()
                if mounts is None or any(p == path.resolve() or p.is_relative_to(path.resolve()) for p in mounts):
                    note(report, "protected", str(path))
                    continue
                if not dry:
                    discarded = cfg.state / "recovery-cleanup/discarded"
                    discarded.mkdir(parents=True, exist_ok=True)
                    path.rename(discarded / path.name)
                    recovery._fsync_dir(discarded)
                    recovery._fsync_dir(parent)
                    request_cleanup()
                note(report, "removed", str(path))
        except (BlockingIOError, FileNotFoundError):
            continue
        except OSError as error:
            note(report, "errors", f"{path}: {error}")


@contextlib.contextmanager
def review_workspace(cfg):
    from .work_recovery import process_owner

    parent = cfg.state / "review-work"
    parent.mkdir(parents=True, exist_ok=True)
    collect_review_scratch(cfg, fresh_report())
    path = Path(tempfile.mkdtemp(prefix="review-", dir=parent))
    atomic_json(path / "owner.json", dict(process=process_owner()))
    try:
        with locked(path / "live.lock"):
            yield path
    finally:
        # The subprocess has exited. A fresh process observation still protects
        # any independently surviving reviewer that has not released its inputs.
        with contextlib.suppress(OSError):
            atomic_json(path / "owner.json", dict(finished=True))
            collect_review_scratch(cfg, fresh_report())


def recycle(cfg, *, now=None, dry=False):
    """At a round boundary, a generation expires even if short rounds kept using it."""
    now = time.time() if now is None else now
    marker = cfg.state / "cache-generation.json"
    row = read_json(marker)
    born = row.get("started_at")
    report = fresh_report(now)
    from . import work_recovery as recovery

    anchor = cfg.checkout.parent.parent if legacy_project() else cfg.state
    pending = [(cfg.checkout.with_name(cfg.checkout.name + ".retention-deleting"), anchor)]
    pending += [(path.with_name(path.name + ".retention-deleting"), root) for path, root in cache_roots(cfg)]
    pending += [
        (cfg.checkout / name, anchor)
        for name in recovery.CACHE_DIRS | recovery.CACHE_TOP
        if name.endswith(".retention-deleting")
    ]
    pending = [(path, root) for path, root in pending if path.exists() and safe(path, root)]
    valid = isinstance(born, (float, int)) and math.isfinite(born)
    from .activity import root as activity_root

    request = read_json(activity_root() / "cache-pressure.json")
    devices = {str(cfg.state.stat().st_dev)}
    with contextlib.suppress(OSError):
        devices.add(str(cfg.checkout.stat().st_dev))
    last_pressure = row.get("last_pressure_at", 0)
    if not isinstance(last_pressure, (float, int)) or not math.isfinite(last_pressure) or last_pressure > now:
        last_pressure = 0
    from .cache_storage import CRITICAL, LOW, headroom

    critical = False
    for location in (cfg.state, cfg.checkout):
        while not location.exists():
            location = location.parent
        usage = shutil.disk_usage(location)
        latched = read_json(activity_root() / f"critical-{location.stat().st_dev}.json").get("active", False)
        critical |= usage.free < headroom(usage.total, CRITICAL) or (
            latched and usage.free < headroom(usage.total, LOW)
        )
    # Emergency reclamation must not depend on collector availability or its records.
    # Ordinary pressure still targets one worker and preserves young build trees.
    forced = (critical and (not valid or now - last_pressure > 300)) or (
        (not request.get("devices") or bool(devices.intersection(request["devices"])))
        and ("workers" not in request or cfg.wid in request["workers"])
        and request.get("until", 0) > now
        and (not valid or request.get("requested_at", 0) > last_pressure)
    )
    if valid and not forced and now - born < LIFETIME and not pending:
        return report
    with contextlib.ExitStack() as stack:
        # Independent verification continuations may outlive the inference round. Their existing
        # job locks protect private caches; holding round.lock prevents a new continuation launching.
        for state in (cfg.state, cfg.state / "verification-state"):
            for path in (state / "verification-jobs").glob("*.lock"):
                try:
                    stack.enter_context(locked(path, dry=dry))
                except BlockingIOError:
                    note(report, "protected", str(path))
                    return report
                except OSError as error:
                    note(report, "errors", str(error))
                    return report
        # These paths were renamed only after being eligible for disposal. Retry them under
        # the same job locks, without removing younger replacement workspaces.
        for path, root in pending:
            if referenced_attempt(cfg, path):
                note(report, "protected", str(path))
            else:
                remove(path, root, report, dry=dry)
        if not valid and not forced:
            if not dry:
                try:
                    atomic_json(marker, dict(started_at=now))
                except OSError as error:
                    note(report, "errors", str(error))
            return report
        if not forced and now - born < LIFETIME:
            return report
        cache_result = fresh_report(now)
        private_only = forced and not critical and (not valid or now - born < LIFETIME)
        if private_only:
            # Drop old private artifact-store references, retaining the warm build tree.
            for path, anchor in cache_roots(cfg):
                remove(path, anchor, cache_result, dry=dry)
        else:
            evict_workspace(cfg, cache_result, dry=dry)
        merge_report(report, cache_result)
    # A blocked candidate must not keep every other cache in an expired generation. Renamed
    # deletions retry independently; paths still in use can wait for the next generation boundary.
    if not dry:
        try:
            atomic_json(
                marker,
                dict(
                    started_at=born if valid and private_only else now,
                    last_pressure_at=now if forced else row.get("last_pressure_at", 0),
                ),
            )
        except OSError as error:
            note(report, "errors", str(error))
    return report


def worker_configs():
    """Discover removed workers too; resolve only the roots, not their children."""
    base = Config.resolve()
    root = worker_state_root().resolve()
    checkout_root = (legacy_data_root(HERE) / "checkouts").resolve() if legacy_project() else root
    ids = (
        {
            p.name
            for p in root.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and any((p / name).exists() for name in ("home", "checkout", "round.lock", "instance.lock"))
        }
        if root.exists()
        else set()
    )
    if legacy_project() and checkout_root.exists():
        ids.update(
            p.name for p in checkout_root.iterdir() if p.is_dir() and not p.is_symlink() and (p / "TauCeti").is_dir()
        )
    for wid in sorted(ids):
        state = root / wid
        checkout = checkout_root / wid / "TauCeti" if legacy_project() else state / "checkout"
        yield dataclasses.replace(
            base,
            wid=wid,
            state=state,
            checkout=checkout,
            data_home=state / "home",
            logdir=(legacy_data_root(HERE) / "logs").resolve() / wid if legacy_project() else state / "logs",
        )


def referenced_attempt(cfg, attempt):
    for state in (cfg.state, cfg.state / "verification-state"):
        for path in (state / "verification-pending").glob("*.json"):
            job = read_json(path)
            for key in ("lease", "receipt", "lint_modules", "root"):
                value = job.get(key)
                if isinstance(value, str):
                    reference = Path(value)
                    if reference.is_relative_to("/opt/verification-state"):
                        reference = state / reference.relative_to("/opt/verification-state")
                    if not reference.is_absolute():
                        reference = state / reference
                    if reference.resolve().is_relative_to(attempt.resolve()):
                        return True
    return False


def past(deadline):
    return deadline is not None and time.monotonic() >= deadline


def remaining(deadline, cap):
    """A phase's time slice: at most ``cap`` seconds, and never past the run's deadline."""
    return cap if deadline is None else max(0.0, min(cap, deadline - time.monotonic()))


def sweep(*, dry=False, now=None, diagnostics_only=False, shared=True, shared_configs=None, deadline=None):
    """``deadline`` (time.monotonic()) bounds the whole sweep: each phase checks it before
    starting and the remaining phases are reported as deferred to the next run."""
    from . import activity, shared_mathlib

    now = time.time() if now is None else now
    report = fresh_report(now)

    def defer(phase):
        if not past(deadline):
            return False
        report.setdefault("deferred", []).append(phase)
        return True

    opened = open_files()
    mounts = mount_points()
    configs = list(worker_configs())
    if not diagnostics_only:
        from . import recovery_maintenance

        # Already-retired data needs no source archival. Use the existing collector
        # and deletion cursors instead of the activity preparer's tiny foreground slice.
        if not dry and not defer("retained-cleanup"):
            try:
                # Retained trees need the existing background collector too;
                # activity preparation only inventories and requests this work.
                report["retained_cleanup"] = recovery_maintenance.prepare(
                    worker_state_root().resolve(),
                    seconds=remaining(deadline, 30),
                    entries=262144,
                    bytes=1024**3,
                    removal_bytes=32 * 1024**3,
                )
            except Exception as error:
                note(report, "errors", f"retained cache cleanup: {error}")
        if not dry and not defer("recovery-trash"):
            try:
                trash = recovery_maintenance.collect_discarded(
                    worker_state_root().resolve(), seconds=remaining(deadline, 30)
                )
                report["recovery_trash"] = trash
                for error in trash.get("errors", []):
                    note(report, "errors", "recovery trash: " + error)
            except Exception as error:
                note(report, "errors", f"recovery trash cleanup: {error}")
        if shared and not defer("shared-caches"):
            from .cache_storage import collect

            try:
                storage = collect(
                    configs if shared_configs is None else shared_configs, dry=dry, now=now, opened=opened
                )
                merge_report(report, storage)
                report.update(
                    {
                        key: value
                        for key, value in storage.items()
                        if key
                        not in (
                            "at",
                            "removed",
                            "protected",
                            "errors",
                            "removed_count",
                            "protected_count",
                            "errors_count",
                        )
                    }
                )
            except (OSError, ValueError) as error:
                note(report, "errors", f"shared cache cleanup: {error}")
        from . import verification_storage

        states = [state for cfg in configs for state in (cfg.state, cfg.state / "verification-state")]
        if not defer("verification-storage"):
            report["verification_storage"] = (
                read_json(verification_storage.root() / "status.json")
                if dry
                else verification_storage.housekeep(states)
            )

    for cfg in configs:
        if defer(f"worker {cfg.wid}"):
            continue
        collect_review_scratch(cfg, report, dry=dry)
        try:
            if not safe(cfg.state, cfg.state.parent):
                continue
            if diagnostics_only or not workspace_idle(cfg, opened):
                raise BlockingIOError  # Live loops recycle their own caches at ordinary boundaries.
            generation = read_json(cfg.state / "cache-generation.json").get("started_at")
            round_lock = cfg.state / "round.lock"
            grace = generation if isinstance(generation, (int, float)) and math.isfinite(generation) else now
            idle_since = max(round_lock.stat().st_mtime, grace) if round_lock.exists() else grace
            with locked(round_lock, dry=dry):
                # A running loop owns instance.lock even between rounds. Retired or one-shot workers
                # have no such owner: after three idle days their saved checkout is a candidate.
                discard = isinstance(idle_since, (int, float)) and now - idle_since >= LIFETIME
                result = recycle(cfg, now=now, dry=dry)
                merge_report(report, result)
                try:
                    with contextlib.ExitStack() as stack:
                        for state in (cfg.state, cfg.state / "verification-state"):
                            for lease in (state / "verification-jobs").glob("*.lock"):
                                stack.enter_context(locked(lease, dry=dry))
                        collect_retired(cfg, report, now=now, dry=dry, discard_checkout=discard)
                except BlockingIOError:
                    note(report, "protected", str(cfg.state / "verification-jobs"))
        except BlockingIOError:
            if not diagnostics_only:
                note(report, "protected", str(cfg.state))
        except (OSError, ValueError, TypeError) as error:
            note(report, "errors", f"{cfg.wid}: {error}")
        expire_transcripts(cfg, report, now=now, dry=dry, opened=opened, mounts=mounts)
        # Diagnostic paths have their own independent references and leases. One disappearing
        # telemetry file or inaccessible directory must not skip other roots or workers.
        for root in (cfg.logdir, cfg.state / "verification", cfg.state / "verification-state" / "verification"):
            try:
                if not root.is_dir() or root.is_symlink():
                    continue
                if root != cfg.logdir and not safe(root, cfg.state):
                    continue
                if opened is None:
                    note(report, "protected", str(root))
                    continue
            except OSError as error:
                note(report, "errors", f"{root}: {error}")
                continue
            for path in children(root, report):
                try:
                    if path.is_symlink() or now - path.stat().st_mtime < LIFETIME:
                        continue
                    if diagnostics_only and not path.is_file():
                        continue
                    if root == cfg.logdir and (not path.is_file() or path.suffix != ".log"):
                        continue
                    if root != cfg.logdir and referenced_attempt(cfg, path):
                        note(report, "protected", str(path))
                        continue
                    if path.is_dir() and (path / "live.lock").exists():
                        try:
                            with locked(path / "live.lock", dry=dry):
                                remove(path, root, report, dry=dry)
                        except BlockingIOError:
                            note(report, "protected", str(path))
                    else:
                        remove(path, root, report, dry=dry, opened=opened if path.is_file() else None, mounts=mounts)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError) as error:
                    note(report, "errors", f"{path}: {error}")
    try:
        for key in (
            ()
            if not shared or diagnostics_only or not (shared_mathlib.root() / "entries").is_dir()
            else shared_mathlib.collect(shared_mathlib.root(), max_age=LIFETIME, now=now, dry_run=dry)
        ):
            note(report, "removed", str(shared_mathlib.root() / "entries" / key))
    except OSError as error:
        note(report, "errors", str(error))
    if shared and not diagnostics_only:
        from .shared_repositories import review_mathlib_cache

        try:
            from runner.source_cache import collect as collect_review_sources

            sources = collect_review_sources(review_mathlib_cache(), now=now, dry=dry)
            for category, values in sources.items():
                for value in values:
                    note(report, category, value)
        except (ImportError, OSError, ValueError) as error:
            note(report, "errors", f"review source cleanup: {error}")
    if shared:
        from .worker_manager import workers_state_dir

        log_root = workers_state_dir().resolve() / "logs"
        if opened is None:
            note(report, "protected", str(log_root))
        else:
            for directory in children(log_root, report):
                try:
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                except OSError as error:
                    note(report, "errors", f"{directory}: {error}")
                    continue
                for path in children(directory, report):
                    try:
                        if not path.name.startswith("work-") or path.suffix != ".log":
                            continue
                        if not path.is_symlink() and path.is_file() and now - path.stat().st_mtime >= LIFETIME:
                            remove(path, log_root, report, dry=dry, opened=opened, mounts=mounts)
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        note(report, "errors", f"{path}: {error}")
    if not dry:
        base = worker_state_root()
        while not base.exists():
            base = base.parent
        report["free_bytes"] = shutil.disk_usage(base).free
        atomic_json(activity.root() / "cleanup.json", report)
        activity.emit("storage_cleanup", **report)
    return report


FALLBACK_INTERVAL = 3600  # the installed timer's cadence
TIMER_SECONDS = 2400  # an installed timer run's deletion budget, inside its hour
TIMER_TIMEOUT = 3300  # systemd stops a run that has not finished by then
FALLBACK_RETRY = 300  # after a start that never reported back
FALLBACK_SECONDS = 900  # one fallback run's budget; durable cursors resume the rest next hour
FALLBACK_LOG_BYTES = 1024**2
# The installed timer's environment: project selection and shared roots, nothing worker-specific.
COLLECTOR_ENV = frozenset(
    {
        "PYTHONPATH",
        "TAUCETI_PROJECT_STATE",
        "TAUCETI_LEGACY_DATA_ROOT",
        "TAUCETI_REPO",
        "TAUCETI_ACTIVITY_DIR",
        "TAUCETI_SHARED_MATHLIB_ROOT",
        "TAUCETI_REVIEW_MATHLIB_CACHE",
        "TAUCETI_SHARED_REPOSITORIES_DIR",
        "TAUCETI_GITHUB_SHARED_DIR",
        "TAUCETI_WORKERS_STATE_DIR",
        "XDG_STATE_HOME",
        "PATH",
        "TAUCETI_BUILD_CACHE_ROOT",
        "TAUCETI_CACHE_MAX_GIB",
    }
)
# Beyond the timer's set: what a user session supplies to a systemd unit implicitly.
_SESSION_ENV = ("USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")


def _covered_root():
    return str(legacy_data_root().resolve())


def _stamp(value):
    """A recorded time, or 0 when missing, malformed, non-finite or in the future."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return value if 0 <= value <= time.time() + 60 else 0


def request_cleanup(*, fallback=False):
    """Ask the collector to run asynchronously, without a disk scan.

    Most installations have no collector service (macOS, Docker, one-off rounds, Linux without
    `workers service install`). With ``fallback`` the caller accepts starting the collector as a
    detached low-priority process instead, at most once per FALLBACK_INTERVAL for its data root."""
    from .activity import root

    directory = root()
    try:
        service = read_json(directory / "cleanup-service.json")
        if not service.get("installed"):
            if fallback:
                _fallback_collector(directory)
            return  # Do not invoke a global unit for an uninstalled/isolated project.
        if fallback and service.get("root") != _covered_root():
            # The unit runs for whichever installation installed it; also cover this one.
            _fallback_collector(directory)
        with locked(directory / "cleanup-request.lock"):
            marker = directory / "cleanup-request.json"
            previous = read_json(marker)
            now = time.time()
            start = now - previous.get("started_at", previous.get("at", 0)) >= 300
            atomic_json(
                marker, dict(at=now, started_at=now if start else previous.get("started_at", previous.get("at", 0)))
            )
            if not start:
                return
            subprocess.run(
                ["systemctl", "--user", "start", "--no-block", "tauceti-cleanup.service"],
                capture_output=True,
                timeout=3,
                check=False,
            )
    except Exception:
        pass  # Best effort: cleanup scheduling never affects ordinary worker progress.


def _fallback_marker(directory):
    import hashlib

    key = hashlib.sha256(_covered_root().encode()).hexdigest()[:16]
    return directory / f"cleanup-fallback-{key}.json"


def _collector_env(directory):
    """Reproduce the timer's context rather than the calling worker's isolated one."""
    import pwd

    source = self_env()
    env = {key: value for key, value in source.items() if key in COLLECTOR_ENV or key in _SESSION_ENV}
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir  # the worker may run with an isolated HOME
    env["TAUCETI_ACTIVITY_DIR"] = str(directory)
    env["TAUCETI_LEGACY_DATA_ROOT"] = _covered_root()
    return env


def _fallback_collector(directory):
    """Start `cleanup --all-data-roots` like the timer would: hourly, niced, idle I/O, bounded.

    cleanup.lock still serialises collectors, so a start racing a running one exits at once.
    A start counts only once the collector reports back; until then it is retried after
    FALLBACK_RETRY, so one failed launch cannot suppress cleanup for an hour."""
    if os.environ.get("TAUCETI_BACKGROUND_CLEANUP", "").lower() == "off":
        return
    marker = _fallback_marker(directory)
    with locked(directory / "cleanup-request.lock"):
        now = time.time()
        row = read_json(marker)
        completed, attempted = _stamp(row.get("completed_at")), _stamp(row.get("attempted_at"))
        if now - completed < FALLBACK_INTERVAL or now - attempted < FALLBACK_RETRY:
            return
        atomic_json(marker, {**row, "attempted_at": now})
    from .paths import self_argv

    argv = self_argv("cleanup", "--all-data-roots", "--max-seconds", FALLBACK_SECONDS, "--fallback-marker", marker)
    if sys.platform == "linux" and shutil.which("ionice"):
        argv = ["ionice", "-c3", *argv]
    if shutil.which("nice"):
        argv = ["nice", "-n", "10", *argv]
    log_path = directory / "cleanup-fallback.log"
    with contextlib.suppress(OSError):
        if log_path.stat().st_size > FALLBACK_LOG_BYTES:
            log_path.replace(log_path.with_suffix(".log.1"))
    with log_path.open("ab") as output:
        child = subprocess.Popen(
            argv,
            env=_collector_env(directory),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            close_fds=True,
        )
    # Reap the child whenever it exits, even in a long-lived `work --loop` or manager.
    threading.Thread(target=child.wait, name="cleanup-fallback-reaper", daemon=True).start()


def report_fallback(marker, report):
    """Called by the collector a fallback started, once its run finished."""
    if marker is None:
        return
    row = read_json(marker)
    errors = len(report.get("errors", [])) if isinstance(report, dict) else 0
    atomic_json(marker, {**row, "completed_at": time.time(), "errors": errors})


def pressure_cleanup():
    """Preparation invokes this only after a concrete ENOSPC; never touches its active workspace."""
    try:
        run(diagnostics_only=True)
    except (OSError, Die) as error:
        log(f"retention: disk-pressure cleanup deferred: {error}")


def free_bytes(path):
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def pending_work(base):
    """Prepared counts and immediate queue entries, never materialized-tree walks."""
    from .recovery_maintenance import cached_summary

    result = dict(recovery_backlog=cached_summary(base)["cleanup_backlog"], discarded_trees=0, verification_jobs=0)
    for state in base.iterdir() if base.is_dir() else ():
        if state.is_symlink() or not state.is_dir():
            continue
        for member in (state, state / "verification-state"):
            result["discarded_trees"] += sum(
                path.is_dir() and not path.is_symlink() for path in (member / "recovery-cleanup/discarded").glob("*")
            )
            result["verification_jobs"] += sum(1 for _ in (member / "verification-pending").glob("*.json"))
    return result


# One collector slice of recovery housekeeping. Activity refresh reuses it on hosts
# without an installed collector, where it is the only path that deletes anything.
COLLECTOR_SLICE = dict(seconds=30, entries=262144, bytes=1024**3, removal_bytes=32 * 1024**3)


def drain_pending(reports, *, once=False, deadline=None):
    """Round-robin existing deletion slices without repeating expensive inventories.

    ``deadline`` (time.monotonic()) ends the run after the current slice; cursors resume later."""
    from . import recovery_maintenance, verification_storage
    from .activity import root

    previous = os.environ.get("TAUCETI_LEGACY_DATA_ROOT")
    requested_at = time.time()
    pending = list(reports)
    extra_requested = False
    try:
        while pending:
            again = []
            for path, report in pending:
                if deadline is not None and time.monotonic() >= deadline:
                    again = []
                    break
                os.environ["TAUCETI_LEGACY_DATA_ROOT"] = str(path)
                totals = report.setdefault("drain", dict(slices=0, removed_bytes=0, removed_entries=0))
                try:
                    states = [
                        member for cfg in worker_configs() for member in (cfg.state, cfg.state / "verification-state")
                    ]
                    recovery = recovery_maintenance.prepare(worker_state_root().resolve(), **COLLECTOR_SLICE)
                    trash = recovery_maintenance.collect_discarded(worker_state_root().resolve())
                    verification = verification_storage.housekeep(
                        states,
                        seconds=1,
                        entries=8192,
                        removal_bytes=1024**3,
                        now=time.time(),
                    )
                    totals["slices"] += 1
                    progress = 0
                    for result in (recovery, trash, verification.get("work", {})):
                        for key in ("removed_bytes", "removed_entries"):
                            amount = result.get(key, 0)
                            totals[key] += amount
                            progress += amount
                    for error in trash.get("errors", []) + verification.get("errors", []):
                        note(report, "errors", str(error))
                    scanning = not recovery.get("sweep_complete", True) and recovery.get("metadata_entries", 0) > 0
                    if progress or scanning:
                        again.append((path, report))
                    report["free_bytes"] = shutil.disk_usage(worker_state_root()).free
                except Exception as error:
                    note(report, "errors", f"{path}: {error}")
            if once or (deadline is not None and time.monotonic() >= deadline):
                break
            requested = read_json(root() / "cleanup-request.json").get("at", 0)
            if requested > requested_at and (again or not extra_requested):
                requested_at = requested
                extra_requested = not bool(again)
                pending = list(reports)
            else:
                pending = again
    finally:
        if previous is None:
            os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
        else:
            os.environ["TAUCETI_LEGACY_DATA_ROOT"] = previous


def root_lock(state_root, *, dry=False):
    """cleanup.lock is per activity directory. Installations sharing a data root through different
    activity directories must still never collect it concurrently, so each collected worker-state
    root carries its own lock too; run_all() takes them in sorted order, so none can deadlock."""
    return locked(Path(state_root).resolve() / ".cleanup.lock", dry=dry)


def run(*, dry=False, diagnostics_only=False, deadline=None):
    from .activity import pin_root, root

    pin_root()
    with locked(root() / "cleanup.lock", dry=dry), root_lock(worker_state_root(), dry=dry):
        before = free_bytes(worker_state_root())
        report = sweep(dry=dry, diagnostics_only=diagnostics_only, deadline=deadline)
        if not diagnostics_only:
            retire_roots(report, dry=dry, deadline=deadline)
        report["free_before_bytes"] = before
        if not dry and not diagnostics_only:
            drain_pending([(legacy_data_root(HERE), report)], deadline=deadline)
            report["pending_work"] = pending_work(worker_state_root())
            report["free_bytes"] = free_bytes(worker_state_root())
            atomic_json(root() / "cleanup.json", report)
        return report


def install_timer():
    from .activity import pin_root
    from .paths import self_argv
    from .worker_manager import _service_path, _systemd_quote

    if sys.platform != "linux":
        raise Die("the hourly cleanup timer requires Linux with systemd user services")
    pin_root()
    directory = _service_path().parent
    directory.mkdir(parents=True, exist_ok=True)

    def quote(value):
        return _systemd_quote(str(value))

    legacy_data_root()  # resolves and exports this installation's data root for the unit
    environment = self_env()
    extra = "".join(
        "Environment=" + quote(key + "=" + value) + "\n" for key, value in environment.items() if key in COLLECTOR_ENV
    )
    (directory / "tauceti-cleanup.service").write_text(
        "[Unit]\nDescription=Reclaim retired TauCeti worker data\n[Service]\nType=oneshot\nNice=10\n"
        # Bounded runs: one stalled in kernel I/O must not stop the timer from ever firing again.
        f"IOSchedulingClass=idle\nTimeoutStartSec={TIMER_TIMEOUT}\nExecStart="
        + " ".join(map(quote, self_argv("cleanup", "--all-data-roots", "--max-seconds", TIMER_SECONDS)))
        + "\n"
        + extra
    )
    (directory / "tauceti-cleanup.timer").write_text(
        "[Unit]\nDescription=Hourly TauCeti cleanup\n[Timer]\nOnCalendar=hourly\nPersistent=true\n"
        "RandomizedDelaySec=5m\n[Install]\nWantedBy=timers.target\n"
    )
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tauceti-cleanup.timer"],
    ):
        try:
            subprocess.run(argv, check=True, capture_output=True, timeout=15)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode(errors="replace").strip()
            raise Die(f"cleanup timer installation failed: {detail or error}") from None
    from .activity import root

    # Record which installation the unit collects for; others keep their fallback.
    atomic_json(root() / "cleanup-service.json", dict(installed=True, root=_covered_root()))


def data_roots():
    """Current selection plus already-owned Fleet roots; upgrades may add aliases."""
    from .paths import legacy_data_root

    selected = legacy_data_root(HERE).resolve()
    roots = {selected}
    if not legacy_project():
        return [selected]
    fleet = Config.resolve().home / ".local/share/tauceti-fleet"
    registry = read_json(fleet / "worker-data/roots.json").get("roots", {})
    candidates = list(registry.values()) if isinstance(registry, dict) else []
    candidates += list((fleet / "worker-runtimes").glob("*/lib/python*/site-packages/tauceti_worker"))
    for value in candidates:
        if not isinstance(value, (str, Path)):
            continue
        path = Path(value).resolve()
        if (
            path.is_relative_to(fleet.resolve())
            and path.is_dir()
            and any((path / name).is_dir() for name in ("state", "checkouts"))
        ):
            roots.add(path)
    return [selected, *sorted(roots - {selected})]


RETIRED_PARTS = ("state", "checkouts", "logs")
RETIRED_TRASH = ".tauceti-retired-"
# Processes that refuse inspection yet cannot use worker data. Anything else that refuses blocks
# retirement and purge: unreadable never means unused.
_SESSION_HELPERS = frozenset({"(sd-pam)", "sd-pam", "sshd-session", "systemd"})
_SYSTEM_AGENTS = ("/usr/libexec/", "/System/", "/usr/bin/login")


_PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X", "-Q", "--check-hash-based-pycs"})


def invocation(argv):
    """What a command line runs: (argv[0], the Python module or script it runs, or None).

    Interpreter options are skipped, so `python3 -u -m tauceti_worker` and `python3 -I script`
    identify the module or script; arguments after it are data, not the code being executed."""
    if not argv:
        return None, None
    program = argv[0]
    if not os.path.basename(program).startswith("python"):
        return program, None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "-m":
            return program, ("-m", argv[index + 1]) if index + 1 < len(argv) else None
        if argument.startswith("-m") and len(argument) > 2:
            return program, ("-m", argument[2:])
        if argument in ("-c", "-"):
            return program, None
        if argument in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return program, ("script", argument)
    return program, None


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def retired_roots():
    """Candidate roots no configuration of this process selects: this installed package's own
    directory (the pre-user_data_root location) and, on Fleet hosts, other runtimes' roots.
    retire_roots() still requires positive evidence that nothing else selects or uses them."""
    from .paths import INSTALLED

    selected = legacy_data_root().resolve()
    candidates = set(data_roots()[1:]) if legacy_project() else set()
    if INSTALLED:
        candidates.add(HERE.resolve())
    return sorted(
        root
        for root in candidates
        if not _same_file(root, selected)
        and not selected.is_relative_to(root)
        and not root.is_relative_to(selected)
        and (
            any((root / part).is_dir() and not (root / part).is_symlink() for part in RETIRED_PARTS)
            or any(root.glob(RETIRED_TRASH + "*"))
        )
    )


def _linux_process(process, uid):
    """(paths, selected root, commands) of one /proc entry; None to skip it; raises if unknown."""
    try:
        if process.stat().st_uid != uid:
            return None
    except FileNotFoundError:
        return None

    def gone():
        # An exiting process drops its working directory and executable links before it shows
        # as a zombie; look again after a moment before calling it unreadable.
        for attempt in range(3):
            try:
                state = (process / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except (OSError, IndexError):
                return True
            if state in ("Z", "X"):
                return True  # a zombie has no files, working directory or future
            if attempt < 2:
                time.sleep(0.05)
        return False

    paths, commands, selects, denied = [], [], None, False
    readers = (
        ("cwd", lambda: paths.append(os.readlink(process / "cwd"))),
        ("exe", lambda: commands.append(os.readlink(process / "exe"))),
        (
            "cmdline",
            lambda: commands.extend(
                part.decode(errors="replace") for part in (process / "cmdline").read_bytes().split(b"\0") if part
            ),
        ),
    )
    for _name, read in readers:
        try:
            read()
        except FileNotFoundError:
            if gone():
                return None
            denied = True
        except OSError:
            denied = True
    try:
        for fd in (process / "fd").iterdir():
            # A descriptor can close between listing and reading it, including the one this listing
            # itself uses; that never means the whole process is gone.
            with contextlib.suppress(FileNotFoundError):
                paths.append(os.readlink(fd))
    except FileNotFoundError:
        if gone():
            return None
        denied = True
    except OSError:
        denied = True
    try:
        for value in (process / "environ").read_bytes().split(b"\0"):
            if value.startswith(b"TAUCETI_LEGACY_DATA_ROOT="):
                selects = value.split(b"=", 1)[1].decode(errors="replace")
    except FileNotFoundError:
        if gone():
            return None
        denied = True
    except OSError:
        denied = True
    if denied:
        try:
            helper = (process / "comm").read_text().strip() in _SESSION_HELPERS
        except OSError:
            helper = False
        if not (helper and not paths):
            raise PermissionError(f"cannot inspect {process}")
        return None
    return paths, selects, commands, int(process.name)


def _processes():
    """This user's processes as (paths, selected root, commands, pid), or None if any is unknown.

    Paths are working directories and open files; commands are the executable and the full
    command line. A process that exits mid-scan, or a zombie, is skipped. One that refuses
    inspection makes the observation unavailable, unless it is a known session helper or
    operating-system agent that cannot use worker data."""
    uid = os.getuid()
    observed = []
    if Path("/proc/self/fd").is_dir():
        try:
            for process in Path("/proc").glob("[0-9]*"):
                if (entry := _linux_process(process, uid)) is not None:
                    observed.append(entry)
        except OSError:
            return None
        return observed
    if sys.platform != "darwin":
        return None
    try:
        import psutil
    except ImportError:
        return None
    for process in psutil.process_iter():
        try:
            entry = _mac_process(psutil, process, uid)
        except (psutil.Error, OSError):
            return None
        if entry is not None:
            observed.append(entry)
    return observed


def _mac_process(psutil, process, uid):
    """(paths, selected root, commands) of one process; None to skip it; raises if unknown."""
    try:
        if process.uids().real != uid:
            return None
    except psutil.NoSuchProcess:
        return None
    paths, commands, selects, denied = [], [], None, False

    def files():
        paths.extend(item.path for item in process.open_files())

    def directory():
        paths.append(process.cwd())

    def command():
        commands.extend([process.exe(), *process.cmdline()])

    def environment():
        nonlocal selects
        selects = process.environ().get("TAUCETI_LEGACY_DATA_ROOT")

    for read in (files, directory, command, environment):
        try:
            read()
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            denied = True
    if denied:
        try:
            agent = process.exe().startswith(_SYSTEM_AGENTS)
        except psutil.NoSuchProcess:
            return None
        except (psutil.Error, OSError):
            agent = False
        if agent and not paths:
            return None
        raise PermissionError(f"cannot inspect process {process.pid}")
    return paths, selects, [c for c in commands if c], process.pid


def _configured_texts():
    """Text of everything that can select a data root without a process running right now:
    systemd user units and drop-ins with their EnvironmentFile=s, LaunchAgents, and the worker
    configuration files, including any `--config` a unit names. None if any of it exists but
    cannot be read, or names a file through a systemd specifier other than %h or %%."""
    import shlex

    from .paths import login_home
    from .worker_manager import default_workers_config

    home = login_home()
    directories = [home / ".config/systemd/user", home / "Library/LaunchAgents"]
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        directories.append(Path(xdg) / "systemd/user")
    files = []
    for directory in directories:
        for base, _dirs, names in os.walk(directory):
            files += [Path(base) / n for n in names if n.endswith((".service", ".timer", ".conf", ".plist"))]
    with contextlib.suppress(Exception):
        files.append(default_workers_config())
    if configured := os.environ.get("TAUCETI_WORKERS_CONFIG"):
        files.append(Path(configured).expanduser())

    def unescape(token):
        """systemd C-style escapes, as _systemd_unit's json.dumps() writes non-ASCII paths."""

        def decode(match):
            code = match[1]
            if code[0] in "uUx":
                return chr(int(code[1:], 16))
            return {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}[code]

        decoded = re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[nt\\\"'])", decode, token)
        if "\\" in decoded:
            raise ValueError(token)  # an escape we cannot interpret: unknown, never absent
        return decoded

    def split(value):
        # Keep backslashes for unescape(): posix shlex would drop them outside quotes.
        lexer = shlex.shlex(value.replace("\\", "\0"), posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""
        return [unescape(token.replace("\0", "\\")) for token in lexer]

    def referenced(value):
        value = value.replace("%h", str(home)).replace("%%", "\0")
        if "%" in value:
            raise ValueError(value)  # a specifier we cannot resolve: unknown, never absent
        return Path(value.replace("\0", "%")).expanduser()

    def configs(arguments):
        for index, argument in enumerate(arguments):
            if argument == "--config" and index + 1 < len(arguments):
                yield arguments[index + 1]
            elif argument.startswith("--config="):
                yield argument.split("=", 1)[1]

    texts, seen = [], set()
    while files:
        path = files.pop()
        if path in seen or not (path.exists() or path.is_symlink()):
            continue
        seen.add(path)
        try:
            raw = path.read_bytes()
            text = raw.decode()
        except (OSError, UnicodeDecodeError):
            return None
        texts.append(text)
        try:
            if path.suffix == ".plist":
                import plistlib

                agent = plistlib.loads(raw)
                files += [
                    Path(value).expanduser() for value in configs([str(a) for a in agent.get("ProgramArguments", [])])
                ]
                texts.append(" ".join(f"{k}={v}" for k, v in (agent.get("EnvironmentVariables") or {}).items()))
                continue
            for line in text.splitlines():
                key, _, value = line.strip().partition("=")
                if key == "EnvironmentFile":
                    files += [referenced(token.removeprefix("-")) for token in split(value)]
                elif key in ("ExecStart", "ExecStartPre", "ExecStartPost"):
                    files += [referenced(token) for token in configs(split(value.lstrip("@-:+!")))]
        except (ValueError, TypeError, AttributeError):
            return None
    return texts


def _venv(root):
    return next((parent for parent in root.parents if (parent / "pyvenv.cfg").is_file()), None)


def _retirement_blocker(root, processes, mounts, configured):
    """Why ``root`` must be kept for now, or None when it is provably retired."""
    if processes is None:
        return "process observation unavailable"
    if mounts is None:
        return "mount observation unavailable"
    if configured is None:
        return "service or worker configuration unreadable"
    target = canonical_candidate(root)
    if any(point == target or point.is_relative_to(target) for point in mounts):
        return "contains a mount"
    if any(str(root) in text for text in configured):
        return "selected by a service or worker configuration"
    parts = [os.path.join(str(root), part) for part in RETIRED_PARTS]
    runtime = _venv(root) if root != HERE.resolve() else None
    for paths, selects, commands, _pid in processes:
        if selects and (_same_file(selects, root) or Path(selects).resolve() == root):
            return "selected by a running process"
        if any(path == part or path.startswith(part + os.sep) for path in paths for part in parts):
            return "in use by a running process"
        # The executable, argv[0] and a Python script identify what runs; other arguments are
        # data (a file an editor has open there is covered by the open-file check above).
        program, code = invocation(commands[1:])
        running = [commands[0] if commands else "", program or ""]
        if code and code[0] == "script":
            running.append(code[1])
        if runtime and any(command.startswith(str(runtime) + os.sep) for command in running):
            return "its runtime is running"
    return None


@contextlib.contextmanager
def _idle_root(root):
    """Hold every worker slot and round lock of a root, or yield False if any is in use.

    Older runtimes keep these flocks for the life of a worker or round, so holding them all
    while the root is renamed away excludes every process that could still be writing there."""
    held = []
    try:
        for lock in sorted([*root.glob("state/*/instance.lock"), *root.glob("state/*/round.lock")]):
            if lock.is_symlink():
                yield False
                return
            fd = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            held.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True
    finally:
        for fd in held:
            os.close(fd)


def _drain(path, deadline=None):
    """Delete a staged tree bottom-up, stopping at ``deadline``; True once it is gone.

    Lake and Git leave read-only directories, which regain owner write permission first."""

    def writable(directory):
        with contextlib.suppress(OSError):
            os.chmod(directory, os.stat(directory, follow_symlinks=False).st_mode | 0o700)

    def unreadable(error):
        if error.filename:
            writable(error.filename)

    def expired():
        return deadline is not None and time.monotonic() >= deadline

    if expired():
        return False
    for base, dirs, files in os.walk(path, topdown=False, onerror=unreadable):
        if expired():
            return False
        for name in [*files, *(d for d in dirs if os.path.islink(os.path.join(base, d)))]:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            target = os.path.join(base, name)
            try:
                os.unlink(target)
            except PermissionError:
                writable(base)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(target)
            except FileNotFoundError:
                pass
        for name in dirs:
            if expired():
                return False
            directory = os.path.join(base, name)
            if not os.path.islink(directory):
                with contextlib.suppress(OSError):
                    os.rmdir(directory)
    if expired():
        return False
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False  # an unreadable corner; the next run retries it
    return True


def retire_roots(report, *, dry=False, deadline=None):
    """Delete worker data left by earlier installations and runtimes, once nothing uses it.

    Nothing migrates such data, so it is deleted outright: checkouts, caches, logs and
    unpublished recovery records alike. A root is staged (renamed aside, which is instant) only
    while every worker and round lock in it is held and no process, service unit or mount refers
    to it; its contents are then deleted in deadline-bounded passes that resume on later runs.
    An older runtime's worker starting a new worker id at the instant of staging is not fenced:
    such code predates this protocol."""
    roots = retired_roots()
    if not roots:
        return
    processes, mounts, configured = _processes(), mount_points(), _configured_texts()
    for root in roots:
        try:
            if any((root / part).is_dir() for part in RETIRED_PARTS):
                if reason := _retirement_blocker(root, processes, mounts, configured):
                    note(report, "protected", f"{root}: {reason}")
                    continue
                if dry:
                    for part in RETIRED_PARTS:
                        if (root / part).is_dir():
                            note(report, "removed", str(root / part))
                    continue
                with _idle_root(root) as idle:
                    if not idle:
                        note(report, "protected", f"{root}: a worker or round holds its lock")
                        continue
                    for part in RETIRED_PARTS:
                        path = root / part
                        if path.is_dir() and not path.is_symlink():
                            path.rename(root / f"{RETIRED_TRASH}{part}-{os.getpid()}-{time.time_ns()}")
            if dry:
                continue
            for trash in sorted(root.glob(RETIRED_TRASH + "*")):
                if trash.is_symlink() or not trash.is_dir():
                    continue
                target = canonical_candidate(trash)
                if mounts is None or any(point == target or point.is_relative_to(target) for point in mounts):
                    note(report, "protected", f"{trash}: contains a mount")
                    continue
                if _drain(trash, deadline):
                    note(report, "removed", str(trash))
                else:
                    note(report, "protected", f"{trash}: deletion continues next run")
        except OSError as error:
            note(report, "errors", f"{root}: {error}")


def run_all(*, dry=False, deadline=None):
    """One collector covers current and retired roots, with fair deletion slices."""
    from .activity import pin_root, root

    if not legacy_project():
        return run(dry=dry, deadline=deadline)
    pin_root()
    previous = os.environ.get("TAUCETI_LEGACY_DATA_ROOT")
    report = fresh_report()
    report["roots"] = []
    report["free_before_bytes"] = free_bytes(worker_state_root())
    with locked(root() / "cleanup.lock", dry=dry), contextlib.ExitStack() as roots:
        try:
            retire_roots(report, dry=dry, deadline=deadline)
            paths = data_roots()
            for path in sorted(paths):
                roots.enter_context(root_lock(path / "state", dry=dry))
            configs = []
            for path in paths:
                os.environ["TAUCETI_LEGACY_DATA_ROOT"] = str(path)
                configs.extend(worker_configs())
            initial = {path: dict(free_before_bytes=free_bytes(path)) for path in paths}
            if not dry:
                # Exact queued disposal must not wait behind shared-cache inventory.
                drain_pending(list(initial.items()), once=True, deadline=deadline)
            for index, path in enumerate(paths):
                os.environ["TAUCETI_LEGACY_DATA_ROOT"] = str(path)
                try:
                    result = sweep(dry=dry, shared=index == 0, shared_configs=configs, deadline=deadline)
                except Exception as error:
                    result = fresh_report()
                    note(result, "errors", str(error))
                result["free_before_bytes"] = initial[path]["free_before_bytes"]
                if "drain" in initial[path]:
                    result["drain"] = initial[path]["drain"]
                report["roots"].append(dict(path=str(path), **result))
            if not dry:
                drain_pending([(Path(result["path"]), result) for result in report["roots"]], deadline=deadline)
            for result in report["roots"]:
                base = Path(result["path"]) / "state"
                try:
                    result["pending_work"] = pending_work(base)
                    result["free_bytes"] = free_bytes(base)
                except (OSError, ValueError, TypeError) as error:
                    note(result, "errors", f"pending-work report: {error}")
                merge_report(report, result)
        finally:
            if previous is None:
                os.environ.pop("TAUCETI_LEGACY_DATA_ROOT", None)
            else:
                os.environ["TAUCETI_LEGACY_DATA_ROOT"] = previous
        report["free_bytes"] = free_bytes(worker_state_root())
        if not dry:
            atomic_json(root() / "cleanup.json", report)
    return report


def cmd_cleanup(args):
    if args.install_timer:
        install_timer()
        return 0
    seconds = getattr(args, "max_seconds", None)
    deadline = time.monotonic() + seconds if seconds else None
    try:
        report = (run_all if args.all_data_roots else run)(dry=args.dry_run, deadline=deadline)
    except BlockingIOError:
        raise Die("cleanup is already running") from None
    if not args.dry_run:
        report_fallback(getattr(args, "fallback_marker", None), report)
    print(json.dumps(report, indent=2))
    return int(bool(report["errors"]))

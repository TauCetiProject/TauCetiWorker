"""Three-day lifetimes for disposable worker data. No GitHub reads or cache service."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, Die, legacy_project, log, worker_state_root
from .paths import HERE, self_env
from .runtime_status import atomic_json, read_json

LIFETIME = 3 * 86400


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
        return None  # Missing observations never mean that diagnostics are unused.
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
    try:

        def unescape(value):
            for escaped, literal in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
                value = value.replace(escaped, literal)
            return value

        return [Path(unescape(line.split()[4])) for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    except OSError:
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
    mounted = (mounts is None and sys.platform == "linux") or any(
        point == path or point.is_relative_to(path) for point in mounts or ()
    )
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


def cache_roots(cfg):
    # Never traverse arbitrary caches in the login home or operator-supplied cache overrides.
    home, anchor = cfg.state / "home", cfg.state
    if sys.platform == "darwin" and legacy_project():
        from .agents import _worker_iso_home

        home = _worker_iso_home(cfg.wid, _base=cfg.home)
        if not safe(home, cfg.home):
            return []
        anchor = home
    return [(home / ".cache" / name, anchor) for name in ("lake", "mathlib")]


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
    applicable = (not request.get("devices") or bool(devices.intersection(request["devices"]))) and (
        "workers" not in request or cfg.wid in request["workers"]
    )
    last_pressure = row.get("last_pressure_at", 0)
    critical = False
    for location in (cfg.state, cfg.checkout):
        while not location.exists():
            location = location.parent
        usage = shutil.disk_usage(location)
        from .cache_storage import CRITICAL, headroom

        critical = critical or usage.free < headroom(usage.total, CRITICAL)
    forced = (
        applicable and request.get("until", 0) > now and (not valid or request.get("requested_at", 0) > last_pressure)
    ) or (critical and (not valid or now - last_pressure > 300))
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
        private_only = forced and not critical and valid and now - born < LIFETIME
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
                    started_at=born if private_only else now,
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
    checkout_root = (HERE / "checkouts").resolve() if legacy_project() else root
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
            logdir=(HERE / "logs").resolve() / wid if legacy_project() else state / "logs",
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


def sweep(*, dry=False, now=None, diagnostics_only=False):
    from . import activity, shared_mathlib

    now = time.time() if now is None else now
    report = fresh_report(now)
    opened = open_files()
    mounts = mount_points()
    configs = list(worker_configs())
    if not diagnostics_only:
        from .cache_storage import collect

        try:
            storage = collect(configs, dry=dry, now=now, opened=opened)
            merge_report(report, storage)
            report.update(
                {
                    key: value
                    for key, value in storage.items()
                    if key
                    not in ("at", "removed", "protected", "errors", "removed_count", "protected_count", "errors_count")
                }
            )
        except (OSError, ValueError) as error:
            note(report, "errors", f"shared cache cleanup: {error}")
        from . import verification_storage

        states = [state for cfg in configs for state in (cfg.state, cfg.state / "verification-state")]
        report["verification_storage"] = (
            read_json(verification_storage.root() / "status.json") if dry else verification_storage.housekeep(states)
        )

    for cfg in configs:
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
            if diagnostics_only or not (shared_mathlib.root() / "entries").is_dir()
            else shared_mathlib.collect(shared_mathlib.root(), max_age=LIFETIME, now=now, dry_run=dry)
        ):
            note(report, "removed", str(shared_mathlib.root() / "entries" / key))
    except OSError as error:
        note(report, "errors", str(error))
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


def request_cleanup():
    """Ask the installed project collector to run asynchronously, without a disk scan."""
    from .activity import root

    directory = root()
    try:
        if not read_json(directory / "cleanup-service.json").get("installed"):
            return  # Do not invoke a global unit for an uninstalled/isolated project.
        with locked(directory / "cleanup-request.lock"):
            marker = directory / "cleanup-request.json"
            if time.time() - read_json(marker).get("at", 0) < 300:
                return
            atomic_json(marker, dict(at=time.time()))
            subprocess.run(
                ["systemctl", "--user", "start", "--no-block", "tauceti-cleanup.service"],
                capture_output=True,
                timeout=3,
                check=False,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # The hourly collector remains independent of ordinary worker progress.


def pressure_cleanup():
    """Preparation invokes this only after a concrete ENOSPC; never touches its active workspace."""
    try:
        run(diagnostics_only=True)
    except (OSError, Die) as error:
        log(f"retention: disk-pressure cleanup deferred: {error}")


def run(*, dry=False, diagnostics_only=False):
    from .activity import pin_root, root

    pin_root()
    with locked(root() / "cleanup.lock", dry=dry):
        return sweep(dry=dry, diagnostics_only=diagnostics_only)


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

    environment = self_env()
    extra = "".join(
        "Environment=" + quote(key + "=" + value) + "\n"
        for key, value in environment.items()
        if key
        in {
            "PYTHONPATH",
            "TAUCETI_PROJECT_STATE",
            "TAUCETI_REPO",
            "TAUCETI_ACTIVITY_DIR",
            "TAUCETI_SHARED_MATHLIB_ROOT",
            "TAUCETI_WORKERS_STATE_DIR",
            "XDG_STATE_HOME",
            "PATH",
            "TAUCETI_BUILD_CACHE_ROOT",
            "TAUCETI_CACHE_MAX_GIB",
        }
    )
    (directory / "tauceti-cleanup.service").write_text(
        "[Unit]\nDescription=Reclaim retired TauCeti worker data\n[Service]\nType=oneshot\nNice=10\n"
        "IOSchedulingClass=idle\nExecStart=" + " ".join(map(quote, self_argv("cleanup"))) + "\n" + extra
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

    atomic_json(root() / "cleanup-service.json", dict(installed=True))


def cmd_cleanup(args):
    if args.install_timer:
        install_timer()
        return 0
    try:
        report = run(dry=args.dry_run)
    except BlockingIOError:
        raise Die("cleanup is already running") from None
    print(json.dumps(report, indent=2))
    return int(bool(report["errors"]))

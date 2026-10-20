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
RETIRED_BATCH = 3


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


def held_locks():
    """Read ordinary flock ownership without briefly taking a starting loop's instance lock."""
    try:
        held = set()
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if fields[1] != "FLOCK":
                continue
            major, minor, inode = fields[5].split(":")
            held.add((os.makedev(int(major, 16), int(minor, 16)), int(inode)))
        return held
    except (OSError, ValueError, IndexError):
        return None


def workspace_idle(cfg, held):
    if held is None:
        return False  # Skip this candidate; diagnostics and workers still proceed.
    try:
        info = (cfg.state / "instance.lock").stat()
    except FileNotFoundError:
        return True
    return (info.st_dev, info.st_ino) not in held


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
    candidates = []
    for tree in (cfg.checkout.parent / recovery.RETAINED_DIR).glob(f"{cfg.checkout.name}-*"):
        try:
            if not safe(tree, anchor) or not tree.is_dir():
                continue
            if tree.name.endswith((".retention-deleting", ".deleting")) or now - tree.stat().st_mtime >= LIFETIME:
                candidates.append(tree)
        except OSError as error:
            note(report, "errors", f"{tree}: {error}")
    # Limit expensive source preservation to three trees per daily pass. A local cursor ensures
    # an uncertain tree cannot starve the others, even when every attempt leaves it in place.
    cursor = cfg.state / "cleanup-retained.json"
    last = read_json(cursor).get("last", "")
    if not isinstance(last, str):
        last = ""
    candidates.sort(key=lambda tree: (tree.name <= last, tree.name))
    for tree in candidates[:RETIRED_BATCH]:
        if not dry:
            try:
                atomic_json(cursor, dict(last=tree.name))
            except OSError as error:
                note(report, "errors", f"{cursor}: {error}")
        try:
            name = tree.name.removesuffix(".retention-deleting").removesuffix(".deleting")
            sidecar = tree.with_name(name + ".owner.json")
            if tree.name.endswith((".retention-deleting", ".deleting")):
                if referenced_attempt(cfg, tree):
                    note(report, "protected", str(tree))
                    continue
                remove(tree, anchor, report, dry=dry)
            else:
                evict_tree(cfg, tree, anchor, report, dry=dry, discard=True, owner=read_json(sidecar))
            if not dry and not tree.exists():
                sidecar.unlink(missing_ok=True)
        except OSError as error:
            note(report, "errors", f"{tree}: {error}")


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
    if valid and now - born < LIFETIME and not pending:
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
        if not valid:
            if not dry:
                try:
                    atomic_json(marker, dict(started_at=now))
                except OSError as error:
                    note(report, "errors", str(error))
            return report
        if now - born < LIFETIME:
            return report
        cache_result = fresh_report(now)
        evict_workspace(cfg, cache_result, dry=dry)
        merge_report(report, cache_result)
    # A blocked candidate must not keep every other cache in an expired generation. Renamed
    # deletions retry independently; paths still in use can wait for the next generation boundary.
    if not dry:
        try:
            atomic_json(marker, dict(started_at=now))
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
    held = held_locks()
    configs = list(worker_configs())
    for cfg in configs:
        try:
            if not safe(cfg.state, cfg.state.parent):
                continue
            if diagnostics_only or not workspace_idle(cfg, held):
                raise BlockingIOError  # Live loops recycle their own caches at ordinary boundaries.
            generation = read_json(cfg.state / "cache-generation.json").get("started_at")
            round_lock = cfg.state / "round.lock"
            idle_since = round_lock.stat().st_mtime if round_lock.exists() else generation
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
            note(report, "protected", str(cfg.state))
        except (OSError, ValueError, TypeError) as error:
            note(report, "errors", f"{cfg.wid}: {error}")
        try:
            # Only known diagnostic artifacts; accounting, source, review outboxes and credentials
            # are deliberately outside these paths. A held verification lease protects its attempt.
            for root in (cfg.logdir, cfg.state / "verification", cfg.state / "verification-state" / "verification"):
                if not root.is_dir() or root.is_symlink():
                    continue
                if root != cfg.logdir and not safe(root, cfg.state):
                    continue
                for path in root.iterdir():
                    if path.is_symlink() or now - path.stat().st_mtime < LIFETIME:
                        continue
                    if diagnostics_only and not path.is_file():
                        continue  # ENOSPC retry only unlinks closed files; no recursive deletion or source archival.
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
        except (OSError, ValueError, TypeError) as error:
            note(report, "errors", f"{cfg.wid}: diagnostics: {error}")
    try:
        for key in (
            ()
            if diagnostics_only
            else shared_mathlib.collect(shared_mathlib.root(), max_age=LIFETIME, now=now, dry_run=dry)
        ):
            note(report, "removed", str(shared_mathlib.root() / "entries" / key))
    except OSError as error:
        note(report, "errors", str(error))
    from .worker_manager import workers_state_dir

    log_root = workers_state_dir().resolve() / "logs"
    for directory in log_root.iterdir() if log_root.is_dir() else ():
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.glob("work-*.log"):
            try:
                if not path.is_symlink() and now - path.stat().st_mtime >= LIFETIME:
                    remove(path, log_root, report, dry=dry, opened=opened, mounts=mounts)
            except OSError as error:
                note(report, "errors", str(error))
    if not dry:
        base = worker_state_root()
        while not base.exists():
            base = base.parent
        report["free_bytes"] = shutil.disk_usage(base).free
        atomic_json(activity.root() / "cleanup.json", report)
        activity.emit("storage_cleanup", **report)
    return report


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

    if sys.platform != "linux":
        raise Die("the daily cleanup timer requires Linux with systemd user services")
    pin_root()
    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)

    def quote(value):
        return json.dumps(str(value).replace("%", "%%"))

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
        }
    )
    (directory / "tauceti-cleanup.service").write_text(
        "[Unit]\nDescription=Reclaim retired TauCeti worker data\n[Service]\nType=oneshot\nNice=10\n"
        "IOSchedulingClass=idle\nExecStart=" + " ".join(map(quote, self_argv("cleanup"))) + "\n" + extra
    )
    (directory / "tauceti-cleanup.timer").write_text(
        "[Unit]\nDescription=Daily TauCeti cleanup\n[Timer]\nOnCalendar=daily\nPersistent=true\n"
        "RandomizedDelaySec=30m\n[Install]\nWantedBy=timers.target\n"
    )
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tauceti-cleanup.timer"],
    ):
        subprocess.run(argv, check=True, capture_output=True, timeout=15)


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

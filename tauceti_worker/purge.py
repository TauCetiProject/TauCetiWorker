"""`tauceti purge`: remove what TauCetiWorker stored for this user.

Run it before `uv tool uninstall tauceti` (or deleting a clone). Without --yes it only prints
the plan. It deletes only what is provably ours: data roots, and the per-user state, cache and
verification locations at their default paths. A location moved by an environment override is
listed for you to remove, because the override may name a directory that also holds other
things. ~/.elan is kept: other Lean projects share it, and `elan toolchain gc` reclaims it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Die
from .paths import HERE, legacy_data_root, login_home, user_data_root

VERIFY_TMP = Path("/tmp")  # verification.cache_root()'s default parent
UNITS = (
    "tauceti-workers.service",
    "tauceti-cleanup.timer",
    "tauceti-cleanup.service",
    "tauceti-activity.timer",
    "tauceti-activity.service",
)

# Stores under the per-user state or cache locations, and the variables that can move them.
OVERRIDES = (
    "TAUCETI_WORKERS_STATE_DIR",
    "TAUCETI_ACTIVITY_DIR",
    "TAUCETI_BUDGET_DIR",
    "TAUCETI_GITHUB_SHARED_DIR",
    "TAUCETI_SHARED_REPOSITORIES_DIR",
    "TAUCETI_BUILD_CACHE_ROOT",
    "TAUCETI_SHARED_MATHLIB_ROOT",
    "TAUCETI_VERIFY_STORAGE_DIR",
    "TAUCETI_VERIFY_CACHE_DIR",
    "TAUCETI_RUNTIME_DIR",
)


def _default_state_home() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "tauceti"
    if sys.platform == "darwin":
        return login_home() / "Library" / "Application Support" / "tauceti" / "state"
    return login_home() / ".local" / "state" / "tauceti"


def inventory(include_config=False):
    """(paths to delete, roots whose worker locks must be held, overridden locations to report)."""
    from .paths import INSTALLED
    from .retention import RETIRED_PARTS, RETIRED_TRASH, retired_roots
    from .worker_manager import config_home, workers_runtime_dir

    home = login_home()
    delete, roots, overridden = [], [], []
    stable = user_data_root()  # lexical: a symlink here is removed as a link, never followed
    delete.append(stable)
    roots.append(stable)
    selected = legacy_data_root()
    for root in [selected, *retired_roots()]:
        if Path(os.path.abspath(root)) == Path(os.path.abspath(stable)) or _same(root, stable):
            continue
        is_checkout = not INSTALLED and Path(root).resolve() == Path(HERE).resolve()
        if root == selected and not is_checkout and os.environ.get("TAUCETI_LEGACY_DATA_ROOT"):
            # An operator-chosen root: generic state/checkouts/logs names prove no ownership.
            overridden.append(f"TAUCETI_LEGACY_DATA_ROOT={os.environ['TAUCETI_LEGACY_DATA_ROOT']}")
            continue
        roots.append(root)
        # A source checkout or an earlier installation holds more than our data.
        delete += [root / part for part in RETIRED_PARTS]
        delete += list(root.glob(RETIRED_TRASH + "*"))
    delete += [_default_state_home(), home / ".cache" / "tauceti"]
    delete += [home / ".cache" / "tauceti-review", home / ".cache" / "tauceti-claims"]
    if not os.environ.get("TAUCETI_VERIFY_CACHE_DIR"):
        delete += sorted(VERIFY_TMP.glob(f"tauceti-verification-{os.getuid()}-*"))
    if sys.platform == "darwin":
        delete.append(home / ".tauceti")  # isolated worker homes (agents._worker_iso_home)
    if not os.environ.get("TAUCETI_RUNTIME_DIR"):
        with contextlib.suppress(Exception):
            delete.append(workers_runtime_dir())
    if not os.environ.get("TAUCETI_VERIFY_STORAGE_DIR"):
        # verification_storage.root()'s default, which ignores XDG_STATE_HOME and macOS's location.
        delete.append(home / ".local" / "state" / "tauceti" / "verification-storage")
    overridden += [f"{name}={os.environ[name]}" for name in OVERRIDES if os.environ.get(name)]
    if include_config:
        if os.environ.get("TAUCETI_CONFIG_HOME"):
            overridden.append(f"TAUCETI_CONFIG_HOME={os.environ['TAUCETI_CONFIG_HOME']}")
        else:
            delete.append(config_home())
        if os.environ.get("TAUCETI_WORKERS_CONFIG"):
            overridden.append(f"TAUCETI_WORKERS_CONFIG={os.environ['TAUCETI_WORKERS_CONFIG']}")
    return delete, sorted(roots), overridden


def _same(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _refusal(path):
    """Why ``path`` must not be deleted, or None."""
    from .retention import mount_points

    path = Path(os.path.abspath(path))
    home = login_home().resolve()
    if len(path.parts) < 3 or path in {Path(path.anchor), home} or home.is_relative_to(path):
        return "too broad"
    if os.path.realpath(path.parent) != str(path.parent):
        return "reached through a symbolic link"
    if path.is_symlink():
        return None  # removed as a link; its target is never touched
    if Path(HERE).resolve().is_relative_to(path.resolve()):
        return "contains the TauCetiWorker code"
    mounts = mount_points()
    if mounts is None:
        return "mounts cannot be observed"
    target = path.resolve()
    if any(point == target or point.is_relative_to(target) for point in mounts):
        return "contains a mount"
    return None


def plan(include_config=False):
    delete, roots, overridden = inventory(include_config)
    paths, refused = [], []
    for path in dict.fromkeys(Path(os.path.abspath(p)) for p in delete):
        if not (path.exists() or path.is_symlink()):
            continue
        if reason := _refusal(path):
            refused.append(f"{path}: {reason}")
            continue
        paths.append(path)
    nested = {p for p in paths for q in paths if p != q and p.is_relative_to(q)}
    return [p for p in paths if p not in nested], roots, overridden, refused


def _unit_directories():
    raw = os.environ.get("XDG_CONFIG_HOME")
    configured = (Path(raw) if raw else login_home() / ".config") / "systemd" / "user"
    return list(dict.fromkeys([configured, login_home() / ".config" / "systemd" / "user"]))


def stop_services():
    """Stop, disable and delete the worker service and timers; returns failures to report."""
    from .worker_manager import _service_path

    failures = []
    if sys.platform == "darwin":
        service = _service_path()
        if service.exists():
            result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{service.stem}"], capture_output=True)
            if result.returncode not in (0, 3, 36):  # 3 and 36: not loaded
                failures.append(f"launchctl bootout {service.stem}: {result.stderr.decode(errors='replace').strip()}")
            service.unlink(missing_ok=True)
        return failures
    present = [directory / unit for directory in _unit_directories() for unit in UNITS if (directory / unit).exists()]
    if not present:
        return failures
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return [f"systemctl is unavailable; stop and remove {', '.join(map(str, present))} yourself"]
    names = sorted({path.name for path in present})
    for action in ("stop", "disable"):
        result = subprocess.run([systemctl, "--user", action, *names], capture_output=True)
        if result.returncode:
            failures.append(f"systemctl --user {action}: {result.stderr.decode(errors='replace').strip()}")
    for path in present:
        path.unlink(missing_ok=True)
    subprocess.run([systemctl, "--user", "daemon-reload"], capture_output=True)
    for name in names:
        state = subprocess.run([systemctl, "--user", "is-active", name], capture_output=True, text=True)
        if state.stdout.strip() in ("active", "activating", "deactivating", "reloading"):
            failures.append(f"{name} is still {state.stdout.strip()}")
    return failures


def _runs_worker(argv):
    """Whether a command line invokes TauCetiWorker: `tauceti …`, `python [options] -m
    tauceti_worker …` or `python [options] …/tauceti …`. Arguments that merely mention it (an
    editor, a search, `vim -m tauceti_worker`) do not."""
    from .retention import invocation

    program, code = invocation(argv)
    if program is None:
        return False
    if os.path.basename(program) == "tauceti":
        return True
    if code is None:
        return False
    kind, target = code
    if kind == "-m":
        return target.split(".")[0] == "tauceti_worker"
    return os.path.basename(target) == "tauceti"


def _lineage():
    """This process and its ancestors."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            try:
                import psutil

                pids.update(parent.pid for parent in psutil.Process(pid).parents())
            except Exception:
                pass
            break
    return pids


def _busy(paths, roots):
    from .retention import _processes
    from .worker_manager import _live_manager_pointer, _manager_lock_held, default_workers_config

    if _manager_lock_held():
        return "the worker manager is running"
    with contextlib.suppress(Exception):
        if _live_manager_pointer(default_workers_config()):
            return "the worker manager is running"
    processes = _processes()
    if processes is None:
        return "running processes cannot be inspected"
    ours = _lineage()
    for found, selects, commands, pid in processes:
        if pid in ours:
            continue  # this purge and whatever launched it (a shell, `uv run`), which inherit its selection
        if _runs_worker(commands[1:]):
            # A manager with its own --config and directories holds nothing purge can find.
            return f"a TauCetiWorker process is running (pid {pid})"
        if selects and any(_same(selects, root) or Path(selects) == Path(root) for root in roots):
            return f"a running process selects {selects}"
        for path in paths:
            prefix = str(path)
            if any(item == prefix or item.startswith(prefix + os.sep) for item in found):
                return f"a running process uses {path}"
    return None


def cmd_purge(args):
    paths, roots, overridden, refused = plan(include_config=args.config)
    if not args.yes:
        print("tauceti purge would remove:" if paths else "tauceti purge: nothing to remove")
        for path in paths:
            print(f"  {path}")
        print("and stop and remove the tauceti-workers service and the cleanup and activity timers.")
        for item in refused:
            print(f"kept (refused): {item}")
        for item in overridden:
            print(f"not removed, set by an override (remove it yourself if only TauCetiWorker uses it): {item}")
        if paths:
            print("Re-run with --yes to delete. ~/.elan is kept; `elan toolchain gc` reclaims toolchains.")
        return 0
    if failures := stop_services():
        raise Die("could not stop TauCetiWorker services: " + "; ".join(failures))
    if reason := _busy(paths, roots):
        raise Die(f"{reason}; stop it first (`tauceti workers manager-stop`), then re-run purge")
    from .retention import _drain, _idle_root

    failed = []
    with contextlib.ExitStack() as held:
        for root in roots:
            if not held.enter_context(_idle_root(root)):
                raise Die(f"a worker or round holds a lock in {root}; stop it first, then re-run purge")
        for path in paths:
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif not _drain(path):
                    raise OSError("some entries could not be removed")
                print(f"removed {path}")
            except OSError as error:
                failed.append(path)
                print(f"could not remove {path}: {error}", file=sys.stderr)
    if sys.platform == "darwin":
        with contextlib.suppress(OSError):
            user_data_root().parent.rmdir()  # …/Application Support/tauceti, once empty
    for item in overridden:
        print(f"not removed, set by an override: {item}")
    return 1 if failed else 0

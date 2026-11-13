"""`tauceti purge`: remove everything TauCetiWorker stored for this user.

Run it before `uv tool uninstall tauceti` (or deleting a clone) to leave nothing behind.
Without --yes it only prints the plan. ~/.elan is left alone: it holds Lean toolchains that
the user's own projects share; `elan toolchain gc` reclaims unused ones.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Die
from .paths import HERE, legacy_data_root, user_data_root


def _candidates(include_config):
    """(path, whole) pairs: whole directories we own, or data parts inside a root we don't."""
    from . import (
        activity,
        activity_live,
        budget,
        expanded_cache,
        github_transport,
        overlap_support,
        shared_mathlib,
        shared_repositories,
        verification,
        verification_storage,
    )
    from .retention import RETIRED_PARTS, retired_roots
    from .worker_manager import config_home, workers_state_dir

    pairs = []
    selected = legacy_data_root().resolve()
    for root in [selected, *retired_roots()]:
        if root == user_data_root().resolve():
            pairs.append((root, True))
            continue
        # A source checkout, a Fleet runtime or an operator-chosen root holds more than our data.
        pairs += [(root / part, True) for part in RETIRED_PARTS]
        pairs += [(path, True) for path in root.glob(".tauceti-retired-*")]
    home = Path.home()
    pairs += [
        (workers_state_dir().parent, True),  # ~/.local/state/tauceti: activity, budget, projects…
        (activity.root(), True),
        (activity_live.root(), True),
        (budget.budget_dir(), True),
        (github_transport.root(), True),
        (shared_repositories.root(), True),
        (overlap_support.root(), True),
        (shared_mathlib.root(), True),
        (expanded_cache.root(), True),
        (verification_storage.root(), True),
        (verification.cache_root(), True),
        (home / ".cache" / "tauceti", True),
    ]
    if sys.platform == "darwin":
        pairs.append((home / ".tauceti", True))  # isolated worker homes (agents._worker_iso_home)
    if include_config:
        pairs.append((config_home(), True))
    return pairs


def _guarded(path):
    """Refuse anything that could be more than ours: the code, a home, or a filesystem root."""
    path = Path(os.path.abspath(path))
    home = Path.home().resolve()
    forbidden = {Path(path.anchor), home, home / ".cache", home / ".local", home / ".local" / "share"}
    forbidden |= {home / ".local" / "state", home / ".config", home / "Library", home / ".elan"}
    forbidden |= {home / "Library" / "Application Support"}
    if path in forbidden or home.is_relative_to(path) or len(path.parts) < 3:
        return False
    return not Path(HERE).resolve().is_relative_to(path)  # never the code or a directory holding it


def plan(include_config=False):
    seen, result = set(), []
    for path, _whole in _candidates(include_config):
        path = Path(os.path.abspath(path))
        if path in seen or not (path.exists() or path.is_symlink()):
            continue
        seen.add(path)
        if not _guarded(path):
            raise Die(f"refusing to purge {path}: it is not a dedicated TauCetiWorker directory")
        result.append(path)
    # Remove children before parents so the summary names each store once.
    nested = {p for p in result for q in result if p != q and p.is_relative_to(q)}
    return [p for p in result if p not in nested]


def _workers_running():
    from .retention import _idle_root
    from .worker_manager import _manager_lock_held

    if _manager_lock_held():
        return "the worker manager is running"
    with _idle_root(legacy_data_root().resolve()) as idle:
        if not idle:
            return "a worker or round is running"
    return None


def _remove_services():
    """Stop and delete the worker service and the cleanup/activity timers before their data."""
    from .worker_manager import _service_path

    service = _service_path()
    if sys.platform == "darwin":
        if service.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{service.stem}"], check=False)
            service.unlink(missing_ok=True)
        return
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return
    directory = service.parent
    for unit in ("tauceti-cleanup.timer", "tauceti-activity.timer", service.name):
        if (directory / unit).exists():
            subprocess.run([systemctl, "--user", "disable", "--now", unit], check=False, capture_output=True)
    for unit in ("tauceti-cleanup.timer", "tauceti-cleanup.service", "tauceti-activity.timer"):
        (directory / unit).unlink(missing_ok=True)
    (directory / "tauceti-activity.service").unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    subprocess.run([systemctl, "--user", "daemon-reload"], check=False, capture_output=True)


def cmd_purge(args):
    paths = plan(include_config=args.config)
    if not args.yes:
        print("tauceti purge would remove:" if paths else "tauceti purge: nothing to remove")
        for path in paths:
            print(f"  {path}")
        print("plus the tauceti-workers service and the tauceti-cleanup/tauceti-activity timers, if installed.")
        if paths:
            print("Re-run with --yes to delete them. ~/.elan is kept; `elan toolchain gc` reclaims toolchains.")
        return 0
    if reason := _workers_running():
        raise Die(f"{reason}; stop it first (`tauceti workers manager-stop`), then re-run purge")
    _remove_services()
    from .retention import _rmtree

    failed = []
    for path in paths:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                _rmtree(path)
            print(f"removed {path}")
        except OSError as error:
            failed.append(path)
            print(f"could not remove {path}: {error}", file=sys.stderr)
    if sys.platform == "darwin":
        with contextlib.suppress(OSError):
            user_data_root().parent.rmdir()  # …/Application Support/tauceti, once empty
    return 1 if failed else 0

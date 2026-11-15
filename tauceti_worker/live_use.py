"""Process ownership and live-use observations, and small Git helpers for worker checkouts.

Cleanup never deletes a tree a live process still uses: ``live_users`` reports the visible
processes whose working directory, root or open files lie inside a path. ``process_owner`` and
``owner_stopped`` record and test a process's birth identity without PID namespace support.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from .paths import canonical_candidate

# Untracked or ignored directories that hold dependency/build caches, never source.
CACHE_DIRS = frozenset(
    {".lake", "lake-packages", ".cache", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".venv"}
)
# Reserved names used by crash-resumable retention deletion contain only generated artifacts.
CACHE_DIRS |= frozenset(name + ".retention-deleting" for name in CACHE_DIRS)
CACHE_TOP = frozenset({"build"})  # Lake's legacy top-level build directory
CACHE_TOP |= frozenset(name + ".retention-deleting" for name in CACHE_TOP)


def _git(repo: Path, *args: str) -> str | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent.resolve())
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def rev(repo: Path, name: str) -> str | None:
    return (_git(repo, "rev-parse", "-q", "--verify", f"{name}^{{commit}}") or "").strip() or None


def common_dir(repo: Path) -> Path:
    value = _git(repo, "rev-parse", "--git-common-dir")
    if value is None:
        raise OSError(f"{repo}: not a Git checkout")
    path = Path(value.strip())
    return path if path.is_absolute() else (repo / path).resolve()


def dependent_worktrees(repo: Path) -> list[str]:
    """Existing linked worktrees, other than ``repo`` itself, that use ``repo``'s Git store.

    Deleting a standalone clone would break them even when no process uses them now."""
    if checkout_kind(repo) != "standalone":
        return []
    listing = _git(repo, "worktree", "list", "--porcelain")
    if listing is None:
        return ["worktree list unavailable"]
    here = repo.resolve()
    found = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            if path.resolve() != here and path.exists():
                found.append(str(path))
    return found


def checkout_kind(repo: Path) -> str | None:
    """ "standalone" for an ordinary clone, "worktree" for a linked worktree, None otherwise.

    A linked worktree must be the top level of its own work tree and its Git directory must be
    a `worktrees/<name>` entry of its common repository; a symlinked `.git` is never accepted.
    """
    dotgit = repo / ".git"
    if dotgit.is_symlink():
        return None
    if dotgit.is_dir():
        return "standalone"
    if not dotgit.is_file():
        return None
    value = _git(repo, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    try:
        top, gitdir, common = (value or "").splitlines()
    except ValueError:
        return None
    common_path = Path(common) if Path(common).is_absolute() else repo / common
    if Path(top).resolve() != repo.resolve() or Path(gitdir).resolve().parent != (common_path / "worktrees").resolve():
        return None
    return "worktree"


def process_owner():
    """Process birth attribution without requiring PID namespace support."""
    import sys

    from .usage_lifecycle import identity

    if sys.platform != "linux":
        owner = identity()
        if sys.platform == "darwin" and os.getsid(0) == os.getpid():
            owner["session"] = os.getpid()
        return owner
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    owner = dict(pid=os.getpid(), start=fields[19], boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    if os.getsid(0) == os.getpid():
        owner["session"] = os.getpid()  # ordinary dedicated round session, when one exists
    return owner


def owner_stopped(owner):
    """Unknown/unreadable owners stay queued; only this candidate is withheld."""
    import sys
    import uuid

    if not isinstance(owner, dict) or not {"pid", "start", "boot"} <= owner.keys():
        return False
    if sys.platform != "linux":
        from .usage_lifecycle import alive

        return not alive({key: value for key, value in owner.items() if key != "session"})
    if not isinstance(owner["start"], str) or not owner["start"].isdigit() or not isinstance(owner["boot"], str):
        return False
    try:
        uuid.UUID(owner["boot"])
        pid = int(owner["pid"])
        if pid <= 0:
            return False
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot != owner["boot"]:
            return True
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] != owner["start"] or fields[0] in {"Z", "X"}
    except (ProcessLookupError, FileNotFoundError):
        # A missing boot-id file is unavailable ownership evidence, rather than a dead PID.
        return Path("/proc/sys/kernel/random/boot_id").is_file() and not Path(f"/proc/{pid}").exists()
    except (OSError, ValueError, TypeError, IndexError):
        return False


def _darwin_process_references():
    """Read visible references, with the same per-process permission limits as Linux procfs."""
    try:
        import psutil
    except ImportError:
        return None

    result = []
    try:
        # Walk the parent chain by hand: psutil's parents() ends the walk at the lowest PID it
        # takes from pids(), so on macOS (PID 0 is its own parent) any doubt there loops forever.
        skip = {os.getpid()}
        process = psutil.Process()
        while True:
            ppid = process.ppid()
            if ppid <= 1 or ppid in skip:
                break
            skip.add(ppid)
            process = psutil.Process(ppid)
        for pid in psutil.pids():
            if pid <= 0:
                continue
            try:
                process = psutil.Process(pid)
                with contextlib.suppress(psutil.Error, OSError):
                    if process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                        continue
                session = None
                with contextlib.suppress(OSError):
                    session = os.getsid(pid)
                paths = []
                if pid not in skip:
                    visible = False
                    with contextlib.suppress(psutil.Error, OSError):
                        uids = process.uids()
                        visible = os.geteuid() == 0 or uids.real == os.getuid() or uids.effective == os.geteuid()
                    # Like Linux procfs, another user's private references are
                    # outside this best-effort observation. Inspect all as root.
                    if visible:
                        with contextlib.suppress(psutil.Error, OSError):
                            paths.append(process.cwd())
                        with contextlib.suppress(psutil.Error, OSError):
                            files = process.open_files()
                            paths.extend(item.path for item in files)
                        # Kernel vnode paths are already canonical. Resolving them
                        # could block on unrelated network mounts or autofs triggers.
                result.append((str(pid), session, f"{pid} (process)", paths))
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue  # Exits during observation are ordinary.
            except (psutil.Error, OSError):
                continue  # One private/disappearing process must not discard the inventory.
    except (psutil.Error, OSError):
        return None
    return result


def process_references():
    """One read-only process inventory, reusable within a cleanup pass only."""
    import sys

    if sys.platform == "darwin":
        return _darwin_process_references()
    if sys.platform != "linux":
        return None
    proc = Path("/proc")
    if not (proc / "self").exists():
        return None
    skip = set()
    pid = os.getpid()
    while pid > 1:
        skip.add(pid)
        try:
            pid = int((proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        session = None
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] not in {"Z", "X"}:
                session = int(fields[3])
        except (OSError, ValueError, IndexError):
            pass
        paths = []
        if int(entry.name) not in skip:
            links = [entry / "cwd", entry / "root"]
            with contextlib.suppress(OSError):
                links += list((entry / "fd").iterdir())
            for link in links:
                with contextlib.suppress(OSError):
                    where = os.readlink(link)
                    if where.startswith("/"):
                        paths.append(where)
        result.append((entry.name, session, entry.name, paths))
    return result


def referenced_users(repo: Path, references, *, session=None) -> list[str]:
    if references is None:
        return ["process observations unavailable"]
    try:
        target = str(canonical_candidate(repo))
    except OSError:
        return ["candidate path unavailable"]
    prefix = target.rstrip("/") + "/"
    found = []
    for pid, process_session, label, paths in references:
        if session is not None and process_session == session:
            found.append(f"{pid} (owner session)")
        elif any(where == target or where.startswith(prefix) for where in paths):
            if label == pid:
                with contextlib.suppress(OSError):
                    label = f"{pid} ({(Path('/proc') / pid / 'comm').read_text().strip()})"
            found.append(label)
    return found


def live_users(repo: Path, *, session=None) -> list[str]:
    """Fresh process references for ordinary callers; no cached negative liveness evidence."""
    return referenced_users(repo, process_references(), session=session)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

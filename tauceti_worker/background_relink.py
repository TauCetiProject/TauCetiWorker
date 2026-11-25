"""Relink a worker's idle build tree between rounds, off every round's critical path.

The agent needs only the hard-link probe before it starts. The full canonical pass, which also
sweeps the worker's private Lake store and usually runs out of time, runs here instead: in a child
of the worker's loop, after a round's process group has exited, on the `.lake` that round left.

- `request()`: after its round-end relink, the round names the checkout it built in.
- `start()`: the loop spawns `_relink-between-rounds` once the round child has exited.
- `stop()`: the next round, before it touches any checkout (and retention, before it deletes a
  worker's caches), stops the child and takes the worker's build-tree lock. A round keeps it until
  it exits.

The build-tree lock (`<worker state>/build-tree.lock`) is the boundary. The child relinks only
while it holds the lock, and every round that prepares a checkout holds it, so a pass never runs
beside Lake or the agent. The canonical store's own checks (no Lake or Lean naming the tree,
nothing open for writing, nothing changed since inspected) still apply to every file.

Linux only: the stop handshake checks the holder's command line in `/proc`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import log
from .runtime_status import read_json

LOCK = "build-tree.lock"
REQUESTS = "relink-between-rounds.json"
COMMAND = "_relink-between-rounds"
# How long the next round waits for a stopped pass to save its cursor before killing it.
STOP_WAIT = 20.0
KILL_WAIT = 10.0
# What a pass needs from the round's environment to find the same stores.
CARRIED = (
    "LAKE_CACHE_DIR",
    "TAUCETI_BUILD_CACHE_ROOT",
    "TAUCETI_SHARED_ARTIFACTS",
    "TAUCETI_CANONICAL_RELINK",
    "TAUCETI_RELINK_SLOTS",
    "TAUCETI_BACKGROUND_RELINK_BUDGET_S",
)
# Lock descriptors this process holds, by worker state directory.
_HELD: dict[str, int] = {}


def supported() -> bool:
    return sys.platform.startswith("linux") and os.path.isdir("/proc/self")


def enabled(env=None) -> bool:
    """On by default; `TAUCETI_BACKGROUND_RELINK=0` relinks only at round end."""
    env = os.environ if env is None else env
    return supported() and env.get("TAUCETI_BACKGROUND_RELINK", "1").strip().lower() not in {"0", "false", "no", "off"}


def worker_state(path: Path) -> Path:
    """The worker state directory holding `round.lock`: `path`, or above a source's state."""
    path = Path(os.path.realpath(path))
    for candidate in [path, *list(path.parents)[:4]]:
        if (candidate / "round.lock").is_file():
            return candidate
    return path


def parked(checkout: Path) -> Path:
    """Where `round_worktrees` keeps a worktree's `.lake` between rounds."""
    return checkout.with_name(checkout.name + ".lake-between-rounds")


def _idle_lake(checkout: Path, env) -> Path | None:
    """The tree's `.lake` once the round has let go of it, or None.

    A per-round worktree's `.lake` is relinked only where release parked it: a worktree still in
    place was not released, and another worker's sweep may remove it under the round lock. A
    persistent worktree, like an ordinary checkout, keeps its `.lake` in place between rounds.
    """
    from .round_worktrees import enabled as worktrees
    from .round_worktrees import persistent

    inside = checkout / ".lake"
    lake = parked(checkout)
    if worktrees(env) and not persistent(env):
        return lake if lake.is_dir() and not lake.is_symlink() and not inside.exists() else None
    if inside.is_dir() and not inside.is_symlink():
        return inside
    # Parked by a round before this host kept its worktrees, and not yet unparked.
    return lake if worktrees(env) and lake.is_dir() and not lake.is_symlink() and not inside.exists() else None


# Set by the loop for the rounds it spawns: only a loop starts the between-rounds pass afterwards.
LOOP_ROUND = "TAUCETI_LOOP_ROUND"
# Round ends that may defer relinking in a row before one relinks itself again: a pass the next
# rounds keep stopping before it converges anything must not leave a tree unshared for ever.
MAX_DEFERRED = 3


def _requests():
    if len(_HELD) != 1:
        return None, {}
    path = Path(next(iter(_HELD))) / REQUESTS
    rows = read_json(path)
    return path, rows if isinstance(rows, dict) else {}


def will_follow(env, checkout: Path) -> bool:
    """Whether this round end may leave relinking to a between-rounds pass.

    Only for a loop's round holding one worker's build-tree lock, and only while earlier rounds have
    not deferred this tree `MAX_DEFERRED` times without a pass completing it.
    """
    if not enabled(env) or os.environ.get(LOOP_ROUND) != "1":
        return False
    path, rows = _requests()
    if path is None:
        return False
    row = rows.get(str(Path(checkout).absolute()))
    return not (isinstance(row, dict) and int(row.get("deferred", 0) or 0) >= MAX_DEFERRED)


def request(checkout: Path, env, *, deferred=False) -> bool:
    """Ask for a full pass on `checkout`'s tree after this round (the caller holds the lock).

    `deferred` says this round end relinked nothing itself; consecutive deferrals are counted.
    True once the request is on disk.
    """
    if not enabled(env):
        return False
    checkout = Path(checkout).absolute()
    path, rows = _requests()
    if path is None:
        return False  # Not a round that took one worker's build-tree lock: nothing would keep a pass out.
    try:
        toolchain = (checkout / "lean-toolchain").read_text().strip()
    except OSError:
        toolchain = ""
    previous = rows.get(str(checkout))
    count = int(previous.get("deferred", 0) or 0) if isinstance(previous, dict) else 0
    rows[str(checkout)] = dict(
        requested_at=time.time(),
        toolchain=toolchain,
        env={key: env[key] for key in CARRIED if env.get(key) is not None},
        deferred=count + 1 if deferred else 0,
    )
    from .runtime_status import atomic_json

    try:
        atomic_json(path, rows)
    except OSError as error:
        log(f"canonical store: between-rounds relink not requested: {error}")
        return False
    return True


def start(state: Path, running: subprocess.Popen | None = None) -> subprocess.Popen | None:
    """From the loop, between rounds: spawn the pass if one was requested and none is running."""
    if running is not None and running.poll() is None:
        return running
    state = worker_state(state)
    if not enabled() or not (state / REQUESTS).is_file():
        return None
    from .paths import self_argv, self_env

    try:
        return subprocess.Popen(self_argv(COMMAND, "--state-dir", str(state)), env=self_env(), stdin=subprocess.DEVNULL)
    except OSError as error:
        log(f"canonical store: between-rounds relink not started: {error}")
        return None


def finish(running: subprocess.Popen | None) -> None:
    """When the loop exits: ask a running pass to stop, then make sure it has."""
    if running is None or running.poll() is not None:
        return
    with contextlib.suppress(OSError):
        running.send_signal(signal.SIGTERM)
    try:
        running.wait(STOP_WAIT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            running.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            running.wait(KILL_WAIT)


def _syscall(number, *args) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(number, *args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _pidfd_open(pid: int) -> int:
    """`pidfd_open(2)`; some Python builds lack the wrapper. Linux's unified syscall table: 434."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    import ctypes

    return _syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def _pidfd_signal(handle: int, sig: int) -> None:
    """`pidfd_send_signal(2)`, 424 in the same table."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(handle, sig)
        return
    import ctypes

    _syscall(424, ctypes.c_int(handle), ctypes.c_int(sig), None, ctypes.c_uint(0))


class _Pid:
    """A kernel without pidfds: signal by pid (the command line was checked just before)."""

    def __init__(self, pid):
        self.pid = pid


def _signal(handle, sig: int) -> None:
    if isinstance(handle, _Pid):
        os.kill(handle.pid, sig)
    else:
        _pidfd_signal(handle, sig)


def _close(handle) -> None:
    if not isinstance(handle, _Pid):
        with contextlib.suppress(OSError):
            os.close(handle)


def _holder(fd: int, state: Path):
    """A handle on the process recorded in the lock, if it is this worker's pass, else None.

    The pidfd is opened before the command line is read and checked alive after it, so the
    process signalled is the one whose command line was checked, even if its pid is reused.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        pid = int(os.read(fd, 32).decode().strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        handle = _pidfd_open(pid)
    except OSError as error:
        if error.errno != errno.ENOSYS:
            return None
        handle = _Pid(pid)
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        _signal(handle, 0)  # Still the process whose command line was read.
    except OSError:
        _close(handle)
        return None
    if COMMAND.encode() in argv and str(state).encode() in argv:
        return handle
    _close(handle)
    return None


def _acquire(state: Path) -> int | None:
    """The worker's build-tree lock, stopping a between-rounds pass that holds it.

    SIGTERM first; SIGKILL once `STOP_WAIT` has passed; failure once `KILL_WAIT` more has passed,
    whatever happens to the holder or the signals.
    """
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(fd, False)
    started = time.monotonic()
    stop_by, give_up = started + STOP_WAIT, started + STOP_WAIT + KILL_WAIT
    signalled = killed = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if signalled:
                log(f"canonical store: stopped the between-rounds relink in {time.monotonic() - started:.1f}s")
            return fd
        except BlockingIOError:
            pass
        now = time.monotonic()
        if now >= give_up:
            os.close(fd)
            log(f"canonical store: {state / LOCK} still held after {now - started:.0f}s")
            return None
        if not killed and (not signalled or now >= stop_by):
            handle = _holder(fd, state)
            if handle is not None:
                try:
                    if not signalled:
                        _signal(handle, signal.SIGTERM)
                        signalled = True
                    else:
                        log("canonical store: the between-rounds relink did not stop; killing it")
                        _signal(handle, signal.SIGKILL)
                        killed = True
                except OSError:
                    pass  # Gone already, or not ours to signal: the deadline still applies.
                finally:
                    _close(handle)
        time.sleep(0.1)


def stop(state: Path) -> bool:
    """Before a round touches a checkout: stop any pass and keep the lock until this process exits."""
    if not supported():
        return True
    state = worker_state(state)
    if str(state) in _HELD:
        return True
    fd = _acquire(state)
    if fd is None:
        return False
    _HELD[str(state)] = fd
    return True


@contextlib.contextmanager
def paused(state: Path):
    """Hold the build-tree lock for a block (a collector deleting caches), then let it go.

    Yields False, doing nothing, when a pass would not stop.
    """
    state = worker_state(state)
    if not supported() or str(state) in _HELD:
        yield True
        return
    fd = _acquire(state)
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)


def main(state_dir: str) -> int:
    """The child: relink each requested tree while holding the lock, until done or stopped."""
    from . import canonical_store

    state = worker_state(state_dir)
    try:
        fd = os.open(state / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # A round, or another pass, has the tree.
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    with contextlib.suppress(OSError):
        os.nice(10)  # Maintenance: rounds' builds come first.
    path = state / REQUESTS
    rows = read_json(path)
    rows = rows if isinstance(rows, dict) else {}
    done = []
    for checkout, row in rows.items():
        if stopping:
            break
        if not isinstance(row, dict):
            done.append(checkout)
            continue
        lake = _idle_lake(Path(checkout), os.environ)
        if lake is None or canonical_store.interrupted(lake):
            # Gone, or not idle: the next round-end asks again. An interrupted round's tree may
            # still have its agent's processes, and the next round checks it before its agent.
            done.append(checkout)
            continue
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (row.get("env") or {}).items() if k in CARRIED})
        if row.get("toolchain"):
            env["ELAN_TOOLCHAIN"] = str(row["toolchain"])  # The checkout itself may be gone.
        report = canonical_store.between_rounds(Path(checkout), lake, env, stop=lambda: bool(stopping))
        if report is None or not (report.get("incomplete") or report.get("skipped")):
            done.append(checkout)
    if done:
        rows = read_json(path)
        rows = rows if isinstance(rows, dict) else {}
        for checkout in done:
            rows.pop(checkout, None)
        try:
            if rows:
                from .runtime_status import atomic_json

                atomic_json(path, rows)
            else:
                path.unlink(missing_ok=True)
        except OSError as error:
            log(f"canonical store: between-rounds requests not updated: {error}")
    os.ftruncate(fd, 0)
    os.close(fd)
    return 0

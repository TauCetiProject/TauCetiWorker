"""Private lifetime supervisor, frozen at import and run in an isolated Python.

Linux subreaping keeps detached/double-forked tool descendants parented here.
Only our own children are signalled; no host process or historical PID scan runs.
Other platforms retain the existing process-group boundary.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def enable_subreaper():
    if sys.platform != "linux":
        return False
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish child lifetime supervisor")
    # Require the kernel's direct-child inventory before starting any tool.
    Path(f"/proc/self/task/{os.getpid()}/children").read_text()
    return True


def reap_exited():
    while True:
        try:
            if not os.waitpid(-1, os.WNOHANG)[0]:
                return False
        except ChildProcessError:
            return True


def reap_orphans(tool_pid):
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
        if info is None or info.si_pid == tool_pid:
            return  # Popen owns its tool's status; poll() consumes it next.
        os.waitpid(info.si_pid, 0)


def reap_children(grace=2.0, timeout=30.0):
    """Kill and reap successive generations adopted after their parents exit."""
    inventory = Path(f"/proc/self/task/{os.getpid()}/children")
    started = time.monotonic()
    deadline = started + grace + timeout
    warned = set()
    signalled = set()
    while True:
        # We are the only reaper. A listed child cannot have its PID recycled
        # between this observation and kill: it stays our zombie until waitpid.
        for pid in map(int, inventory.read_text().split()):
            action = signal.SIGKILL if time.monotonic() - started >= grace else signal.SIGTERM
            if (pid, action) in signalled:
                continue
            try:
                os.kill(pid, action)
                signalled.add((pid, action))
            except ProcessLookupError:
                pass
            except PermissionError:
                if pid not in warned:
                    print("child lifetime cleanup: cannot signal an owned child", file=sys.stderr, flush=True)
                    warned.add(pid)
        if reap_exited():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def reap_group(child):
    """Other kernels retain a private tool group and the ordinary TERM grace."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def main():
    # Never inherit automatic child reaping: PID ownership lasts until waitpid.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    subreaper = enable_subreaper()
    reader = int(sys.argv[1])
    stopped = threading.Event()
    stop_signal = signal.SIGTERM

    def stop(signum=None, _frame=None):
        nonlocal stop_signal
        if not stopped.is_set() and signum is not None:
            stop_signal = signum
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)

    def parent_gone():
        while os.read(reader, 1):
            pass
        stop()

    threading.Thread(target=parent_gone, daemon=True).start()
    lease = os.environ.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
    child = None
    cleaned = True
    cancelled = False
    try:
        if stopped.is_set():
            return 143
        environment = dict(os.environ)
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        # Git's normal maintenance must finish inside the owned tool lifetime,
        # rather than detach and be mistaken for abandoned agent work.
        for index, key in enumerate(("gc.autoDetach", "maintenance.autoDetach"), count):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = "false"
        environment["GIT_CONFIG_COUNT"] = str(count + 2)
        try:
            child = subprocess.Popen(
                sys.argv[2:],
                env=environment,
                close_fds=True,
                start_new_session=not subreaper,
                pass_fds=(int(lease),) if lease else (),
            )
        except OSError:
            print("owned tool could not start", file=sys.stderr, flush=True)
            return 127
        while child.poll() is None and not stopped.wait(0.02):
            if subreaper:
                reap_orphans(child.pid)
        cancelled = stopped.is_set() and child.poll() is None
        result = child.returncode if child.returncode is not None else 143
    finally:
        if subreaper and child is not None:
            cleaned = reap_children()
            child.poll()  # Synchronize Popen after our waitpid consumed its child.
        elif child is not None:
            reap_group(child)
    if not cleaned:
        print("child lifetime cleanup incomplete", file=sys.stderr, flush=True)
        return 70
    if cancelled:
        # Preserve subprocess signal status after cleanup, so verification can
        # distinguish interruption from an ordinary failing command.
        signal.signal(stop_signal, signal.SIG_DFL)
        os.kill(os.getpid(), stop_signal)
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    try:
        os._exit(main())
    except BaseException:
        # No request arguments, environment or provider output in diagnostics.
        print("child lifetime supervisor failed", file=sys.stderr, flush=True)
        os._exit(70)

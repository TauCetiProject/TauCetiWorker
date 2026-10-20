"""Ordinary subprocess execution with best-effort owned process-group cleanup."""

import contextlib
import os
import subprocess
import sys
import threading

from .round import reap_round_group

# The existing agent/downloader guardian also supervises other tools. Its only
# lifetime evidence is its owner's pipe; it never scans host or historical state.
PARENT_BOUND_AGENT = """
import os, signal, subprocess, sys, threading
fd = int(sys.argv[1])
def parent_gone():
    while os.read(fd, 1):
        pass
    os.killpg(os.getpgrp(), signal.SIGKILL)
threading.Thread(target=parent_gone, daemon=True).start()
lease = os.environ.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
child = subprocess.Popen(sys.argv[2:], close_fds=True, pass_fds=(int(lease),) if lease else ())
rc = child.wait()
os._exit(rc if rc >= 0 else 128 - rc)
"""


@contextlib.contextmanager
def owned_process(argv, **kwargs):
    """Own a tool group, cleaning it on leader exit and owner death.

    A waiter sweeps the group even when inherited stdout keeps a reader busy.
    Deliberately detached descendants can escape this best-effort cleanup.
    """
    reader, writer = os.pipe()
    process = None
    watcher = None
    cleanup_lock = threading.Lock()

    def cleanup():
        with cleanup_lock:
            reap_round_group(process.pid)

    def wait_and_cleanup():
        process.wait()
        cleanup()

    try:
        try:
            extra_fds = tuple(kwargs.pop("pass_fds", ()))
            environment = kwargs.get("env")
            environment = os.environ if environment is None else environment
            lease = environment.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
            if lease is not None:
                try:
                    lease_fd = int(lease)
                    os.fstat(lease_fd)
                except (OSError, ValueError):
                    # Intermediate shells may close inherited descriptors. A stale
                    # optional lease hint must not prevent ordinary tool execution.
                    kwargs["env"] = dict(environment)
                    kwargs["env"].pop("TAUCETI_SHARED_MATHLIB_LEASE_FD", None)
                else:
                    extra_fds = tuple(dict.fromkeys((*extra_fds, lease_fd)))
            process = subprocess.Popen(
                [sys.executable, "-c", PARENT_BOUND_AGENT, str(reader), *argv],
                start_new_session=True,
                pass_fds=(reader, *extra_fds),
                **kwargs,
            )
        finally:
            os.close(reader)
        thread = threading.Thread(target=wait_and_cleanup, daemon=True)
        thread.start()
        watcher = thread
        yield process
    finally:
        try:
            if process is not None:
                if process.poll() is None or watcher is None:
                    cleanup()
                process.wait()
                if watcher is not None:
                    watcher.join()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        finally:
            os.close(writer)


def run(argv, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """Run a command with ordinary owned-group cleanup and subprocess results."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input cannot both be supplied")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output cannot be combined with stdout or stderr")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with owned_process(argv, **kwargs) as process:
        stdout, stderr = process.communicate(input, timeout=timeout)
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result

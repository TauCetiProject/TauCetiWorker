"""Ordinary subprocess execution with best-effort owned process-group cleanup."""

import subprocess

from .round import reap_round_group


def run(argv, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """Like subprocess.run, sweeping the owned group on exit or interruption.

    Deliberately detached descendants can escape this cleanup. No host-wide state,
    namespace capability or historical cleanup evidence is consulted.
    """
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input cannot both be supplied")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output cannot be combined with stdout or stderr")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(argv, start_new_session=True, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        finally:
            reap_round_group(process.pid)
            process.wait()
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result

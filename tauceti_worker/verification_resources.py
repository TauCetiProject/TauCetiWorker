"""Standalone verification resource signalling, also staged into Bubble."""

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RESOURCE_ERROR = re.compile(
    r"ThreadPoolBuildError|failed to (?:create|spawn) thread|thread constructor failed|pthread_create.*(?:unavailable|EAGAIN)",
    re.I,
)
DEFAULTS = {"LEAN_NUM_THREADS": "4", "RAYON_NUM_THREADS": "1"}
WAIT_ENV = "TAUCETI_BUILD_RESOURCE_WAIT"


def defaults(env):
    for name, value in DEFAULTS.items():
        env.setdefault(name, value)
    return env


def queue_owned(labels):
    return "bors-staging" in {str(x.get("name", "") if isinstance(x, dict) else x).lower() for x in labels or []}


def wait_reason():
    try:
        return json.loads(Path(os.environ[WAIT_ENV]).read_text()).get("reason")
    except (KeyError, OSError, ValueError, TypeError):
        return None


def record_wait(reason):
    if path := os.environ.get(WAIT_ENV):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps({"reason": reason, "at": time.time()}))
        os.replace(temporary, target)


def clear_wait():
    if path := os.environ.get(WAIT_ENV):
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)


def verify_shared(args):
    """Stream the unchanged shared checks, recording resource failure before an agent can retry it."""
    if reason := wait_reason():
        print(f"Verification deferred: {reason}; preserve work for the next round.", flush=True)
        return 75
    defaults(os.environ)
    command = [
        sys.executable,
        "-c",
        "import sys; from tauceti_tools.templates import run_script; raise SystemExit(run_script('local_checks.py', sys.argv[1:]))",
        *args,
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
    ) as child:
        for line in child.stdout:
            print(line, end="", flush=True)
            if RESOURCE_ERROR.search(line):
                record_wait("build tool could not create threads; host resource exhaustion")
        rc = child.wait()
    if rc == 0:
        clear_wait()
        return 0
    return 75 if wait_reason() else rc

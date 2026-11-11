#!/usr/bin/env python3
"""A command run without the service's directory variables must find the running manager.

Without this, `tauceti workers --config <fleet config> restart <id>` from a plain shell resolved
its own runtime directory, found no manager there, and started a second manager over the same
configuration: a duplicate copy of every worker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

root = Path(tempfile.mkdtemp(prefix="tauceti-manager-discovery-"))
for key in ("TAUCETI_CONFIG_HOME", "TAUCETI_WORKERS_CONFIG", "TAUCETI_WORKERS_STATE_DIR", "TAUCETI_RUNTIME_DIR"):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["XDG_RUNTIME_DIR"] = str(root / "xdg-run")
(root / "xdg-run").mkdir(mode=0o700)
os.environ["TAUCETI_MANAGER_TEST_COMMAND"] = "true"

import tauceti_worker.worker_manager as wm

service_run = root / "service-run"
service_state = root / "service-state"
config = root / "fleet" / "workers.toml"
config.parent.mkdir()
wm.save_worker_specs(config, [])


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


manager = subprocess.Popen(
    [sys.executable, "-m", "tauceti_worker", "workers", "--config", str(config), "manager", "--interval", "0.05"],
    cwd=REPO,
    env=dict(os.environ, TAUCETI_RUNTIME_DIR=str(service_run), TAUCETI_WORKERS_STATE_DIR=str(service_state)),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
try:
    pointer = wm.manager_pointer(config)
    wait_for(lambda: wm.read_json(pointer).get("pid") == manager.pid)
    wait_for(lambda: (service_run / "manager.sock").exists())

    # A plain shell (no directory variables) adopts the service's directories.
    wm.adopt_manager_dirs(config)
    assert os.environ["TAUCETI_RUNTIME_DIR"] == str(service_run.resolve()), os.environ["TAUCETI_RUNTIME_DIR"]
    assert os.environ["TAUCETI_WORKERS_STATE_DIR"] == str(service_state.resolve())
    assert wm.manager_request("ping")["pid"] == manager.pid

    # The CLI finds that manager instead of starting another one under XDG_RUNTIME_DIR.
    env = {k: v for k, v in os.environ.items() if k not in ("TAUCETI_RUNTIME_DIR", "TAUCETI_WORKERS_STATE_DIR")}
    result = subprocess.run(
        [sys.executable, "-m", "tauceti_worker", "workers", "--config", str(config), "apply"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "xdg-run" / "tauceti" / "manager.lock").exists(), "a second manager was started"

    # Directories that disagree with the running manager are refused rather than used.
    conflicting = dict(env, TAUCETI_RUNTIME_DIR=str(root / "other-run"))
    result = subprocess.run(
        [sys.executable, "-m", "tauceti_worker", "workers", "--config", str(config), "apply"],
        cwd=REPO, env=conflicting, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0 and "would start a second manager" in result.stdout + result.stderr, (
        result.stdout + result.stderr
    )
    assert not (root / "other-run" / "manager.lock").exists(), "a second manager was started"

    # A clean shutdown removes the record; a stale record is ignored.
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(20)
    assert not pointer.exists(), "manager left its location record behind"
    wm.atomic_json(pointer, {"pid": manager.pid, "runtime_dir": str(service_run), "state_dir": str(service_state)})
    os.environ["TAUCETI_RUNTIME_DIR"] = str(root / "fresh-run")
    wm.adopt_manager_dirs(config)  # dead manager: no error, nothing adopted
    assert os.environ["TAUCETI_RUNTIME_DIR"] == str(root / "fresh-run")
    print("worker manager discovery: OK")
finally:
    if manager.poll() is None:
        manager.terminate()
        manager.wait(10)
    shutil.rmtree(root, ignore_errors=True)

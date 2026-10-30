#!/usr/bin/env python3
"""Two disposable workers exercise native containment; no API calls or memory pressure."""

import dataclasses
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_worker import activity
from tauceti_worker import worker_manager as wm
from tauceti_worker import worker_scopes as scopes


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=15, check=check)


def wait(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("worker scope lifecycle did not converge")


def gone(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def grandchild_pid(wid):
    try:
        return int(Path(str(wm.status_path(wid)) + ".grandchild").read_text())
    except (FileNotFoundError, ValueError):
        return None


# A CI victim must remain visible, and missing journal permissions must not be
# reported as a healthy zero-kill observation or escape into supervision.
with tempfile.TemporaryDirectory(prefix="tauceti-oom-observation-") as raw:
    state = Path(raw)
    record = dict(
        __CURSOR="native-ci-event",
        __REALTIME_TIMESTAMP="1793340000000000",
        MESSAGE="Marked /user.slice/user-1003.slice/user@1003.service/tauceti.slice/"
        "tauceti-fleet.slice/tauceti-fleet-ci.slice/tauceti-fleet-vm-example.service for killing due to "
        "memory pressure for /user.slice being 90.24% > 80.00% for > 30s with reclaim activity",
    )
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps(record), stderr="")
    with (
        patch.object(scopes, "enabled", return_value=True),
        patch.object(scopes, "cgroup", return_value=Path("/sys/fs/cgroup/worker.service")),
        patch.object(wm, "workers_state_dir", return_value=state),
        patch.object(wm, "workers_runtime_dir", return_value=state),
        patch.object(wm, "load_worker_specs", return_value=[wm.WorkerSpec(id="a")]),
        patch.object(activity, "emit"),
        patch.object(scopes.subprocess, "run", return_value=result),
    ):
        assert scopes.collect_oom_events(state / "config", time.time() - 60)
        observation = wm.read_json(state / "diagnostics/isolation.json")
        assert observation["events"][0]["role"] == "ci"
        assert observation["events"][0]["workers"] == []
        result.stderr = "Hint: You are currently not seeing messages from other users and the system."
        assert not scopes.collect_oom_events(state / "config", time.time() - 60)
        assert wm.read_json(state / "diagnostics/isolation.json")["error"] == "OSError"


if (
    sys.platform != "linux"
    or not shutil.which("systemctl")
    or not shutil.which("busctl")
    or run("systemctl", "--user", "show-environment", check=False).returncode
):
    print("worker scopes: native integration skipped (no Linux user manager)")
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix="tauceti-scopes-") as raw:
    root = Path(raw)
    name = "tauceti-isolation-test-" + uuid.uuid4().hex
    unit, slice_name = name + ".service", name + ".slice"
    os.environ.update(
        TAUCETI_WORKERS_STATE_DIR=str(root / "state"),
        TAUCETI_RUNTIME_DIR=str(root / "run"),
        TAUCETI_ACTIVITY_DIR=str(root / "activity"),
        TAUCETI_WORKER_SLICE=slice_name,
    )
    test_command = shlex.join(
        [
            sys.executable,
            "-c",
            "import os,subprocess,pathlib; "
            "child=subprocess.Popen(['sleep','300'], start_new_session=True); "
            "pathlib.Path(os.environ['TAUCETI_RUNTIME_STATUS']+'.grandchild').write_text(str(child.pid)); "
            "os.read(int(os.environ['TAUCETI_PARENT_PIPE_FD']),1)",
        ]
    )
    config = root / "workers.toml"
    specs = [wm.WorkerSpec(id="a"), wm.WorkerSpec(id="b")]
    wm.save_worker_specs(config, specs)
    argv = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--unit=" + unit,
        "--slice=" + slice_name,
        "-p",
        "Restart=on-failure",
        "-p",
        "RestartSec=0.2",
        "-p",
        "TimeoutStopSec=5",
        "--setenv=PYTHONPATH=" + str(Path(__file__).resolve().parent.parent),
        "--setenv=TAUCETI_MANAGER_TEST_COMMAND=" + test_command,
        "--setenv=TAUCETI_GITHUB_STATE_URL=",
    ]
    for key in ("TAUCETI_WORKERS_STATE_DIR", "TAUCETI_RUNTIME_DIR", "TAUCETI_ACTIVITY_DIR", scopes.SLICE_ENV):
        argv += ["--setenv=" + key + "=" + os.environ[key]]
    argv += [
        sys.executable,
        "-m",
        "tauceti_worker",
        "workers",
        "--config",
        str(config),
        "manager",
        "--interval",
        "0.05",
    ]
    try:
        run(*argv)

        def ready(wid):
            s = wm.runner_status(wid)
            return s if s.get("alive") and s.get("scope_unit") and s.get("child_pid") else None

        a, b = wait(lambda: ready("a")), wait(lambda: ready("b"))
        manager = wm.manager_request("ping")["pid"]
        assert scopes.cgroup(a["wrapper_pid"]).name == a["scope_unit"]
        pool = scopes.Scopes(root / "run")
        pool.root = scopes.cgroup(a["wrapper_pid"]).parent
        pool.cleanup("a")  # A live wrapper's lock excludes it from orphan cleanup.
        assert ready("a")["wrapper_pid"] == a["wrapper_pid"]
        grandchild = wait(lambda: grandchild_pid("a"))
        run("systemctl", "--user", "kill", "--signal=KILL", a["scope_unit"])
        a2 = wait(lambda: (s := ready("a")) and s["wrapper_pid"] != a["wrapper_pid"] and s)
        assert ready("b")["wrapper_pid"] == b["wrapper_pid"]
        assert wm.manager_request("ping")["pid"] == manager
        wait(lambda: gone(grandchild))

        # Both a crash restart and an ordinary service restart must preserve work.
        run("systemctl", "--user", "kill", "--kill-whom=main", "--signal=KILL", unit)
        wait(lambda: (s := wm.manager_request("ping")) and s["pid"] != manager)
        run("systemctl", "--user", "restart", unit)
        wait(lambda: wm.manager_request("ping"))
        assert ready("a")["wrapper_pid"] == a2["wrapper_pid"]
        assert ready("b")["wrapper_pid"] == b["wrapper_pid"]

        # An adopted wrapper has no parent wait status; its abrupt death still backs off.
        os.kill(a2["wrapper_pid"], signal.SIGKILL)
        wait(lambda: not wm.runner_status("a").get("alive"))
        time.sleep(1)
        assert not wm.runner_status("a").get("alive")
        wait(lambda: ready("a"))
        grandchild = wait(lambda: grandchild_pid("b"))
        wm.save_worker_specs(config, [specs[0], dataclasses.replace(specs[1], enabled=False)])
        wait(lambda: not wm.runner_status("b").get("alive") and gone(grandchild))

        # The slice stop is the primitive used by Fleet update and rollback.
        run("systemctl", "--user", "stop", slice_name)
        wait(lambda: not wm.runner_status("a").get("alive"))
        wait(lambda: not pool.units())

        # Scope setup failure keeps this same wrapper PID running in its old group.
        os.environ[scopes.SLICE_ENV] = "invalid slice"
        os.environ["TAUCETI_MANAGER_TEST_COMMAND"] = shlex.join(
            [
                sys.executable,
                "-c",
                "import os; os.read(int(os.environ['TAUCETI_PARENT_PIPE_FD']),1)",
            ]
        )
        p = wm._launch_runner(wm.WorkerSpec(id="fallback"), wm.workers_state_dir(), wm.workers_runtime_dir())
        try:
            s = wait(lambda: (s := wm.runner_status("fallback")) and s.get("isolation_error") and s)
            assert s["wrapper_pid"] == p.pid and s["alive"] and s["scope_unit"] is None
        finally:
            wm._stop_runner("fallback")
            p.wait(10)
    finally:
        run("systemctl", "--user", "stop", slice_name, check=False)
print("worker scopes: OK")

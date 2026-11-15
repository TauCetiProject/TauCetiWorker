#!/usr/bin/env python3
"""Graceful worker restarts: a restart waits for the round in flight, then runs the new runtime.

The managed fixtures run the real wrapper and the real loop driver; only the round itself is a
stand-in child that records when it starts, finishes, or is killed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def round_child(log: Path, runtime: str, duration: float) -> int:
    """A stand-in round: all-or-nothing, so it either records `finish` or records `killed`."""

    def killed(_signum, _frame):
        with log.open("a") as stream:
            stream.write(f"killed {os.getpid()}\n")
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, killed)
    with log.open("a") as stream:
        stream.write(f"start {runtime} {os.getpid()}\n")
    time.sleep(duration)
    with log.open("a") as stream:
        stream.write(f"finish {os.getpid()}\n")
    return 0


def driver(log: Path, runtime_file: Path, duration_file: Path, idle: str) -> int:
    """The real loop driver. Its runtime and round length are fixed when it starts, as loaded code is."""
    import tauceti_worker.loop as loop
    import tauceti_worker.round as round_lifecycle

    runtime = runtime_file.read_text().strip()
    duration = duration_file.read_text().strip()
    loop.choose_model = lambda *_args, **_kwargs: ("codex", {})
    loop.github_budget = lambda: {}
    if idle == "idle":
        # Waiting for fleet capacity: the loop sleeps between rounds and never starts one.
        loop.fleet_admission_reason = lambda _wid: "fixture capacity wait"
    round_lifecycle.spawn_round = lambda _tail, **_kwargs: subprocess.Popen(
        [sys.executable, __file__, "round", str(log), runtime, duration],
        start_new_session=True,
    )
    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
    cfg = SimpleNamespace(wid=os.environ.get("TAUCETI_WORKER_ID", "graceful"))
    return loop.cmd_loop(args, cfg, only=[], agent="codex")


if len(sys.argv) > 1 and sys.argv[1] == "round":
    raise SystemExit(round_child(Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])))
if len(sys.argv) > 1 and sys.argv[1] == "driver":
    raise SystemExit(driver(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]))


def wait_for(predicate, timeout: float = 20, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def events(log: Path) -> list[list[str]]:
    try:
        return [line.split() for line in log.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


root = Path(tempfile.mkdtemp(prefix="tauceti-graceful-restart-"))
for key in ("TAUCETI_CONFIG_HOME", "TAUCETI_WORKERS_CONFIG", "TAUCETI_WORKER_SLICE"):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["TAUCETI_WORKERS_STATE_DIR"] = str(root / "state" / "workers")
os.environ["TAUCETI_RUNTIME_DIR"] = str(root / "run")
os.environ["TAUCETI_INTERROUND"] = "1"
os.environ["TAUCETI_GITHUB_STATE_URL"] = ""
os.environ["TAUCETI_BACKGROUND_CLEANUP"] = "off"

log = root / "rounds.log"
runtime_file = root / "runtime"
duration_file = root / "duration"
mode_file = root / "mode"


def set_command(mode: str) -> None:
    os.environ["TAUCETI_MANAGER_TEST_COMMAND"] = " ".join(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "driver",
            str(log),
            str(runtime_file),
            str(duration_file),
            mode,
        ]
    )


import tauceti_worker.worker_manager as wm  # noqa: E402
from tauceti_worker.cli import build_parser  # noqa: E402

manager = None
try:
    # 1. A loop between rounds exits promptly on the drain signal, without starting a round.
    runtime_file.write_text("v0\n")
    duration_file.write_text("30\n")
    idle = subprocess.Popen(
        [sys.executable, __file__, "driver", str(log), str(runtime_file), str(duration_file), "idle"],
        env={**os.environ, "TAUCETI_WORKER_ID": "idle"},
    )
    time.sleep(1.5)  # the handler is installed and the loop is sleeping on fleet capacity
    began = time.monotonic()
    idle.send_signal(signal.SIGUSR1)
    rc = idle.wait(10)
    assert rc == 0, f"idle loop exited {rc}, expected 0"
    assert time.monotonic() - began < 3, "an idle loop waited instead of exiting promptly"
    assert events(log) == [], f"an idle loop started a round: {events(log)}"
    print("[OK ] an idle loop exits promptly on a graceful restart")

    # 2. The parser accepts the restart options and rejects a negative deadline.
    parsed = build_parser().parse_args(["workers", "restart", "w", "--timeout", "2.5", "--no-wait"])
    assert parsed.timeout == 2.5 and parsed.no_wait is True
    parsed = build_parser().parse_args(["workers", "restart", "w"])
    assert parsed.timeout is None and parsed.no_wait is False
    try:
        build_parser().parse_args(["workers", "restart", "w", "--timeout", "-1"])
    except SystemExit:
        pass
    else:
        raise AssertionError("a negative --timeout was accepted")

    # 3. Through the manager, wrapper and loop: a restart requested mid-round lets that round
    #    finish, then starts the new runtime.
    set_command("busy")
    runtime_file.write_text("v1\n")
    duration_file.write_text("3\n")
    config = wm.default_workers_config()
    assert config.is_relative_to(root), f"test configuration escaped its temporary root: {config}"
    wm.save_worker_specs(config, [wm.WorkerSpec(id="graceful")])
    manager = subprocess.Popen(
        [sys.executable, "-m", "tauceti_worker", "workers", "--config", str(config), "manager", "--interval", "0.1"],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for(lambda: (wm.manager_request("ping") or {}).get("graceful_restart"), what="manager")
    first = wait_for(lambda: next((e for e in events(log) if e[:2] == ["start", "v1"]), None), what="round 1")
    old_instance = wm.runner_status("graceful").get("instance")
    assert old_instance
    runtime_file.write_text("v2\n")
    began = time.monotonic()
    wm.restart_worker(config, "graceful", wait=True)
    waited = time.monotonic() - began
    new = wm.runner_status("graceful")
    assert new.get("alive") and new.get("instance") != old_instance
    seen = events(log)
    assert ["finish", first[2]] in seen, f"the round in flight did not finish: {seen}"
    assert not any(e[0] == "killed" for e in seen), f"a graceful restart killed a round: {seen}"
    assert waited >= 1, f"the restart did not wait for the round ({waited:.1f}s)"
    v1_rounds = [e for e in seen if e[:2] == ["start", "v1"]]
    assert len(v1_rounds) == 1, f"the old runtime started another round after the restart request: {seen}"
    wait_for(lambda: any(e[:2] == ["start", "v2"] for e in events(log)), what="a round on the new runtime")
    print("[OK ] a restart requested mid-round lets it finish, then runs the new runtime")

    # 4. --timeout: a round still running at the deadline is stopped (and discarded) as before.
    duration_file.write_text("600\n")
    runtime_file.write_text("v3\n")
    wm.restart_worker(config, "graceful", wait=True)  # waits out a 3s v2 round; v3 rounds are long
    long_round = wait_for(lambda: next((e for e in events(log) if e[:2] == ["start", "v3"]), None), what="round 3")
    v3_instance = wm.runner_status("graceful").get("instance")
    runtime_file.write_text("v4\n")
    began = time.monotonic()
    restarted = threading.Event()

    def restart_with_deadline() -> None:
        wm.restart_worker(config, "graceful", timeout=1, wait=True)
        restarted.set()

    thread = threading.Thread(target=restart_with_deadline, daemon=True)
    thread.start()
    time.sleep(0.5)
    assert ["killed", long_round[2]] not in events(log), "the deadline was not honoured"
    status = wm.worker_snapshots(config=config)[0]
    assert status.get("drain_requested_at"), "a pending graceful restart is not visible in status"
    lines = "\n".join(wm._worker_status_lines(config, [status], True, width=100))
    assert "after the current round" in lines, lines
    wait_for(restarted.is_set, timeout=60, what="the deadline restart")
    waited = time.monotonic() - began
    assert waited < 40, f"--timeout 1 took {waited:.1f}s"
    assert wm.runner_status("graceful").get("instance") not in (None, v3_instance)
    wait_for(lambda: ["killed", long_round[2]] in events(log), what="the unfinished round to be killed")
    assert ["finish", long_round[2]] not in events(log)
    wait_for(lambda: any(e[:2] == ["start", "v4"] for e in events(log)), what="a round on the newest runtime")
    print("[OK ] --timeout stops an unfinished round after the deadline, then restarts")

    # 5. Disabling still stops a worker at once rather than waiting for its round.
    began = time.monotonic()
    wm.set_worker_enabled(config, "graceful", False)
    wait_for(lambda: not wm.runner_status("graceful").get("alive"), timeout=40, what="disable to stop the worker")
    assert time.monotonic() - began < 30
    print("[OK ] disable still stops the worker immediately")
    print("graceful restart: OK")
finally:
    if manager is not None and manager.poll() is None:
        wm.manager_request("shutdown", stop_workers=True)
        try:
            manager.wait(40)
        except subprocess.TimeoutExpired:
            manager.terminate()
            manager.wait(5)
    if (root / "run" / "w-graceful.sock").exists():
        wm._stop_runner("graceful")
    shutil.rmtree(root, ignore_errors=True)

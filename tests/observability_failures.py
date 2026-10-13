"""Inject reporting IO failures while native work and supervision remain productive."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import claude_api_client as client
from tauceti_worker import config
from tauceti_worker import github_transport as tr
from tauceti_worker import runtime_status as rs
from tauceti_worker import worker_manager as wm
from tauceti_worker.budget import Budget


class ObservabilityTests(unittest.TestCase):
    def test_receipt_failure_preserves_native_result_and_reservation(self):
        for native_rc in (0, 1):
            for settlement_available in (True, False):
                with self.subTest(native_rc=native_rc, settlement_available=settlement_available):
                    with tempfile.TemporaryDirectory() as raw:
                        root = Path(raw)
                        bridge = root / "bridge"
                        (bridge / "inbox").mkdir(parents=True)
                        budget = Budget(root / "budget")
                        budget.configure(grant=100)
                        invocation = None

                        def request(
                            directory, action, *, budget=budget, settlement_available=settlement_available, **fields
                        ):
                            nonlocal invocation
                            if action == "admit":
                                invocation = fields["id"]
                                answer = budget.admit(invocation, "test", "opus", "fix")
                                return {**answer, "id": invocation}
                            if action == "settle":
                                if not settlement_available:
                                    raise OSError("settlement storage unavailable")
                                budget.settle_receipt(fields)
                                return {"ok": True}
                            raise AssertionError(action)

                        (root / "config.json").write_text(
                            json.dumps({"key": "fake", "bridge": str(bridge), "command": ["fake-claude"]})
                        )
                        native = SimpleNamespace(
                            stdout=io.StringIO(
                                json.dumps(
                                    {
                                        "type": "result",
                                        "session_id": "test-session",
                                        "total_cost_usd": 10,
                                        "result": "done",
                                    }
                                )
                                + "\n"
                            ),
                            wait=lambda native_rc=native_rc: native_rc,
                        )
                        output = io.StringIO()
                        broken_stderr = io.StringIO()
                        broken_stderr.close()
                        with (
                            patch.object(client, "__file__", str(root / "shim.py")),
                            patch.object(
                                sys,
                                "argv",
                                ["claude", "-p", "hello", "--model", "opus", "--output-format", "stream-json"],
                            ),
                            patch.object(client, "api_environment", return_value={}),
                            patch.object(client, "authenticate"),
                            patch.object(client, "request", request),
                            patch.object(client, "atomic", side_effect=OSError("receipt storage unavailable")),
                            patch.object(client.subprocess, "Popen", return_value=native),
                            contextlib.redirect_stdout(output),
                            contextlib.redirect_stderr(broken_stderr),
                        ):
                            self.assertEqual(client.main(), native_rc)
                        self.assertIn('"result": "done"', output.getvalue())
                        snapshot = budget.snapshot()
                        if settlement_available:
                            self.assertEqual(Decimal(snapshot["spent"]), 10)
                        else:
                            # Missing logging cannot silently erase costs or consume grants again.
                            self.assertGreater(Decimal(snapshot["pending"]), 0)
                            budget.settle(invocation, None, "missing receipt")
                            self.assertEqual(budget.snapshot()["unresolved"], [invocation])
                            self.assertTrue(budget.admit("peer", "peer", "opus", "fix")["allowed"])

    def test_status_lock_contention_does_not_stall_native_work(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            rs.update_status(path, state="running")
            with path.with_suffix(".json.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch.dict(os.environ, {rs.STATUS_ENV: str(path)}):
                    started = time.monotonic()
                    rs.report_runtime("surveying")
                    self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(rs.read_json(path)["state"], "running")
            with patch.dict(os.environ, {rs.STATUS_ENV: str(path)}):
                rs.report_runtime(phase="fix")
            self.assertEqual(rs.read_json(path)["state"], "surveying")

    def test_status_publisher_preserves_identity_then_flushes_after_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            rs.update_status(path, spec_hash="old", state="old")
            publisher = rs.StatusPublisher(path)
            with patch.object(rs, "atomic_json", side_effect=OSError("status disk unavailable")):
                publisher.update(spec_hash="new", wrapper_pid=123, state="starting")
                publisher.update(child_pid=456)
                self.assertEqual(publisher.snapshot()["spec_hash"], "new")
                self.assertEqual(publisher.snapshot()["child_pid"], 456)
            publisher.update(heartbeat_at=100)
            self.assertEqual(rs.read_json(path)["spec_hash"], "new")
            rs.update_status(path, state="running", phase="review")
            publisher.update(heartbeat_at=200)
            self.assertEqual(publisher.snapshot()["state"], "running")
            self.assertEqual(publisher.snapshot()["phase"], "review")

    def test_metrics_lock_cache_and_proxy_failures_do_not_block_requests(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": raw}):
            with (Path(raw) / "accounting.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                started = time.monotonic()
                tr.event({"kind": "test"})
                self.assertLess(time.monotonic() - started, 0.5)
            with patch.object(tr, "atomic", side_effect=OSError("cache unavailable")):
                tr.register_principal(123, "test", {"GH_TOKEN": "fake"})
            with patch.object(tr, "_install_proxy", side_effect=OSError("shim unavailable")):
                tr.install_proxy()

    def test_console_failure_does_not_prevent_file_logging(self):
        broken = io.StringIO()
        broken.close()
        logfile = io.StringIO()
        with contextlib.redirect_stderr(broken), patch.object(config, "_LOG_FH", logfile):
            config.log("native work continues")
        self.assertIn("native work continues", logfile.getvalue())

    def test_receipt_write_can_recover_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            receipt = Path(raw) / "session.receipt"
            with patch.object(client.os, "fsync", side_effect=OSError("transient disk error")):
                with self.assertRaises(OSError):
                    client.atomic(receipt, {"cost": 10})
            self.assertFalse(receipt.with_suffix(".tmp").exists())
            client.atomic(receipt, {"cost": 20})
            self.assertEqual(json.loads(receipt.read_text()), {"cost": 20})

    def test_proxy_launcher_fallback_preserves_publication_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "bin").write_text("broken optional launcher directory")
            with patch.dict(os.environ, {"TAUCETI_GITHUB_SHARED_DIR": raw, "TAUCETI_REAL_GH": "/bin/true"}):
                tr.install_proxy()
                self.assertEqual(tr.root(), root)
                self.assertTrue((Path(os.environ["PATH"].split(os.pathsep)[0]) / "gh").exists())

    def test_native_exit_status_survives_broken_output_in_all_formats(self):
        class BrokenOutput(io.StringIO):
            def write(self, value):
                raise OSError("output pipe closed")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.json").write_text(json.dumps({"key": "fake", "bridge": raw, "command": ["fake"]}))
            for output_format in ("text", "json", "stream-json"):
                native = SimpleNamespace(
                    stdout=io.StringIO(json.dumps({"type": "result", "result": "done", "total_cost_usd": 1}) + "\n"),
                    wait=lambda: 1,
                )
                with (
                    patch.object(client, "__file__", str(root / "shim.py")),
                    patch.object(sys, "argv", ["claude", "-p", "hello", "--output-format", output_format]),
                    patch.object(client, "api_environment", return_value={}),
                    patch.object(client, "authenticate"),
                    patch.object(client, "atomic"),
                    patch.object(client, "request", return_value={"allowed": True, "id": "test"}),
                    patch.object(client.subprocess, "Popen", return_value=native),
                    contextlib.redirect_stdout(BrokenOutput()),
                ):
                    self.assertEqual(client.main(), 1)

    def test_manager_honors_restart_policy_without_terminal_status_file(self):
        for policy in ("never", "on-failure", "always"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                state, runtime = root / "state", root / "runtime"
                marker = root / "launches"
                child = root / "child.py"
                child.write_text("import pathlib, sys\nwith pathlib.Path(sys.argv[1]).open('a') as f: f.write('x')\n")
                configuration = root / "workers.toml"
                wm.save_worker_specs(
                    configuration, [wm.WorkerSpec.from_dict({"id": "policy", "enabled": True, "restart": policy}, 0)]
                )
                harness = root / "manager.py"
                harness.write_text("""import pathlib, subprocess, sys, time
from tauceti_worker import worker_manager as wm
wm.workers_runtime_dir = lambda: pathlib.Path(sys.argv[2])
wm.workers_state_dir = lambda: pathlib.Path(sys.argv[3])
wm._launch_runner = lambda *args: subprocess.Popen([sys.executable, sys.argv[4], sys.argv[5]])
# Make a former logging-dependent launch backoff elapse within a test iteration.
time.time = lambda: time.monotonic() * 1000
raise SystemExit(wm.run_manager(pathlib.Path(sys.argv[1]), interval=0.05))
""")
                manager = subprocess.Popen(
                    [
                        sys.executable,
                        str(harness),
                        str(configuration),
                        str(runtime),
                        str(state),
                        str(child),
                        str(marker),
                    ],
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and manager.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(marker.exists())
                    time.sleep(0.6)
                    count = len(marker.read_text())
                    self.assertEqual(count, 1) if policy != "always" else self.assertGreater(count, 1)
                finally:
                    wm._socket_request(wm.manager_socket(runtime), {"action": "shutdown", "stop_workers": True})
                    try:
                        manager.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        manager.kill()
                        manager.wait()
                    manager.communicate()

    def test_supervisor_survives_status_and_log_storage_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state, runtime = root / "state", root / "runtime"
            state.mkdir()
            (state / "logs").write_text("cannot create log subdirectory")
            marker = root / "child-started"
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text('started')\ntime.sleep(60)\n"
            )
            spec = wm.WorkerSpec.from_dict({"id": "fault-test", "enabled": True, "agent": "claude"}, 0)
            harness = root / "runner.py"
            harness.write_text("""import sys
from types import SimpleNamespace
from tauceti_worker import worker_manager as wm, runtime_status as rs
def fail(*args, **kwargs):
    raise OSError('status storage unavailable')
rs.atomic_json = fail
raise SystemExit(wm.cmd_managed_runner(SimpleNamespace(spec=sys.argv[1], state_dir=sys.argv[2], runtime_dir=sys.argv[3])))
""")
            environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "TAUCETI_MANAGER_TEST_COMMAND": shlex.join([sys.executable, str(child), str(marker)]),
            }
            runner = subprocess.Popen(
                [sys.executable, str(harness), wm._encode_spec(spec), str(state), str(runtime)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and runner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "child failed to launch under logging failure")
                self.assertIsNone(runner.poll(), "supervisor killed useful work after status-write failure")
                live = wm.runner_status(spec.id, state, runtime)
                self.assertTrue(live["alive"])
                self.assertEqual(live["spec_hash"], spec.fingerprint())
                self.assertEqual(live["wrapper_pid"], runner.pid)
                self.assertGreater(live["child_pid"], 0)
                self.assertTrue(wm._stop_runner(spec.id, runtime))
                self.assertEqual(runner.wait(timeout=5), 0)
            finally:
                if runner.poll() is None:
                    wm._stop_runner(spec.id, runtime)
                    try:
                        runner.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        runner.kill()
                        runner.wait()
                runner.communicate()


if __name__ == "__main__":
    unittest.main()

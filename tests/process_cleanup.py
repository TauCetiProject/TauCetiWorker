#!/usr/bin/env python3
"""Host execution ignores old containment state and cleans only its owned groups."""

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, processes


def running(pid):
    if sys.platform == "linux":
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        markers = self.root / "activity/containment-unconfirmed"
        markers.mkdir(parents=True)
        (markers / "broken.json").write_text("{broken")
        (markers / "old.json").write_text('{"boundary": "old", "pid_namespace": 42}')
        (self.root / "quiescence.json").write_text("not json")
        self.probe = self.root / "namespace-probed"
        bwrap = self.root / "bwrap"
        bwrap.write_text(f"#!/bin/sh\ntouch '{self.probe}'\nexit 93\n")
        bwrap.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "TAUCETI_ACTIVITY_DIR": str(markers.parent),
            "TAUCETI_PROCESS_CONTAINMENT": "required",
            "TAUCETI_SHARED_MATHLIB_BUILDS": "off",
        }
        self.pidfile = self.root / "tool.pid"
        self.addCleanup(self.cleanup_tool)

    def cleanup_tool(self):
        if self.pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(self.pidfile.read_text()), signal.SIGKILL)

    def command(self, *, hang=False):
        # The background child remains in its parent's group and ignores TERM so
        # successful cleanup has to reach KILL. It does not inherit captured pipes.
        code = (
            "import os,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
        )
        parent = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"child=subprocess.Popen([sys.executable,'-c',{code!r},{str(self.pidfile)!r}], "
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"p=Path({str(self.pidfile)!r}); "
            "exec('while not p.exists(): time.sleep(.01)'); "
            "print('ready',flush=True); " + ("time.sleep(60)" if hang else "sys.exit(0)")
        )
        return [sys.executable, "-c", parent]

    def assert_cleaned(self):
        self.assertTrue(self.pidfile.exists())
        pid = int(self.pidfile.read_text())
        deadline = time.monotonic() + 2
        while running(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(running(pid))
        self.assertFalse(self.probe.exists(), "host execution probed PID namespaces")
        self.assertEqual((self.root / "quiescence.json").read_text(), "not json")
        self.assertEqual((self.root / "activity/containment-unconfirmed/broken.json").read_text(), "{broken")

    def test_tool_normal_exit_sweeps_owned_children_and_leaves_other_work_running(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as unrelated:
            try:
                result = processes.run(self.command(), env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "ready")
                self.assert_cleaned()
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_tool_timeout_retains_output_and_cleans_owned_children(self):
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            processes.run(self.command(hang=True), env=self.env, capture_output=True, timeout=1)
        self.assertIn(b"ready", error.exception.output)
        self.assert_cleaned()

    def test_host_agent_needs_no_namespace_or_history_scan(self):
        profile = agents.AuthoringProfile("claude", "fixture", None, "fixture", "fixture")
        real_glob = Path.glob

        def glob(path, pattern):
            if path == self.root / "activity/containment-unconfirmed":
                self.fail("worker startup scanned old cleanup markers")
            return real_glob(path, pattern)

        with (
            patch.dict(os.environ, self.env),
            patch.object(agents, "host_agent_argv", return_value=(self.command(), self.env)),
            patch.object(agents, "host_build_cache_context", return_value=contextlib.nullcontext([])),
            patch.object(Path, "glob", glob),
            patch("tauceti_worker.github_transport.execute", side_effect=AssertionError("unexpected GitHub read")),
        ):
            self.assertEqual(agents.run_agent_host(self.root, "fixture", profile, self.root / "logs"), 0)
        self.assert_cleaned()

    def test_claim_cancellation_cleans_owned_agent_group(self):
        event = threading.Event()

        def cancel_when_started():
            deadline = time.monotonic() + 10
            while not self.pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            event.set()

        watcher = threading.Thread(target=cancel_when_started)
        watcher.start()
        try:
            rc = agents.run_agent_proc(
                self.command(hang=True),
                env=self.env,
                cwd=self.root,
                logdir=self.root / "logs",
                label="fixture",
                provider="test",
                cancel_event=event,
            )
        finally:
            watcher.join(timeout=10)
        self.assertNotEqual(rc, 0)
        self.assert_cleaned()


if __name__ == "__main__":
    unittest.main()

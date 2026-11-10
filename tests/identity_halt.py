#!/usr/bin/env python3
"""A worker bound to a GitHub account halts for good on an identity no retry fixes.

All GitHub calls hit a temporary fake gh executable; no real credentials or network are used.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker import cli, github, loop
from tauceti_worker import worker_manager as wm
from tauceti_worker.config import Die, Halted
from tauceti_worker.constants import EX_HALTED

# TEST_GH_MODE names the login /user answers with, or "<status> <message>" for a refusal, which is
# printed the way gh prints it under GH_DEBUG=api: a request trace, then its one-line error.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["TEST_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if args[:2] == ["auth", "token"]:
    print("test-alice")
    sys.exit(0)
mode = open(os.environ["TEST_GH_MODE"]).read().strip()
if " " not in mode:
    print(json.dumps({"login": mode, "id": 11}))
    sys.exit(0)
status, message = mode.split(" ", 1)
sys.stderr.write(
    "* Request at 2026-01-01 00:00:00\\n* Request to https://api.github.com/user\\n> GET /user HTTP/1.1\\n"
    f"< HTTP/2.0 {status}\\n* Request took 5ms\\ngh: {message} (HTTP {status})\\n"
)
print(json.dumps({"message": message}))
sys.exit(1)
"""

BAD_CREDENTIALS = "401 Bad credentials"
SUSPENDED = "403 Sorry. Your account was suspended."
NOT_HALTS = (
    "403 You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
    "403 API rate limit exceeded for user ID 11.",
    "403 Resource not accessible by personal access token",
    "403 Resource protected by organization SAML enforcement. You must grant your OAuth token access.",
    "403 Repository access blocked",
    "404 Not Found",
    "503 Service Unavailable",
)


class IdentityHaltTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="tauceti-halt-")))
        fake = self.root / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(0o700)
        self.calls = self.root / "gh.jsonl"
        self.mode = self.root / "mode"
        self.halt = self.root / "state" / "w1" / "halt.json"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root),
                    "PATH": f"{self.root}:{os.environ['PATH']}",
                    "PYTHONPATH": str(REPO),
                    "TEST_GH_LOG": str(self.calls),
                    "TEST_GH_MODE": str(self.mode),
                    "TAUCETI_CONFIG_HOME": str(self.root / "config"),
                    "TAUCETI_LEGACY_DATA_ROOT": str(self.root),
                },
                clear=True,
            )
        )
        # Only github.py's own waits are recorded; the rest of the process keeps the real clock.
        self.naps = []
        clock = SimpleNamespace(time=time.time, sleep=self.naps.append)
        self.enterContext(patch.object(github, "time", clock))
        self.answer("alice")

    def answer(self, mode):
        """Set /user's answer, with no quota memory left over from the previous answer."""
        self.mode.write_text(mode)
        os.environ["TAUCETI_GITHUB_SHARED_DIR"] = tempfile.mkdtemp(dir=self.root)

    def requests(self):
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return sum(json.loads(line)[:1] == ["api"] for line in lines)

    def halted(self, **kwargs):
        with self.assertRaises(Halted) as raised:
            github.pin_github_account("alice", **kwargs)
        return raised.exception.reason

    def test_only_an_account_verdict_is_a_halt(self):
        def verdict(stderr, status=None):
            records = [{"status": status}] if status else []
            return github._identity_halt(SimpleNamespace(stdout="", stderr=stderr, github_records=records))

        self.assertEqual(verdict("gh: Bad credentials (HTTP 401)"), "invalid-credentials")
        self.assertEqual(verdict("gh: Requires authentication", 401), "invalid-credentials")
        self.assertEqual(verdict("gh: Sorry. Your account was suspended. (HTTP 403)"), "account-halt")
        self.assertEqual(verdict("gh: Your account is locked.", 403), "account-halt")
        self.assertIsNone(verdict("gh: Your account was suspended. (HTTP 404)"))
        for mode in NOT_HALTS:
            status, message = mode.split(" ", 1)
            self.assertIsNone(verdict(f"gh: {message} (HTTP {status})", int(status)), mode)

    def test_start_halts_on_the_first_answer(self):
        for mode, reason in (
            (BAD_CREDENTIALS, "invalid-credentials"),
            (SUSPENDED, "account-halt"),
            ("bob", "identity-mismatch"),
        ):
            before = self.requests()
            self.answer(mode)
            self.assertEqual(self.halted(), reason)
            self.assertEqual(self.requests(), before + 1)
        self.assertEqual(self.naps, [])

    def test_other_refusals_and_outages_stay_ordinary_failures(self):
        for mode in NOT_HALTS:
            self.answer(mode)
            with self.assertRaises(Die) as raised:
                github.pin_github_account("alice")
            self.assertNotIsInstance(raised.exception, Halted, mode)

    def test_each_round_asks_once_and_survives_an_unanswered_recheck(self):
        self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
        self.assertEqual(self.requests(), 1)
        # A round child inherits the pinned credential. Other commands keep the hourly reuse.
        self.assertEqual(github.pin_github_account("alice"), "alice")
        self.assertEqual(self.requests(), 1)
        self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
        self.assertEqual(self.requests(), 2)

        for sent, mode in enumerate(("503 Service Unavailable", "404 Not Found", NOT_HALTS[0]), 3):
            self.mode.write_text(mode)
            self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
            self.assertEqual(self.requests(), sent)
        # The secondary limit above left a cooldown: the next recheck is answered without a request.
        self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
        self.assertEqual(self.requests(), 5)
        with patch.object(github.transport, "execute", side_effect=subprocess.TimeoutExpired("gh", 30)):
            self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
        self.assertEqual(self.naps, [])

        for mode, reason in ((SUSPENDED, "account-halt"), (BAD_CREDENTIALS, "invalid-credentials")):
            self.answer(mode)
            self.assertEqual(self.halted(recheck=True), reason)

        # A different credential was never verified by this loop, so its outage is not survivable.
        self.answer("503 Service Unavailable")
        os.environ["GH_TOKEN"] = "test-other"
        with self.assertRaises(Die) as raised:
            github.pin_github_account("alice", recheck=True)
        self.assertNotIsInstance(raised.exception, Halted)

    def run_cli(self, *argv):
        with (
            patch.object(sys, "argv", ["tauceti", *argv]),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(cli, "cmd_work", return_value=0) as work,
        ):
            return cli.cli_main(), work.called

    def test_halt_is_recorded_published_and_refused_until_cleared(self):
        status = self.root / "status.json"
        os.environ["TAUCETI_RUNTIME_STATUS"] = str(status)
        round_argv = ("_round", "--worker-id", "w1", "--github-account", "alice")
        self.answer("bob")
        self.assertEqual(self.run_cli(*round_argv), (EX_HALTED, False))
        record = json.loads(self.halt.read_text())
        self.assertEqual(record["reason"], "identity-mismatch")
        self.assertIn("belongs to bob", record["detail"])
        published = json.loads(status.read_text())
        self.assertEqual(
            (published["state"], published["halt_reason"], published["failure_code"]),
            ("halted", "identity-mismatch", EX_HALTED),
        )
        self.assertIn("belongs to bob", published["failure_reason"])

        # A repaired credential does not lift the halt, and the refusal reaches neither gh nor GitHub.
        self.answer("alice")
        self.calls.unlink()
        for argv in (round_argv, ("work", "--loop", *round_argv[1:])):
            self.assertEqual(self.run_cli(*argv), (EX_HALTED, False))
        self.assertFalse(self.calls.exists())
        self.assertIn("--worker-id w1 --clear-halt", json.loads(status.read_text())["failure_reason"])

        # Without a configured account the file is inert.
        self.assertEqual(self.run_cli("_round", "--worker-id", "w1"), (0, True))

        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            self.assertEqual(self.run_cli("work", "--worker-id", "w1", "--clear-halt"), (0, False))
        self.assertIn("identity-mismatch", shown.getvalue())
        self.assertFalse(self.halt.exists())
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.run_cli(*round_argv), (0, True))
        del os.environ["TAUCETI_WORKER_ID"]
        self.assertEqual(self.run_cli("work", "--clear-halt"), (1, False))

    def test_other_commands_fail_as_before_and_record_nothing(self):
        self.answer("bob")
        os.environ["TAUCETI_WORKER_ID"] = "w1"
        with self.assertRaises(Die) as raised:
            cli.main(["status", "--github-account", "alice"])
        self.assertNotIsInstance(raised.exception, Halted)
        self.assertIn("belongs to bob", str(raised.exception))
        self.assertFalse(self.halt.exists())

    def test_loop_stops_without_backoff(self):
        args = cli.build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--loop"])
        states = []
        with (
            patch.dict(os.environ, {"TAUCETI_USE_BUDGET": "0", "TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid"}),
            patch.object(loop, "api_mode", return_value=True),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", return_value=EX_HALTED) as rounds,
            patch.object(loop, "report_runtime", side_effect=lambda state=None, **_: states.append(state)),
            patch.object(loop.time, "sleep") as pause,
        ):
            self.assertEqual(loop.cmd_loop(args, SimpleNamespace(wid="w1"), only=["fix"], agent="claude"), EX_HALTED)
        rounds.assert_called_once()
        pause.assert_not_called()
        self.assertNotIn("backoff", states)

    def test_manager_leaves_a_halted_worker_stopped(self):
        config, state, runtime = self.root / "workers.toml", self.root / "workers", self.root / "run"
        runtime.mkdir(mode=0o700)
        wm.save_worker_specs(config, [wm.WorkerSpec(id="w1")])
        manager = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tauceti_worker",
                "workers",
                "--config",
                str(config),
                "manager",
                "--interval",
                "0.05",
            ],
            cwd=REPO,
            env={
                **os.environ,
                "TAUCETI_WORKERS_STATE_DIR": str(state),
                "TAUCETI_RUNTIME_DIR": str(runtime),
                "TAUCETI_MANAGER_TEST_COMMAND": f"{sys.executable} -c 'raise SystemExit({EX_HALTED})'",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def halted_wrapper(other_than=None):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                seen = wm.runner_status("w1", state, runtime)
                if seen.get("state") == "halted" and seen.get("wrapper_pid") != other_than:
                    return seen
                time.sleep(0.05)
            self.fail("the worker never reached the halted state")

        try:
            first = halted_wrapper()
            self.assertEqual(first["exit_code"], EX_HALTED)
            time.sleep(2)  # forty reconciliations of a restart = "always" worker
            self.assertEqual(wm.runner_status("w1", state, runtime)["wrapper_pid"], first["wrapper_pid"])
            self.assertTrue(wm._socket_request(runtime / "manager.sock", {"action": "restart", "id": "w1"}))
            self.assertEqual(halted_wrapper(other_than=first["wrapper_pid"])["restart_count"], 1)
        finally:
            wm._socket_request(runtime / "manager.sock", {"action": "shutdown", "stop_workers": True})
            try:
                manager.wait(15)
            except subprocess.TimeoutExpired:
                manager.kill()
                manager.wait(5)

        item = {**first, "id": "w1", "actual": "halted", "desired": "running", "failure_reason": "belongs to bob"}
        lines = "\n".join(wm._worker_status_lines(config, [item], True, width=120))
        self.assertIn("w1 — halted", lines)
        self.assertIn("reason:   belongs to bob", lines)
        self.assertIn("tauceti work --worker-id w1 --clear-halt, then tauceti workers restart w1", lines)


if __name__ == "__main__":
    unittest.main()

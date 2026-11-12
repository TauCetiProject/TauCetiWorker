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
import urllib.error
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
            (BAD_CREDENTIALS, "github:invalid-credentials"),
            (SUSPENDED, "github:account-halt"),
            ("bob", "github:identity-mismatch"),
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

        for mode, reason in ((SUSPENDED, "github:account-halt"), (BAD_CREDENTIALS, "github:invalid-credentials")):
            self.answer(mode)
            self.assertEqual(self.halted(recheck=True), reason)

        # A different credential was never verified by this loop, so its outage is not survivable.
        self.answer("503 Service Unavailable")
        os.environ["GH_TOKEN"] = "test-other"
        with self.assertRaises(Die) as raised:
            github.pin_github_account("alice", recheck=True)
        self.assertNotIsInstance(raised.exception, Halted)

    def test_a_verdict_outranks_a_peer_exhausting_the_shared_quota(self):
        self.assertEqual(github.pin_github_account("alice", recheck=True), "alice")
        real_budget, real_execute = github.transport.observed_budget, github.transport.execute
        no_wait = SimpleNamespace(time=time.time, sleep=lambda nap: self.fail(f"waited {nap}s for the quota reset"))
        self.enterContext(patch.object(github, "time", no_wait))
        cases = ((SUSPENDED, "github:account-halt", True), (BAD_CREDENTIALS, "github:invalid-credentials", True))
        for mode, reason, recheck in (*cases, *((m, r, False) for m, r, _ in cases)):
            if not recheck:  # a fresh `work --loop` start, with nothing verified yet
                for name in ("_TAUCETI_PINNED_GITHUB_ACCOUNT", "GH_TOKEN"):
                    os.environ.pop(name, None)
            self.mode.write_text(mode)
            answered = []

            def budget(env=None, answered=answered):
                return {"core": (0, int(time.time()) + 600)} if answered else real_budget(env)

            def execute(*args, answered=answered, **kwargs):
                result = real_execute(*args, **kwargs)
                answered.append(result)
                return result

            with (
                patch.object(github.transport, "observed_budget", side_effect=budget),
                patch.object(github.transport, "execute", side_effect=execute),
            ):
                self.assertEqual(self.halted(recheck=recheck, wait_for_reset=True), reason)
            self.assertEqual(len(answered), 1)

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
        self.assertEqual((record["reason"], record["forge"]), ("github:identity-mismatch", "github"))
        self.assertIn("belongs to bob", record["detail"])
        published = json.loads(status.read_text())
        self.assertEqual(
            (published["state"], published["halt_reason"], published["failure_code"]),
            ("halted", "github:identity-mismatch", EX_HALTED),
        )
        self.assertIn("belongs to bob", published["failure_reason"])

        # A repaired credential does not lift the halt, and the refusal reaches neither gh nor GitHub.
        self.answer("alice")
        self.calls.unlink()
        for argv in (round_argv, ("work", "--loop", *round_argv[1:])):
            self.assertEqual(self.run_cli(*argv), (EX_HALTED, False))
        self.assertFalse(self.calls.exists())
        self.assertIn("--worker-id w1 --clear-halt", json.loads(status.read_text())["failure_reason"])
        with patch.dict(os.environ, {"TAUCETI_MANAGED": "1"}):
            self.assertEqual(self.run_cli(*round_argv), (EX_HALTED, False))
        self.assertIn("`tauceti workers clear-halt w1`", json.loads(status.read_text())["failure_reason"])

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

    def test_workers_clear_halt_uses_the_workers_own_data_root(self):
        config, data = self.root / "workers.toml", self.root / "worker-data"
        record = data / "state" / "w1" / "halt.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"reason": "invalid-credentials", "detail": "rejected", "at": "then"}))
        wm.save_worker_specs(config, [wm.WorkerSpec(id="w1", env={"TAUCETI_LEGACY_DATA_ROOT": str(data)})])
        # The operator's own data root is self.root; only the worker's definition names `data`.
        shown = subprocess.run(
            [sys.executable, "-m", "tauceti_worker", "workers", "--config", str(config), "clear-halt", "w1"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("invalid-credentials", shown.stdout)
        self.assertFalse(record.exists())
        with self.assertRaises(wm.WorkersError):
            wm.clear_worker_halt(config, "w2")

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
        lines = "\n".join(wm._worker_status_lines(config, [item], True, width=300))
        self.assertIn("w1 — halted", lines)
        self.assertIn("reason:   belongs to bob", lines)
        workers = f"tauceti workers --config {config}"
        self.assertIn(f"{workers} clear-halt w1, then {workers} restart w1", lines)


class FakeForgejo:
    """What /api/v1/user answers through tauceti_tools' real Forgejo client: a user row or an HTTP status."""

    def __init__(self):
        self.answer, self.requests = {"id": 3, "login": "worker"}, []


class _Response:
    def __init__(self, body):
        self.body, self.status, self.reason, self.headers = body, 200, "OK", {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=None):
        return self.body


class ForgejoHaltTests(unittest.TestCase):
    answer = IdentityHaltTests.answer
    requests = IdentityHaltTests.requests
    run_cli = IdentityHaltTests.run_cli

    def setUp(self):
        IdentityHaltTests.setUp(self)
        token = self.root / "forgejo"
        token.mkdir(mode=0o700)
        token = token.resolve()  # The credential reader refuses a symlinked ancestor such as macOS's /tmp.
        (token / "worker-token").write_text("forgejo-test-token")
        (token / "worker-token").chmod(0o600)
        os.environ.update(
            TAUCETI_FORGEJO_ORIGIN="https://forgejo.test",
            TAUCETI_FORGEJO_REPO="one/project",
            TAUCETI_FORGEJO_REPOSITORY_ID="7",
            TAUCETI_FORGEJO_TOKEN_FILE=str(token / "worker-token"),
            TAUCETI_FORGEJO_USER_ID="3",
            TAUCETI_RUNTIME_STATUS=str(self.root / "status.json"),
        )
        self.forgejo = FakeForgejo()
        forgejo = self.forgejo

        class Opener:
            def open(self, request, timeout=None):
                forgejo.requests.append(request.full_url)
                answer = forgejo.answer(request.full_url) if callable(forgejo.answer) else forgejo.answer
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(request.full_url, answer, "refused", {}, None)
                return _Response(json.dumps(answer).encode())

        import tauceti_tools.forge

        self.enterContext(patch.object(tauceti_tools.forge.urllib.request, "build_opener", return_value=Opener()))

    def published(self):
        return json.loads((self.root / "status.json").read_text())

    def test_a_forgejo_only_worker_halts_on_its_forgejo_identity_alone(self):
        round_argv = ("_round", "--worker-id", "w1", "--no-github", "--forgejo")
        for answer, reason in (
            ({"id": 4, "login": "other"}, "forgejo:identity-mismatch"),
            (401, "forgejo:invalid-credentials"),
            (403, "forgejo:account-halt"),
        ):
            self.forgejo.answer, self.forgejo.requests = answer, []
            self.assertEqual(self.run_cli(*round_argv), (EX_HALTED, False))
            self.assertEqual(self.forgejo.requests, ["https://forgejo.test/api/v1/user"])
            record = json.loads(self.halt.read_text())
            self.assertEqual((record["reason"], record["forge"]), (reason, "forgejo"))
            self.assertIn("Forgejo https://forgejo.test", record["detail"])
            published = self.published()
            self.assertEqual((published["state"], published["halt_reason"]), ("halted", reason))
            self.assertIn("Forgejo https://forgejo.test", published["failure_reason"])
            self.halt.unlink()
        # Neither the halts nor the passing check contacted GitHub, which this worker has no account for.
        self.assertFalse(self.calls.exists())

        # The record refuses later starts before either forge is asked.
        self.forgejo.answer = 401
        self.run_cli(*round_argv)
        self.forgejo.answer, self.forgejo.requests = {"id": 3, "login": "worker"}, []
        self.assertEqual(self.run_cli(*round_argv), (EX_HALTED, False))
        self.assertEqual(self.forgejo.requests, [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli("work", "--worker-id", "w1", "--clear-halt"), (0, False))
        self.assertEqual(self.run_cli(*round_argv), (0, True))

    def test_outages_and_unselected_sources_do_not_halt(self):
        for answer in (500, 404):
            self.forgejo.answer = answer
            self.assertEqual(self.run_cli("_round", "--worker-id", "w1", "--forgejo"), (0, True))
        self.forgejo.answer, self.forgejo.requests = 401, []
        self.assertEqual(self.run_cli("_round", "--worker-id", "w1"), (0, True))
        del os.environ["TAUCETI_FORGEJO_USER_ID"]
        self.assertEqual(self.run_cli("_round", "--worker-id", "w1", "--forgejo"), (0, True))
        self.assertEqual(self.forgejo.requests, [])
        self.assertFalse(self.halt.exists())

    def test_both_sources_are_checked_and_either_halts(self):
        argv = ("_round", "--worker-id", "w1", "--forgejo", "--github-account", "alice")
        self.assertEqual(self.run_cli(*argv), (0, True))
        self.assertEqual((self.requests(), len(self.forgejo.requests)), (1, 1))
        self.forgejo.answer = {"id": 4, "login": "other"}
        self.assertEqual(self.run_cli(*argv), (EX_HALTED, False))
        self.assertEqual(json.loads(self.halt.read_text())["reason"], "forgejo:identity-mismatch")

    def serve(self, *users):
        """Answer the repository binding, and /user with each of `users` in turn (the last repeats)."""
        users = list(users)

        def answer(url):
            if url.endswith("/repos/one/project"):
                return {"id": 7, "full_name": "one/project", "archived": False, "permissions": {"push": True}}
            if url.endswith("/user"):
                return users.pop(0) if len(users) > 1 else users[0]
            return 404

        self.forgejo.answer = answer

    def activation(self):
        from tauceti_worker import worker_sources

        os.environ.update(TAUCETI_WORKER_ID="w1", TAUCETI_WORKER_FORGES="forgejo")
        return worker_sources.activate(
            worker_sources.native_source(SimpleNamespace(home=self.root)),
            SimpleNamespace(state=self.root.resolve() / "native"),
        )

    def test_git_presents_the_credential_activation_verified(self):
        from tauceti_worker.source_cli import load_source

        self.serve({"id": 3, "login": "worker"})
        token = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
        bound = self.activation()  # The source, and its API token, exist before the file changes.
        token.write_text("forgejo-other-token")
        with bound:
            fill = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=forgejo.test\n\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertIn("password=forgejo-test-token\n", fill.stdout, fill.stderr)
            self.assertEqual(load_source().api._token, "forgejo-test-token")
            copy = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
        self.assertFalse(copy.exists())

    def test_the_verified_copy_is_readable_under_group_writable_worker_state(self):
        from tauceti_worker import worker_sources
        from tauceti_worker.source_cli import load_source

        self.serve({"id": 3, "login": "worker"})
        shared = self.root.resolve() / "shared"
        shared.mkdir()
        shared.chmod(0o775)
        previous = os.umask(0o002)
        try:
            os.environ.update(TAUCETI_WORKER_ID="w1", TAUCETI_WORKER_FORGES="forgejo")
            source = worker_sources.native_source(SimpleNamespace(home=self.root))
            with worker_sources.activate(source, SimpleNamespace(state=shared / "workers" / "w1")):
                self.assertEqual(load_source().api._token, "forgejo-test-token")
        finally:
            os.umask(previous)

    def test_a_read_only_credential_directory_still_activates(self):
        from tauceti_worker.source_cli import load_source

        self.serve({"id": 3, "login": "worker"})
        token = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
        token.chmod(0o400)
        token.parent.chmod(0o500)
        self.addCleanup(token.parent.chmod, 0o700)
        with self.activation():
            self.assertEqual(load_source().api._token, "forgejo-test-token")
            copy = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
        self.assertFalse(copy.parent.exists())
        self.assertEqual(sorted(path.name for path in token.parent.iterdir()), [token.name])

    def test_the_executing_actor_is_held_to_the_configured_user(self):
        from tauceti_worker.source_cli import load_source
        from tauceti_worker.worker_sources import verify_forgejo_identity

        other = {"id": 4, "login": "other"}
        # An unanswered check, then the credential answers as another user; and a token replaced after a
        # passing check. Either way the round's activation halts before anything runs as user 4.
        for first in (503, {"id": 3, "login": "worker"}):
            self.serve(first, other)
            os.environ["TAUCETI_WORKER_FORGES"] = "forgejo"
            verify_forgejo_identity()
            with self.assertRaises(Halted) as raised, self.activation():
                self.fail("activated as another user")
            self.assertEqual(raised.exception.reason, "forgejo:identity-mismatch")
            self.assertEqual(json.loads(self.halt.read_text())["reason"], "forgejo:identity-mismatch")
            self.assertEqual(os.environ["TAUCETI_FORGEJO_USER_ID"], "3")
            self.halt.unlink()

        # A subprocess of a correctly bound round re-checks the actor its own credential resolves to.
        self.serve({"id": 3, "login": "worker"})
        with self.activation():
            self.assertEqual(load_source().repo, "one/project")
            self.serve(other)
            with self.assertRaisesRegex(ValueError, "differs from the selected worker"):
                load_source()

    def test_shared_github_coordination_uses_the_verified_account(self):
        from tauceti_tools import git_credentials

        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.worker_sources import shared_github_environment

        tokens = []
        with patch.object(
            git_credentials, "scoped_git_environment", side_effect=lambda forge, token, **_: tokens.append(token) or {}
        ):
            github.pin_github_account("alice")
            with shared_github_environment():
                pass
            self.assertEqual(tokens, ["test-alice"])
            os.environ["_TAUCETI_PINNED_GITHUB_ACCOUNT"] = "bob"
            with self.assertRaises(SourceUnavailable), shared_github_environment():
                pass
        auth = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertNotIn(["auth", "token", "--hostname", "github.com"], auth)


if __name__ == "__main__":
    unittest.main()

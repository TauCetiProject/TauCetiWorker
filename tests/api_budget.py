"""Grant accounting and real subprocess coverage using a fake Claude, with no paid calls."""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import agents, loop
from tauceti_worker.budget import Budget, BudgetError
from tauceti_worker.claude_api import ClaudeAPIContext, configure_api
from tauceti_worker.cli import build_parser
from tauceti_worker.worker_manager import WorkersError, WorkerSpec


def competing_admission(directory, invocation):
    return Budget(Path(directory), clock=lambda: 1000).admit(invocation, invocation, "opus", "fix")["allowed"]


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.now = 1000
        self.budget = Budget(self.directory, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_grants_set_rate_and_readonly_projection(self):
        self.budget.configure(grant="100", rate="20")
        original = self.budget.ledger.read_bytes()
        self.now += 1800
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 110)
        self.assertEqual(original, self.budget.ledger.read_bytes())
        self.budget.configure(set_grant="50", rate="0")
        self.now += 3600
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 50)
        self.assertTrue(self.budget.ledger.read_bytes().startswith(original))
        self.assertEqual(Decimal(self.budget.records()[-1]["events"][1]["delta"]), -60)
        for value in ("NaN", "Infinity", "nonsense"):
            with self.assertRaises(BudgetError):
                self.budget.configure(grant=value)
        self.now -= 20000
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 50)

    def test_concurrent_admission_and_deficit(self):
        self.budget.configure(grant=30)
        self.assertTrue(self.budget.admit("seed", "seed", "opus", "fix")["allowed"])
        self.budget.settle("seed", 10)
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(competing_admission, [str(self.directory)] * 4, ["a", "b", "c", "d"]))
        self.assertEqual(sum(results), 2)
        active = self.budget.snapshot()["active_sessions"]
        self.budget.configure(set_grant=0)
        for session in active:
            self.budget.settle(session["id"], 60)
            self.budget.settle(session["id"], 60)
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), -120)
        self.assertFalse(self.budget.admit("e", "e", "opus", "fix")["allowed"])

    def test_unknown_receipt_reconciliation_and_correction(self):
        self.budget.configure(grant=100)
        self.budget.admit("a", "a", "opus", "fix")
        self.budget.settle("a", None, "crashed")
        self.assertIn("unresolved", self.budget.admit("b", "b", "opus", "fix")["reason"])
        self.budget.reconcile("a", 23, "recovered cost receipt")
        self.budget.reconcile("a", 25, "Console reconciliation")
        snapshot = self.budget.snapshot()
        self.assertEqual(Decimal(snapshot["balance"]), 75)
        self.assertEqual(Decimal(snapshot["spent"]), 25)
        self.assertEqual(snapshot["unresolved"], [])

    def test_corruption_and_audited_torn_recovery(self):
        self.budget.configure(grant=100)
        original = self.budget.ledger.read_bytes()
        with self.budget.ledger.open("ab") as file:
            file.write(b'{"seq":2')
        damaged = self.budget.ledger.read_bytes()
        with self.assertRaises(BudgetError):
            self.budget.admit("a", "a", "opus", "fix")
        snapshot = self.budget.repair("power loss; investigate outstanding costs")
        archive = next(self.directory.glob("events.damaged-*"))
        self.assertEqual(archive.read_bytes(), damaged)
        self.assertTrue(self.budget.ledger.read_bytes().startswith(original))
        self.assertEqual(len(snapshot["unresolved"]), 1)

    def test_interrupted_repair_preserves_authoritative_damaged_ledger(self):
        self.budget.configure(grant=100)
        with self.budget.ledger.open("ab") as file:
            file.write(b'{"seq":2')
        damaged = self.budget.ledger.read_bytes()
        with patch("tauceti_worker.budget.os.replace", side_effect=OSError("power loss")):
            with self.assertRaises(BudgetError):
                self.budget.repair("failed replacement")
        self.assertEqual(self.budget.ledger.read_bytes(), damaged)
        self.assertEqual(next(self.directory.glob("events.damaged-*")).read_bytes(), damaged)
        with self.assertRaises(BudgetError):
            self.budget.admit("a", "a", "opus", "fix")
        self.assertEqual(len(self.budget.repair("retry audited repair")["unresolved"]), 1)

    def test_calibration_cooldown_and_orphan(self):
        self.budget.configure(grant=100)
        self.budget.admit("a", "a", "opus", "fix", bridge=str(self.directory / "missing"))
        result = self.budget.admit("b", "b", "opus", "fix")
        self.assertIn("unresolved", result["reason"])
        self.budget.reconcile("a", 10, "recovered orphan cost")
        self.budget.cooldown("opus", 60)
        self.assertIn("cooldown", self.budget.admit("b", "b", "opus", "fix")["reason"])
        self.now += 61
        self.assertTrue(self.budget.admit("b", "b", "opus", "fix")["allowed"])

    def test_resumption_baseline_and_fair_waiting(self):
        self.budget.configure(grant=30)
        self.assertTrue(self.budget.admit("seed", "seed", "opus", "fix")["allowed"])
        self.budget.settle("seed", 10, totals={"provider-session": "10"})
        self.budget.configure(set_grant=0)
        self.assertFalse(self.budget.admit("first", "first", "opus", "fix")["allowed"])
        self.now += 1
        self.assertFalse(self.budget.admit("second", "second", "opus", "fix")["allowed"])
        self.budget.configure(grant=10)
        self.assertFalse(self.budget.admit("second", "second", "opus", "fix")["allowed"])
        self.assertTrue(self.budget.admit("first", "first", "opus", "fix", resume="provider-session")["allowed"])
        with self.assertRaises(BudgetError):
            self.budget.admit("unknown", "unknown", "opus", "fix", resume="no-history")

    def test_budget_cli_json_history_and_set_grant(self):
        env = {**os.environ, "TAUCETI_BUDGET_DIR": str(self.directory)}
        command = [sys.executable, "-m", "tauceti_worker", "budget"]
        result = subprocess.run(
            command + ["--grant", "100", "--grant-rate", "0", "--json"], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Decimal(json.loads(result.stdout)["balance"]), 100)
        original = self.budget.ledger.read_bytes()
        result = subprocess.run(command + ["--set-grant", "50", "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Decimal(json.loads(result.stdout)["balance"]), 50)
        self.assertTrue(self.budget.ledger.read_bytes().startswith(original))
        history = subprocess.run(command + ["--log", "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(len(history.stdout.splitlines()), 2)

    def test_wait_polls_have_bounded_audit_growth(self):
        self.budget.configure(grant=10)
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10)
        for _ in range(1000):
            self.now += 2
            self.assertFalse(self.budget.admit("waiting", "waiting", "opus", "fix")["allowed"])
        self.assertEqual(len(self.budget.records()), 4)
        self.assertEqual(len(self.budget.snapshot()["waiting"]), 1)

    def test_other_model_cooldown_and_stale_queue_do_not_block(self):
        self.budget.configure(grant=100)
        self.budget.cooldown("opus", 60)
        self.assertFalse(self.budget.admit("opus", "opus", "opus", "fix")["allowed"])
        self.assertTrue(self.budget.admit("sonnet", "sonnet", "sonnet", "fix")["allowed"])
        self.budget.settle("sonnet", 10)
        self.budget.configure(set_grant=0)
        self.budget.admit("old", "old", "sonnet", "fix")
        self.now += 121
        self.budget.admit("new", "new", "sonnet", "fix")
        self.now += 1
        self.budget.admit("old", "old", "sonnet", "fix")
        self.budget.configure(grant=10)
        self.assertTrue(self.budget.admit("new", "new", "sonnet", "fix")["allowed"])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.native = self.root / "native-claude"
        self.native.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
assert os.environ['ANTHROPIC_API_KEY'] == 'fake-api-key'
assert not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
assert '--max-budget-usd' not in sys.argv
if sys.argv[1:3] == ['auth', 'status']:
    if os.environ.get('FAKE_UNLINK_AFTER_AUTH'):
        pathlib.Path(__file__).unlink()
    if os.environ.get('FAKE_BAD_AUTH'):
        print(json.dumps({'authMethod': 'oauth_token'}))
        sys.exit(0)
    print(json.dumps({'authMethod': 'api_key'}))
    sys.exit(0)
print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)
time.sleep(float(os.environ.get('FAKE_DELAY', '0')))
if os.environ.get('FAKE_CRASH'):
    sys.exit(1)
if os.environ.get('FAKE_EVENTS'):
    for event in json.loads(os.environ['FAKE_EVENTS']):
        print(json.dumps(event), flush=True)
    sys.exit(0)
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': os.environ.get('FAKE_SESSION', str(os.getpid())),
                  'total_cost_usd': float(os.environ.get('FAKE_COST', '10')), 'result': 'done'}), flush=True)
""")
        self.native.chmod(0o700)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_CLAUDE_CMD": str(self.native),
                "TAUCETI_BUDGET_DIR": str(self.root / "budget"),
                "TAUCETI_USE_BUDGET": "1",
                "TAUCETI_CLAUDE_BILLING": "api",
                "ANTHROPIC_API_KEY": "fake-api-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
                "TAUCETI_API_WAIT": "0",
            },
        )
        self.env.start()
        self.budget = Budget()
        self.budget.configure(grant=100)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_claude(self, **kwargs):
        return subprocess.run(
            ["claude", "-p", "hello", "--model", "opus", "--output-format", "stream-json"],
            capture_output=True,
            text=True,
            **kwargs,
        )

    def test_finished_sessions_never_cut_off_on_balance_change(self):
        os.environ["FAKE_DELAY"] = "0.6"
        os.environ["FAKE_COST"] = "150"
        with ClaudeAPIContext("worker1", "fix"):
            thread = threading.Thread(target=lambda: (time.sleep(0.3), self.budget.configure(set_grant=0, rate=0)))
            thread.start()
            result = self.run_claude()
            thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"result": "done"', result.stdout)
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), -150)
        with self.assertRaises(BudgetError), ClaudeAPIContext("worker2", "review"):
            self.fail("an unfunded work stage started")
        self.assertFalse(list((self.root / "budget/bridges").glob("*/key")))

    def test_external_reviewer_clean_environment_and_cumulative_cost(self):
        os.environ["FAKE_SESSION"] = "resumable-session"
        with ClaudeAPIContext("reviewer", "review") as context:
            minimal = {"PATH": os.environ["PATH"], "HOME": str(self.root), "FAKE_SESSION": "resumable-session"}
            result = self.run_claude(env=minimal)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = next(context.inbox.glob("*.receipt"))
            self.assertNotIn("fake-api-key", receipt.read_text())
        with ClaudeAPIContext("reviewer", "review"):
            result = self.run_claude(env={**os.environ, "FAKE_COST": "15"})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 15)

    def test_crash_freezes_future_admissions(self):
        with ClaudeAPIContext("worker1", "fix"):
            crashed = self.run_claude(env={**os.environ, "FAKE_CRASH": "1"})
        self.assertEqual(crashed.returncode, 1)
        self.assertEqual(len(self.budget.snapshot()["unresolved"]), 1)

    def test_cumulative_results_and_separate_session_segments(self):
        events = [
            dict(type="result", session_id="first", total_cost_usd=10),
            dict(type="result", session_id="first", total_cost_usd=15),
            dict(type="result", session_id="second", total_cost_usd=7),
        ]
        with ClaudeAPIContext("worker1", "fix"):
            result = self.run_claude(env={**os.environ, "FAKE_EVENTS": json.dumps(events)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 22)

    def test_missing_segment_is_not_hidden_by_later_known_cost(self):
        events = [
            dict(type="result", session_id="first", subtype="error_during_execution", total_cost_usd=0),
            dict(type="result", session_id="second", total_cost_usd=7),
        ]
        with ClaudeAPIContext("worker1", "fix"):
            result = self.run_claude(env={**os.environ, "FAKE_EVENTS": json.dumps(events)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.budget.snapshot()["unresolved"]), 1)

    def test_flags_managed_serialization_and_key_file(self):
        parser = build_parser()
        keyfile = self.root / "key-file"
        keyfile.write_text("fake-api-key\n")
        args = parser.parse_args(
            [
                "work",
                "--agent",
                "claude",
                "--claude-billing",
                "api",
                "--budget",
                "--anthropic-api-key-file",
                str(keyfile),
            ]
        )
        configure_api(args, "claude")
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        spec = WorkerSpec.from_dict(
            dict(id="api", agent="claude", claude_billing="api", budget=True, anthropic_api_key_file=str(keyfile)), 0
        )
        self.assertEqual(WorkerSpec.from_dict(spec.as_dict(), 0), spec)
        self.assertIn("--budget", spec.work_argv())
        with self.assertRaises(WorkersError):
            WorkerSpec.from_dict(dict(id="bad", agent="auto", claude_billing="api"), 0)
        with self.assertRaises(WorkersError):
            WorkerSpec.from_dict(dict(id="bad", agent="claude", budget=True), 0)

    def test_auth_preflight_prevents_stage_start(self):
        with patch.dict(os.environ, {"FAKE_BAD_AUTH": "1"}):
            with self.assertRaises(BudgetError), ClaudeAPIContext("worker1", "fix"):
                self.fail("stage started without API authentication")
        self.assertEqual(self.budget.snapshot()["active_sessions"], [])

    def test_launch_failure_costs_zero_and_does_not_freeze(self):
        with ClaudeAPIContext("worker1", "fix"):
            result = self.run_claude(env={**os.environ, "FAKE_UNLINK_AFTER_AUTH": "1"})
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.budget.snapshot()["unresolved"], [])
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_malformed_telemetry_does_not_interrupt_session(self):
        events = [
            dict(type="system", subtype="api_retry", error_status=429, retry_delay_ms="nonsense"),
            dict(type="result", session_id="first", total_cost_usd="nonsense"),
            dict(type="result", session_id="first", total_cost_usd=10),
        ]
        with ClaudeAPIContext("worker1", "fix"):
            result = self.run_claude(env={**os.environ, "FAKE_EVENTS": json.dumps(events)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_hostile_mailbox_input_is_rejected_without_host_file_access(self):
        with ClaudeAPIContext("worker1", "fix") as context:
            sentinel = self.root / "sentinel"
            sentinel.write_text("private host file")
            (context.inbox / ("a" * 32 + ".request")).symlink_to(sentinel)
            (context.inbox / ("b" * 32 + ".request")).write_text("[]")
            (context.inbox / ("c" * 32 + ".receipt")).write_text("{}")
            with self.assertRaises(BudgetError):
                context.handle(dict(action="cooldown", id="forged", model="opus", seconds=3600))
            result = self.run_claude()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(), "private host file")
        self.assertNotIn("TAUCETI_API_CONTEXT", os.environ)
        self.assertFalse((context.directory / "key").exists())

    def test_stale_receipt_recovery_and_live_reconciliation_guard(self):
        with ClaudeAPIContext("worker1", "fix") as context:
            admission = context.handle(dict(action="admit", id="a" * 32, model="opus"))
            invocation = admission["id"]
            with self.assertRaises(BudgetError):
                self.budget.reconcile(invocation, 0, "cannot ignore a running session")
            receipt = dict(id=invocation, totals={"provider-session": "12"}, outcome="exit:0")
            (context.inbox / (invocation + ".receipt")).write_text(json.dumps(receipt))
            context.record = lambda _: (_ for _ in ()).throw(BudgetError("simulated lost acknowledgment"))
        self.assertEqual(len(self.budget.snapshot()["unresolved"]), 1)
        os.utime(context.outbox / "heartbeat", (1, 1))
        (context.directory / "key").write_text("left by crash")
        snapshot = self.budget.recover("recover durable receipt after worker crash")
        self.assertEqual(snapshot["unresolved"], [])
        self.assertEqual(Decimal(snapshot["spent"]), 12)
        self.assertFalse((context.directory / "key").exists())
        self.assertEqual(Decimal(self.budget.recover("idempotent retry")["spent"]), 12)

    def test_bubble_does_not_require_host_claude_binary(self):
        with patch.dict(os.environ, {"TAUCETI_CLAUDE_CMD": "missing-host-claude"}):
            with ClaudeAPIContext("bubble", "review", host=False):
                pass

    def test_429_cools_new_admissions_without_stopping_active_session(self):
        events = [
            dict(type="system", subtype="api_retry", error_status=429, retry_delay_ms=10000),
            dict(type="result", session_id="first", total_cost_usd=10),
        ]
        with ClaudeAPIContext("worker1", "fix"):
            result = self.run_claude(env={**os.environ, "FAKE_EVENTS": json.dumps(events)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cooldown", self.budget.admit("second", "second", "opus", "fix")["reason"])

    def test_denial_never_waits_inside_round_even_for_loop_worker(self):
        with patch.dict(os.environ, {"TAUCETI_API_WAIT": "1", "FAKE_DELAY": "0.5"}):
            with ClaudeAPIContext("worker1", "fix"):
                proc = subprocess.Popen(
                    ["claude", "-p", "hello", "--model", "opus", "--output-format", "stream-json"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertIn('"init"', proc.stdout.readline())
                started = time.monotonic()
                denied = self.run_claude()
                self.assertEqual(denied.returncode, 75, denied.stderr)
                self.assertLess(time.monotonic() - started, 1)
                proc.communicate()
                self.assertEqual(proc.returncode, 0)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_broken_output_consumer_does_not_cut_off_native_session(self):
        with ClaudeAPIContext("worker1", "fix"):
            proc = subprocess.Popen(
                ["claude", "-p", "hello", "--model=opus", "--output-format=stream-json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            proc.stdout.close()
            proc.wait()
            proc.stderr.close()
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_loop_funding_wait_precedes_round_timeout_and_github(self):
        self.budget.configure(set_grant=-10, rate=10)
        args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--budget", "--loop"])
        with patch.object(loop, "run_round_subprocess") as round_mock, patch.object(loop, "github_budget") as gh_mock:
            with patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt):
                loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
        round_mock.assert_not_called()
        gh_mock.assert_not_called()

    def test_loop_propagates_api_flags_and_internal_ignore_quota(self):
        keyfile = self.root / "operator-key"
        keyfile.write_text("fake-api-key")
        parser = build_parser()
        args = parser.parse_args(
            [
                "work",
                "--agent",
                "claude",
                "--claude-billing",
                "api",
                "--budget",
                "--loop",
                "--anthropic-api-key-file",
                str(keyfile),
            ]
        )
        configure_api(args, "claude")
        tails = []

        def capture(tail):
            tails.append(tail)
            raise KeyboardInterrupt

        with (
            patch.object(loop, "run_round_subprocess", side_effect=capture),
            patch.object(loop, "github_budget", return_value={}),
        ):
            loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
        child = parser.parse_args(["_round", *tails[0]])
        self.assertTrue(child.budget)
        self.assertEqual(child.claude_billing, "api")
        self.assertEqual(child.anthropic_api_key_file, str(keyfile))
        self.assertTrue(child.ignore_quota)
        configure_api(child, "claude")

    def test_bubble_key_bridge_without_subscription_credentials(self):
        native_bin = self.root / "real-bin"
        native_bin.mkdir()
        (native_bin / "claude").symlink_to(self.native)
        bubble = self.root / "fake-bubble.py"
        bubble.write_text("""import json, os, pathlib, shutil, subprocess, sys, tempfile
args = sys.argv[1:]
assert '--no-claude-credentials' in args
assert '--claude-credentials' not in args
mounts = {}
for index, arg in enumerate(args):
    if arg == '--mount':
        src, dst, access = args[index + 1].split(':')
        mounts[dst] = src
        if dst == '/opt/api-bridge/inbox': assert access == 'rw'
        if dst in ('/opt/api-bridge/outbox', '/opt/api-bin', '/opt/round'): assert access == 'ro'
assert '/opt/api-bridge' not in mounts
assert '/opt/api-bridge/inbox' in mounts
assert '/opt/api-bridge/outbox' in mounts
assert all('events.jsonl' not in v for v in mounts.values())
private = pathlib.Path(tempfile.mkdtemp())
shutil.copytree(mounts['/opt/api-bin'], private / 'bin')
settings = private / 'bin/config.json'
config = json.loads(settings.read_text())
config['key'] = config['key'].replace('/opt/round', mounts['/opt/round'])
(private / 'inbox').symlink_to(mounts['/opt/api-bridge/inbox'])
(private / 'outbox').symlink_to(mounts['/opt/api-bridge/outbox'])
config['bridge'] = str(private)
settings.write_text(json.dumps(config))
mounts['/opt/api-bin'] = str(private / 'bin')
command = args[args.index('--command') + 1]
for dst, src in sorted(mounts.items(), key=lambda item: -len(item[0])):
    command = command.replace(dst, src)
env = dict(os.environ)
env['PATH'] = os.environ['CONTAINER_PATH']
sys.exit(subprocess.call(['bash', '-c', command], env=env))
""")
        os.environ["CONTAINER_PATH"] = f"{native_bin}:{os.environ['PATH']}"
        state = self.root / "worker-state"
        state.mkdir()
        worker = SimpleNamespace(
            cfg=SimpleNamespace(state=state, wid="bubble", home=self.root, logdir=state),
            rc=SimpleNamespace(add_cleanup=lambda _: None),
        )
        opts = SimpleNamespace(work_model="claude", account=None)
        with ClaudeAPIContext("bubble", "review"), contextlib.ExitStack() as stack:
            for name, replacement in (
                ("ensure_bubble_home", lambda _: dict(os.environ)),
                ("_bubble_pop", lambda *_: None),
                ("mirror_creds", lambda _: None),
                ("bubble_cmd", lambda: [sys.executable, str(bubble)]),
            ):
                stack.enter_context(patch.object(agents, name, replacement))
            stack.enter_context(
                patch.object(
                    agents,
                    "_stage_claude_creds_for_bubble",
                    side_effect=AssertionError("subscription credentials used"),
                )
            )
            rc = agents.run_in_bubble(
                worker, "repo/pull/1", "", opts, inner_cmd="claude -p hello --model opus --output-format stream-json"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)
        self.assertFalse((state / "bubble-round/anthropic.key").exists())


if __name__ == "__main__":
    unittest.main()

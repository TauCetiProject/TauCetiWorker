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

from tauceti_worker import agents
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
    print(json.dumps({'authMethod': 'api_key'}))
    sys.exit(0)
print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)
time.sleep(float(os.environ.get('FAKE_DELAY', '0')))
if os.environ.get('FAKE_CRASH'):
    sys.exit(1)
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
        with ClaudeAPIContext("worker2", "review"):
            denied = self.run_claude()
        self.assertEqual(denied.returncode, 75)
        self.assertNotIn('"type": "system"', denied.stdout)
        self.assertFalse(list((self.root / "budget/bridges").glob("*/key")))

    def test_external_reviewer_clean_environment_and_cumulative_cost(self):
        os.environ["FAKE_SESSION"] = "resumable-session"
        with ClaudeAPIContext("reviewer", "review") as context:
            minimal = {"PATH": os.environ["PATH"], "HOME": str(self.root), "FAKE_SESSION": "resumable-session"}
            result = self.run_claude(env=minimal)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = next(context.directory.glob("*.receipt"))
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
        spec = WorkerSpec.from_dict(
            dict(id="api", agent="claude", claude_billing="api", budget=True, anthropic_api_key_file=str(keyfile)), 0
        )
        self.assertEqual(WorkerSpec.from_dict(spec.as_dict(), 0), spec)
        self.assertIn("--budget", spec.work_argv())
        with self.assertRaises(WorkersError):
            WorkerSpec.from_dict(dict(id="bad", agent="auto", claude_billing="api"), 0)
        with self.assertRaises(WorkersError):
            WorkerSpec.from_dict(dict(id="bad", agent="claude", budget=True), 0)

    def test_bubble_key_bridge_without_subscription_credentials(self):
        native_bin = self.root / "real-bin"
        native_bin.mkdir()
        (native_bin / "claude").symlink_to(self.native)
        bubble = self.root / "fake-bubble.py"
        bubble.write_text("""import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
assert '--no-claude-credentials' in args
assert '--claude-credentials' not in args
mounts = {}
for index, arg in enumerate(args):
    if arg == '--mount':
        src, dst, access = args[index + 1].split(':')
        mounts[dst] = src
assert '/opt/api-bridge' in mounts
assert all('events.jsonl' not in v for v in mounts.values())
settings = pathlib.Path(mounts['/opt/api-bin']) / 'config.json'
config = json.loads(settings.read_text())
config['key'] = config['key'].replace('/opt/round', mounts['/opt/round'])
config['bridge'] = mounts['/opt/api-bridge']
settings.write_text(json.dumps(config))
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

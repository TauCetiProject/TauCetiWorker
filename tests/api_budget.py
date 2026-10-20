"""Grant accounting and real subprocess coverage using a fake Claude, with no paid calls."""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import signal
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

from tauceti_worker import agents, loop, work_units
from tauceti_worker.budget import Budget, BudgetError, process_identity
from tauceti_worker.claude_api import ClaudeAPIContext, configure_api
from tauceti_worker.cli import build_parser
from tauceti_worker.config import AdmissionUnavailable
from tauceti_worker.survey import Counters
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
        self.assertTrue(self.budget.admit("b", "b", "opus", "fix")["allowed"])
        self.assertEqual(self.budget.snapshot()["unresolved"], ["a"])
        self.budget.settle("b", 0)
        self.budget.reconcile("a", 23, "recovered cost receipt")
        self.budget.reconcile("a", 25, "Console reconciliation")
        snapshot = self.budget.snapshot()
        self.assertEqual(Decimal(snapshot["balance"]), 75)
        self.assertEqual(Decimal(snapshot["spent"]), 25)
        self.assertEqual(snapshot["unresolved"], [])

    def test_missing_receipt_keeps_reservation_and_funding_limit(self):
        self.budget.configure(grant=30)
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10)
        self.budget.admit("lost", "worker", "opus", "fix")
        self.budget.settle("lost", None, "reboot")
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["unresolved"], ["lost"])
        self.assertEqual(Decimal(snapshot["pending"]), 10)
        self.assertEqual(Decimal(snapshot["available"]), 10)
        self.assertEqual(Decimal(snapshot["spent"]), 10)
        self.assertTrue(self.budget.admit("next", "worker", "opus", "fix")["allowed"])
        self.assertEqual(self.budget.admit("third", "third", "opus", "fix")["reason"], "waiting for funding")
        self.budget.settle("next", 8)
        self.budget.reconcile("lost", 12, "recovered receipt after reboot")
        self.assertEqual(self.budget.snapshot()["unresolved"], [])
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 30)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_missing_calibration_receipt_does_not_block_other_models(self):
        self.budget.configure(grant=100)
        self.budget.admit("lost", "first", "opus", "fix")
        self.budget.settle("lost", None, "crashed")
        self.assertTrue(self.budget.admit("sonnet", "second", "sonnet", "fix")["allowed"])
        self.assertTrue(self.budget.admit("replacement", "third", "opus", "fix")["allowed"])
        self.assertEqual(
            self.budget.admit("parallel", "fourth", "opus", "fix")["reason"],
            "waiting for calibration session cost",
        )

    def test_repeated_missing_calibration_receipts_consume_available_funds(self):
        self.budget.configure(grant=3)
        for i in range(3):
            self.assertTrue(self.budget.admit(str(i), "worker", "cold-model", "fix")["allowed"])
            self.budget.settle(str(i), None, "crashed")
            snapshot = self.budget.snapshot()
            self.assertEqual(Decimal(snapshot["pending"]), i + 1)
            self.assertEqual(Decimal(snapshot["available"]), 2 - i)
        self.assertEqual(self.budget.admit("next", "worker", "cold-model", "fix")["reason"], "waiting for funding")
        self.budget.configure(grant=1)
        self.assertTrue(self.budget.admit("next", "worker", "cold-model", "fix")["allowed"])

    def test_calibration_uses_recent_costs_and_does_not_learn_from_failed_launches(self):
        self.budget.configure(grant=100)
        self.budget.admit("failed", "first", "opus", "fix")
        self.budget.settle("failed", 0, "launch-failed")
        self.assertEqual(self.budget.admit("replacement", "first", "opus", "fix")["estimate"], "1")
        self.assertIn("calibration", self.budget.admit("parallel", "second", "opus", "fix")["reason"])
        self.budget.settle("replacement", 7)
        self.assertEqual(self.budget.admit("new-model", "second", "sonnet", "fix")["estimate"], "7")

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
        self.assertTrue(snapshot["ledger_repair_required"])
        self.assertEqual(
            self.budget.admit("next", "next", "opus", "fix")["reason"], "ledger repair requires reconciliation"
        )
        self.budget.reconcile(snapshot["unresolved"][0], 0, "verified archived partial write was not a paid session")
        self.assertFalse(self.budget.snapshot()["ledger_repair_required"])
        self.assertTrue(self.budget.admit("next", "next", "opus", "fix")["allowed"])

    def test_provisional_reservation_preserves_grants_and_peer_admission(self):
        self.budget.configure(grant=100, rate=10)
        self.budget.admit("a", "a", "opus", "fix")
        receipt = dict(
            id="a", totals=None, provisional=True, lower_bound_totals={"provider": "12"}, outcome="interrupted"
        )
        self.budget.settle_receipt(receipt)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["unresolved"], [])
        self.assertEqual(snapshot["provisional"], ["a"])
        self.assertEqual(Decimal(snapshot["pending"]), 24)
        self.assertEqual(Decimal(snapshot["spent"]), 0)
        recorded = self.budget.ledger.read_bytes()
        for _ in range(3):
            self.budget.settle_receipt(receipt)
        self.assertEqual(
            self.budget.ledger.read_bytes(), recorded, "re-reading a checkpoint cannot compound its reserve"
        )
        self.now += 3600
        self.assertTrue(self.budget.admit("b", "b", "opus", "fix")["allowed"])
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 110, "grants keep accruing")
        self.budget.settle_receipt(dict(id="a", totals={"provider": "18"}, outcome="exit:0"))
        self.budget.settle_receipt(dict(id="a", totals={"provider": "18"}, outcome="exit:0"))
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["provisional"], [])
        self.assertEqual(Decimal(snapshot["spent"]), 18)
        self.assertEqual(Decimal(snapshot["balance"]), 92)
        self.assertEqual(Decimal(snapshot["pending"]), 24, "only the other worker's reservation remains")

    def test_provisional_reservation_obeys_funding_without_a_global_freeze(self):
        self.budget.configure(grant=30)
        self.budget.admit("a", "a", "opus", "fix")
        self.budget.settle_receipt(
            dict(id="a", totals=None, provisional=True, lower_bound_totals={"provider": "12"}, outcome="interrupted")
        )
        self.assertEqual(self.budget.admit("b", "b", "opus", "fix")["reason"], "waiting for funding")
        self.budget.configure(grant=20)
        self.assertTrue(self.budget.admit("b", "b", "opus", "fix")["allowed"])

    def test_increasing_checkpoint_and_resumed_cost_use_cumulative_baseline(self):
        self.budget.configure(grant=100)
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10, totals={"provider": "10"})
        self.budget.admit("a", "a", "opus", "fix", resume="provider")
        for cumulative, reserve in ((12, 12), (20, 20)):
            self.budget.settle_receipt(
                dict(
                    id="a",
                    totals=None,
                    provisional=True,
                    lower_bound_totals={"provider": str(cumulative)},
                    outcome="interrupted",
                )
            )
            self.assertEqual(Decimal(self.budget.snapshot()["pending"]), reserve)
        self.budget.settle_receipt(dict(id="a", totals={"provider": "23"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 23)

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
        self.assertTrue(result["allowed"])
        self.assertEqual(self.budget.snapshot()["unresolved"], ["a"])
        self.assertIn("calibration", self.budget.admit("c", "c", "opus", "fix")["reason"])
        self.budget.reconcile("a", 10, "recovered orphan cost")
        self.budget.cooldown("opus", 60)
        self.assertIn("cooldown", self.budget.admit("c", "c", "opus", "fix")["reason"])
        self.now += 61
        self.assertTrue(self.budget.admit("c", "c", "opus", "fix")["allowed"])

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_stale_bridge_with_live_owner_keeps_its_calibration_slot(self):
        self.budget.configure(grant=100)
        owner = process_identity()
        self.assertIsNotNone(owner)
        self.budget.admit("first", "first", "opus", "fix", bridge=str(self.directory / "missing"), owner=owner)
        self.assertIn("calibration", self.budget.admit("next", "next", "opus", "fix")["reason"])
        self.assertEqual(self.budget.snapshot()["unresolved"], [])
        with self.assertRaisesRegex(BudgetError, "live accounting bridge"):
            self.budget.reconcile("first", 1, "stale heartbeat does not prove a stopped session")
        self.assertEqual(self.budget.recover("check stale bridge")["unresolved"], [])

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_old_boot_or_reused_pid_releases_orphaned_calibration_slot(self):
        self.budget.configure(grant=100)
        owner = process_identity()
        self.assertIsNotNone(owner)
        for i, stale in enumerate([{**owner, "boot": "previous-boot"}, {**owner, "start": "different-process"}]):
            self.budget.admit(
                f"lost-{i}", "worker", f"model-{i}", "fix", bridge=str(self.directory / "missing"), owner=stale
            )
            self.assertTrue(self.budget.admit(f"next-{i}", "next", f"model-{i}", "fix")["allowed"])
            self.assertIn(f"lost-{i}", self.budget.snapshot()["unresolved"])

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_receipt_recovery_does_not_remove_key_for_live_owner(self):
        self.budget.configure(grant=100)
        bridge = self.directory / "live-bridge"
        bridge.mkdir()
        key = bridge / "key"
        key.write_text("fake-key")
        invocation = "a" * 32
        self.budget.admit(invocation, "worker", "opus", "fix", bridge=str(bridge), owner=process_identity())
        self.budget.settle(invocation, None, "missing result while bridge still alive")
        inbox = bridge / "inbox"
        inbox.mkdir()
        (inbox / (invocation + ".receipt")).write_text(
            json.dumps({"id": invocation, "totals": {"provider": "5"}, "outcome": "completed"})
        )
        self.assertEqual(self.budget.recover("recover pending receipt without a fresh heartbeat")["unresolved"], [])
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)
        self.assertTrue(key.exists())

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

    def test_unresolved_resume_only_holds_its_own_provider_until_receipt_recovery(self):
        self.budget.configure(grant=100)
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10, totals={"provider-session": "10"})
        self.budget.admit("lost", "worker", "opus", "fix", resume="provider-session")
        self.budget.settle("lost", None, "reboot")
        self.assertEqual(
            self.budget.admit("next", "worker", "opus", "fix", resume="provider-session")["reason"],
            "provider session cost baseline unresolved",
        )
        self.assertTrue(self.budget.admit("unrelated", "other", "opus", "fix")["allowed"])
        self.budget.settle("unrelated", 2)
        self.budget.settle_receipt({"id": "lost", "totals": {"provider-session": "20"}, "outcome": "recovered"})
        self.assertTrue(self.budget.admit("next", "worker", "opus", "fix", resume="provider-session")["allowed"])
        self.assertEqual(
            self.budget.admit("concurrent", "other", "opus", "fix", resume="provider-session")["reason"],
            "provider session already being resumed",
        )
        self.budget.settle_receipt({"id": "next", "totals": {"provider-session": "25"}, "outcome": "completed"})
        self.assertEqual(self.budget.snapshot()["unresolved"], [])
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 27)

    def test_accounting_integrity_wait_uses_admission_wait_exit(self):
        from tauceti_worker.claude_api import admission_failure
        from tauceti_worker.config import AdmissionUnavailable

        for reason in ("ledger repair requires reconciliation", "provider session cost baseline unresolved"):
            self.assertIsInstance(admission_failure(reason), AdmissionUnavailable)

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
print(json.dumps({'type': 'system', 'subtype': 'init', 'pid': os.getpid()}), flush=True)
time.sleep(float(os.environ.get('FAKE_DELAY', '0')))
if os.environ.get('FAKE_CRASH'):
    sys.exit(1)
if os.environ.get('FAKE_EVENTS'):
    for event in json.loads(os.environ['FAKE_EVENTS']):
        print(json.dumps(event), flush=True)
    time.sleep(float(os.environ.get('FAKE_POST_RESULT_DELAY', '0')))
    sys.exit(0)
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': os.environ.get('FAKE_SESSION', str(os.getpid())),
                  'total_cost_usd': float(os.environ.get('FAKE_COST', '10')), 'result': 'done'}), flush=True)
time.sleep(float(os.environ.get('FAKE_POST_RESULT_DELAY', '0')))
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

    def test_refusals_refund_all_stage_budgets_and_recover_without_paid_launch(self):
        head = "a" * 40
        candidate = SimpleNamespace(pr=7, head=head, rebase_budget_key="rebase-pr-7", foreign_repair_base="main")
        cfg = SimpleNamespace(state=self.root / "worker")
        worker = SimpleNamespace(cfg=cfg, counters=Counters(cfg))
        for stage, keys in (
            ("fix", ("fix-7-aaaaaaaaaaaa",)),
            ("fix-ci", ("ci-7-aaaaaaaaaaaa", "ci-pr-7")),
            ("bump", ("bump-7-aaaaaaaaaaaa", "bump-pr-7")),
            ("lint-repair", ("lint-repair-7-aaaaaaaaaaaa", "lint-repair-pr-7")),
            ("rebase", ("rebase-pr-7",)),
            ("review", ("review-err-7",)),
            ("progress", ("progress-err", "progress-attempt-ts")),
            ("roadmap", ()),
            ("overlap-scan", ()),
            ("overlap-fix", ()),
        ):
            for refusal in ("capacity", "funding"):
                with self.subTest(stage=stage, refusal=refusal):
                    self.budget.configure(set_grant=100)
                    for key in keys:
                        worker.counters.write(key, 2)
                    with ClaudeAPIContext("worker1", stage) as context:

                        def launch(*_, keys=keys, stage=stage, refusal=refusal):
                            for key in keys:
                                worker.counters.incr(key)
                            if stage == "rebase":
                                worker.counters.record_foreign_repair_launch(7, head, "base", "main")
                            if refusal == "funding":
                                self.budget.configure(set_grant=0)
                            result = self.run_claude()
                            self.assertEqual(result.returncode, 75, result.stderr)
                            self.assertNotIn('"type": "system"', result.stdout)
                            return result.returncode

                        with patch(
                            "tauceti_worker.fleet_capacity.admission_reason",
                            return_value="CI has capacity priority" if refusal == "capacity" else None,
                        ):
                            with self.assertRaises(AdmissionUnavailable) as caught:
                                work_units._run_github_stage(stage, launch, worker, None, candidate, None, False)
                        self.assertEqual(
                            caught.exception.state, "waiting-" + ("capacity" if refusal == "capacity" else "budget")
                        )
                        self.assertFalse(context.invocations)
                    self.assertEqual([worker.counters.read(key) for key in keys], [2] * len(keys))
                    self.assertFalse(worker.counters.foreign_repair_launched(7))
                    self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        # The next round can launch as soon as funding and capacity return.
        self.budget.configure(set_grant=100)
        with ClaudeAPIContext("worker1", "fix"):
            self.assertEqual(self.run_claude().returncode, 0)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_secondary_exception_propagates_wait_and_preserves_existing_checkpoint(self):
        cfg = SimpleNamespace(state=self.root / "worker")
        counters = Counters(cfg)
        worker = SimpleNamespace(cfg=cfg, counters=counters)
        candidate = SimpleNamespace(pr=7, head="a" * 40, rebase_budget_key="rebase-pr-7", foreign_repair_base="main")
        counters.record_foreign_repair_launch(7, candidate.head, "saved", "main")
        checkpoint = cfg.state / "rebase-progress-7.json"
        checkpoint.write_text('{"checkpoint":"preserved"}')
        with ClaudeAPIContext("worker1", "rebase"):

            def launch(*_):
                counters.incr("rebase-pr-7")
                self.budget.configure(set_grant=0)
                self.assertEqual(self.run_claude().returncode, 75)
                raise RuntimeError("secondary engine could not parse refused result")

            with self.assertRaises(AdmissionUnavailable):
                work_units._run_github_stage("rebase", launch, worker, None, candidate, None, False)
        self.assertEqual(counters.read("rebase-pr-7"), 0)
        self.assertTrue(counters.foreign_repair_launched(7))
        self.assertEqual(checkpoint.read_text(), '{"checkpoint":"preserved"}')

    def test_later_funding_refusal_keeps_previously_admitted_attempt(self):
        cfg = SimpleNamespace(state=self.root / "worker")
        worker = SimpleNamespace(cfg=cfg, counters=Counters(cfg))
        candidate = SimpleNamespace(pr=7, head="a" * 40)
        with ClaudeAPIContext("worker1", "fix"):

            def launch(*_):
                worker.counters.incr("fix-7-aaaaaaaaaaaa")
                self.assertEqual(self.run_claude().returncode, 0)
                self.budget.configure(set_grant=0)
                return self.run_claude().returncode

            with self.assertRaises(AdmissionUnavailable):
                work_units._run_github_stage("fix", launch, worker, None, candidate, None, False)
        self.assertEqual(worker.counters.read("fix-7-aaaaaaaaaaaa"), 1)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_policy_expiry_or_malformed_policy_between_bridge_and_launch(self):
        policy = self.root / "admission.json"
        for value in (dict(version=1, workers=["worker1"], expires_at=time.time() - 1), [], "malformed"):
            with self.subTest(policy=value), ClaudeAPIContext("worker1", "fix") as context:
                policy.write_text(json.dumps(value))
                with patch.dict(os.environ, TAUCETI_FLEET_ADMISSION_FILE=str(policy)):
                    result = self.run_claude()
                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertFalse(context.invocations)
                self.assertNotIn('"type": "system"', result.stdout)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_launched_failure_stays_charged_and_admitted_unit_finishes_after_ci_reduction(self):
        cfg = SimpleNamespace(state=self.root / "worker")
        worker = SimpleNamespace(cfg=cfg, counters=Counters(cfg))
        candidate = SimpleNamespace(pr=7, head="a" * 40)
        key = "fix-7-aaaaaaaaaaaa"
        with ClaudeAPIContext("worker1", "fix") as context:

            def launch(*_):
                worker.counters.incr(key)
                first = self.run_claude()
                self.assertEqual(first.returncode, 0)
                with patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"):
                    failed = self.run_claude(env={**os.environ, "FAKE_CRASH": "1"})
                return failed.returncode

            self.assertEqual(work_units._run_github_stage("fix", launch, worker, None, candidate, None, False), 1)
            self.assertEqual(len(context.invocations), 2)
        self.assertEqual(worker.counters.read(key), 1)

    def test_usage_storage_failure_before_launch_releases_reservation(self):
        with ClaudeAPIContext("worker1", "fix") as context:
            settings = json.loads((context.bin / "config.json").read_text())
            blocked = self.root / "not-a-directory"
            blocked.write_text("fixture")
            settings["usage_dir"] = str(blocked / "usage")
            (context.bin / "config.json").write_text(json.dumps(settings))
            result = self.run_claude()
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertNotIn('"type": "system"', result.stdout)
            self.assertIn("no inference started", result.stderr)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_internal_partial_events_are_not_forwarded_to_ordinary_callers(self):
        os.environ["FAKE_EVENTS"] = json.dumps(
            [
                dict(
                    type="stream_event",
                    session_id="s",
                    event=dict(
                        type="message_start",
                        message=dict(id="m", model="claude-opus-5-5", usage=dict(input_tokens=1, output_tokens=0)),
                    ),
                ),
                dict(
                    type="assistant",
                    session_id="s",
                    message=dict(id="m", model="claude-opus-5-5", usage=dict(input_tokens=1, output_tokens=2)),
                ),
                dict(type="result", session_id="s", total_cost_usd=1),
            ]
        )
        with ClaudeAPIContext("worker1", "fix") as context:
            result = self.run_claude()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('"type": "stream_event"', result.stdout)
            namespace = Path(context.config["usage_dir"])
            self.assertEqual(namespace.name, context.directory.name)
            self.assertTrue(list(namespace.glob("*.json")))

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
        with self.assertRaises(AdmissionUnavailable), ClaudeAPIContext("worker2", "review"):
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
            result = subprocess.run(
                ["claude", "-p", "hello", "--model=opus", "--resume=resumable-session", "--output-format=json"],
                env={**os.environ, "FAKE_COST": "15"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["total_cost_usd"], 15)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 15)

    def test_crash_keeps_cost_unresolved_and_allows_next_native_session(self):
        with ClaudeAPIContext("worker1", "fix"):
            crashed = self.run_claude(env={**os.environ, "FAKE_CRASH": "1"})
        self.assertEqual(crashed.returncode, 1)
        unresolved = self.budget.snapshot()["unresolved"]
        self.assertEqual(len(unresolved), 1)
        with ClaudeAPIContext("worker2", "fix", model="opus"):
            following = self.run_claude()
        self.assertEqual(following.returncode, 0, following.stderr)
        self.assertEqual(self.budget.snapshot()["unresolved"], unresolved)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 10)

    def test_stage_and_loop_continue_with_unresolved_costs(self):
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10)
        self.budget.admit("lost", "worker1", "opus", "fix")
        self.budget.settle("lost", None, "reboot")
        with ClaudeAPIContext("worker2", "fix", model="opus"):
            result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--budget", "--loop"])
        with (
            patch.object(loop, "run_round_subprocess", side_effect=KeyboardInterrupt) as round_mock,
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
        round_mock.assert_called_once()
        self.assertEqual(self.budget.snapshot()["unresolved"], ["lost"])
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 10)

    def test_stage_and_loop_still_pause_for_ledger_repair(self):
        with self.budget.ledger.open("ab") as file:
            file.write(b'{"seq":2')
        self.budget.repair("audit torn transaction")
        with (
            self.assertRaisesRegex(AdmissionUnavailable, "ledger repair"),
            ClaudeAPIContext("worker1", "fix", model="opus"),
        ):
            self.fail("stage started before ledger audit")
        args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--budget", "--loop"])
        with (
            patch.object(loop, "run_round_subprocess") as round_mock,
            patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
        round_mock.assert_not_called()

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
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        self.assertGreaterEqual(Decimal(self.budget.snapshot()["pending"]), 14)

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

    def test_stage_requires_learned_threshold_and_reports_funding_time(self):
        self.budget.admit("seed", "seed", "opus", "fix")
        self.budget.settle("seed", 10)
        self.budget.configure(set_grant=1, rate=10)
        snapshot = self.budget.snapshot(worker="worker1", model="opus", phase="fix")
        self.assertEqual(Decimal(snapshot["admission_threshold"]), 10)
        self.assertGreater(snapshot["funding_at"], time.time() + 3200)
        with self.assertRaises(AdmissionUnavailable), ClaudeAPIContext("worker1", "fix", model="opus"):
            self.fail("stage began below the learned admission estimate")

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
            admitted = context.handle(dict(action="admit", id="d" * 32, model="sonnet"))
            with self.assertRaises(BudgetError):
                context.handle(dict(action="settle", id=admitted["id"], totals="malformed", outcome="exit:0"))
            context.handle(dict(action="settle", id=admitted["id"], totals={"unused": "0"}, outcome="exit:0"))
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
        with patch("tauceti_worker.budget.bridge_owner_alive", return_value=False):
            snapshot = self.budget.recover("recover durable receipt after worker crash")
        self.assertEqual(snapshot["unresolved"], [])
        self.assertEqual(Decimal(snapshot["spent"]), 12)
        self.assertFalse((context.directory / "key").exists())
        self.assertEqual(Decimal(self.budget.recover("idempotent retry")["spent"]), 12)

    def test_result_cost_survives_sigterm_before_native_exit(self):
        events = [dict(type="result", session_id="completed-model", total_cost_usd=12)]
        with ClaudeAPIContext("worker1", "roadmap") as context:
            proc = subprocess.Popen(
                ["claude", "-p", "hello", "--model=opus", "--output-format=stream-json"],
                env={**os.environ, "FAKE_EVENTS": json.dumps(events), "FAKE_POST_RESULT_DELAY": "30"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                self.assertEqual(json.loads(proc.stdout.readline())["type"], "system")
                self.assertEqual(json.loads(proc.stdout.readline())["type"], "result")
                self.assertIsNone(proc.poll())
                receipt = json.loads(next(context.inbox.glob("*.receipt")).read_text())
                self.assertIsNone(receipt["totals"])
                self.assertTrue(receipt["provisional"])
                self.assertEqual(receipt["lower_bound_totals"], {"completed-model": "12"})
                self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=5)
        self.assertEqual(len(self.budget.snapshot()["provisional"]), 1)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_native_signal_after_result_keeps_checkpoint_provisional(self):
        with ClaudeAPIContext("worker1", "roadmap") as context:
            proc = subprocess.Popen(
                ["claude", "-p", "hello", "--model=opus", "--output-format=stream-json"],
                env={**os.environ, "FAKE_POST_RESULT_DELAY": "30"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            native = json.loads(proc.stdout.readline())["pid"]
            self.assertEqual(json.loads(proc.stdout.readline())["type"], "result")
            os.kill(native, signal.SIGTERM)
            proc.communicate(timeout=5)
            receipt = json.loads(next(context.inbox.glob("*.receipt")).read_text())
            self.assertTrue(receipt["provisional"])
            self.assertIsNone(receipt["totals"])
        self.assertEqual(len(self.budget.snapshot()["provisional"]), 1)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_new_inference_invalidates_previous_cost_checkpoint(self):
        events = [
            dict(type="result", session_id="continued-model", total_cost_usd=12),
            dict(type="assistant", session_id="continued-model", message={"usage": {"output_tokens": 1}}),
        ]
        with ClaudeAPIContext("worker1", "roadmap") as context:
            proc = subprocess.Popen(
                ["claude", "-p", "hello", "--model=opus", "--output-format=stream-json"],
                env={**os.environ, "FAKE_EVENTS": json.dumps(events), "FAKE_POST_RESULT_DELAY": "30"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                for expected in ("system", "result", "assistant"):
                    self.assertEqual(json.loads(proc.stdout.readline())["type"], expected)
                self.assertIsNone(json.loads(next(context.inbox.glob("*.receipt")).read_text())["totals"])
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=5)
        self.assertEqual(len(self.budget.snapshot()["provisional"]), 1)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

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

    def test_local_admission_wait_does_not_escalate_no_progress_backoff(self):
        from tauceti_worker.constants import EX_ADMISSION_WAIT

        args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--budget", "--loop"])
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise KeyboardInterrupt

        with (
            patch.object(loop, "run_round_subprocess", return_value=EX_ADMISSION_WAIT),
            patch.object(loop, "github_budget", return_value={}),
        ):
            with patch.object(loop.time, "sleep", side_effect=sleep):
                loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
        self.assertEqual(waits, [5, 5])

    def test_loop_queue_refresh_failure_is_recoverable(self):
        from tauceti_worker.constants import EX_ADMISSION_WAIT

        args = build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--budget", "--loop"])
        with (
            patch.object(loop, "run_round_subprocess", return_value=EX_ADMISSION_WAIT),
            patch.object(loop, "github_budget", return_value={}),
        ):
            with (
                patch.object(Budget, "refresh_waiter", side_effect=BudgetError("damaged ledger")),
                patch.object(loop.time, "sleep", side_effect=KeyboardInterrupt),
            ):
                self.assertEqual(loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude"), 130)

    def test_auth_failure_uses_normal_backoff_and_stale_ticket_expires(self):
        from tauceti_worker.claude_api import admission_failure
        from tauceti_worker.config import AdmissionUnavailable, NoProgress

        self.assertIs(type(admission_failure("Claude API authentication unavailable")), NoProgress)
        self.assertIs(type(admission_failure("waiting for funding")), AdmissionUnavailable)
        self.budget.configure(set_grant=0)
        self.budget.admit("wait", "worker1", "opus", "fix")
        self.budget.clock = lambda: time.time() + 121
        self.budget.refresh_waiter("worker1")
        self.assertEqual(self.budget.snapshot()["waiting"], [])

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
config['usage_dir'] = mounts['/opt/api-usage']
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

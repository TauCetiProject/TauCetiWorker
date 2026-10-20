"""Unpaid process fixtures for durable launch, crash recovery and late evidence."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.budget import Budget, BudgetError, process_identity
from tauceti_worker.usage_accounting import UsageJournal, read_usage
from tauceti_worker.usage_lifecycle import alive, durable, identity, resolve
from tauceti_worker.usage_supervisor import (
    ensure_supervisor,
    lifecycle_path,
    read_lifecycle,
    record_round_end,
    register,
    registration,
    tick,
)

MODEL = "claude-opus-5-5"


@unittest.skipUnless(sys.platform == "linux", "Linux birth identities and namespace process fixtures")
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.budget = Budget(self.root)
        self.budget.configure(grant=100)
        self.bridge = self.root / "bridges" / uuid.uuid4().hex
        (self.bridge / "inbox").mkdir(parents=True)
        (self.bridge / "outbox").mkdir()
        self.id = uuid.uuid4().hex
        self.shim = self.sleeper()
        self.owner = process_identity()
        self.lifecycle = registration(
            self.id,
            uuid.uuid4().hex,
            self.bridge,
            identity(self.shim.pid),
            enabled=True,
            owner=self.owner,
            parent=identity(),
        )
        self.budget.admit(
            self.id, "fixture", MODEL, "fix", bridge=str(self.bridge), owner=self.owner, lifecycle=self.lifecycle
        )
        register(self.root, self.lifecycle)
        self.journal = UsageJournal(self.root / "usage" / self.bridge.name, self.id, model=MODEL)

    def sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(cleanup)
        return proc

    def stop(self, proc, sig=signal.SIGKILL):
        proc.send_signal(sig)
        proc.wait()

    def launch(self):
        native = self.sleeper()
        durable(self.bridge / "inbox" / (self.id + ".launch"), dict(id=self.id, native=identity(native.pid)))
        tick(self.budget)
        self.assertTrue((self.bridge / "outbox" / (self.id + ".permit")).exists())
        return native

    def response(self, model=MODEL, complete=True):
        self.journal.observe(
            dict(
                type="assistant",
                session_id="provider",
                message=dict(id="response", model=model, usage=dict(input_tokens=100, output_tokens=100000)),
            )
        )
        if not complete:
            self.journal.observe(dict(type="user", session_id="provider"))

    def saved(self):
        with self.budget.locked():
            return self.budget.replay()["sessions"][self.id]

    def test_sigterm_and_sigkill_after_response_keep_uncertain_tail(self):
        native = self.launch()
        self.response()
        self.assertFalse(read_usage(self.journal.path)["inflight"])
        self.stop(native, signal.SIGTERM)
        self.stop(self.shim, signal.SIGKILL)
        record_round_end(self.root, self.owner, "timeout", -9)
        tick(self.budget)
        saved = self.saved()
        self.assertEqual(saved["status"], "provisional")
        self.assertGreater(Decimal(saved["cost"]), 2)
        self.assertGreater(Decimal(saved["reserve"]), 0)
        self.assertFalse(read_usage(self.journal.path)["closed"])
        self.assertIn("round-timeout", {e["kind"] for e in read_lifecycle(self.root, self.id)["events"]})
        before = self.budget.ledger.read_bytes()
        tick(self.budget)
        self.assertEqual(self.budget.ledger.read_bytes(), before)

    def test_sigkill_during_request_preserves_priced_prefix(self):
        native = self.launch()
        self.response(complete=False)
        self.stop(native)
        self.stop(self.shim, signal.SIGTERM)
        tick(self.budget)
        self.assertEqual(self.saved()["cost_provenance"], "token-priced-prefix")
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_live_native_is_never_recovered_even_with_dead_bridge_and_reused_shim_pid(self):
        native = self.launch()
        self.response()
        self.journal.flush(closed=True)
        self.stop(self.shim)
        saved = read_lifecycle(self.root, self.id)
        saved["owner"] = {**self.owner, "start": "reused"}
        durable(lifecycle_path(self.root, self.id), saved)
        tick(self.budget)
        self.assertTrue(alive(identity(native.pid)))
        self.assertIsNone(self.budget.recover_usage(self.id, allow_live_owner=True))
        self.budget.recover("fixture")
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        self.assertEqual(self.saved()["status"], "active")
        self.assertIn("bridge-death", {e["kind"] for e in read_lifecycle(self.root, self.id)["events"]})

    def test_no_inference_failed_launch_has_exact_zero(self):
        self.stop(self.shim)
        tick(self.budget)
        self.assertEqual(self.saved()["status"], "settled")
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        # A delayed gate cannot obtain a permit after the zero-cost decision.
        native = self.sleeper()
        durable(self.bridge / "inbox" / (self.id + ".launch"), dict(id=self.id, native=identity(native.pid)))
        tick(self.budget)
        self.assertFalse((self.bridge / "outbox" / (self.id + ".permit")).exists())

    def test_missing_journal_never_gets_launch_permit(self):
        self.journal.path.unlink()
        self.journal.log.unlink()
        native = self.sleeper()
        durable(self.bridge / "inbox" / (self.id + ".launch"), dict(id=self.id, native=identity(native.pid)))
        tick(self.budget)
        self.assertFalse((self.bridge / "outbox" / (self.id + ".permit")).exists())

    def test_late_native_receipt_replaces_prefix_once(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        durable(
            self.bridge / "inbox" / (self.id + ".receipt"), dict(id=self.id, totals={"provider": "3"}, outcome="exit:0")
        )
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)
        before = self.budget.ledger.read_bytes()
        tick(self.budget)
        self.assertEqual(self.budget.ledger.read_bytes(), before)

    def test_resume_response_replay_is_not_charged_twice(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        durable(
            self.bridge / "inbox" / (self.id + ".receipt"), dict(id=self.id, totals={"provider": "3"}, outcome="exit:0")
        )
        tick(self.budget)
        resumed = uuid.uuid4().hex
        shim = self.sleeper()
        lifecycle = registration(
            resumed,
            uuid.uuid4().hex,
            self.bridge,
            identity(shim.pid),
            enabled=True,
            owner=self.owner,
            parent=identity(),
        )
        self.assertTrue(
            self.budget.admit(
                resumed, "fixture", MODEL, "fix", bridge=str(self.bridge), resume="provider", lifecycle=lifecycle
            )["allowed"]
        )
        register(self.root, lifecycle)
        journal = UsageJournal(self.journal.directory, resumed, model=MODEL)
        replay = dict(
            type="assistant",
            session_id="provider",
            message=dict(id="response", model=MODEL, usage=dict(input_tokens=100, output_tokens=100000)),
        )
        journal.observe(replay)
        journal.observe({**replay, "message": {**replay["message"], "id": "new-response"}})
        # Simulate a fully drained successful journal, with a lost native receipt.
        journal.flush(closed=True)
        self.stop(shim)
        # It was never permitted in this fixture, so register the already-proved
        # native exit instead of invoking the no-inference admission path.
        saved = read_lifecycle(self.root, resumed)
        saved.update(phase="stopped", native=identity())
        saved["native"]["start"] = "stopped-birth"
        durable(lifecycle_path(self.root, resumed), saved)
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), Decimal("5.0004"))
        durable(
            self.bridge / "inbox" / (resumed + ".receipt"), dict(id=resumed, totals={"provider": "5"}, outcome="exit:0")
        )
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)

    def test_torn_checkpoint_recovers_jsonl_without_closing_it(self):
        native = self.launch()
        self.response()
        self.journal.path.write_text('{"torn":')
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        self.assertGreater(Decimal(self.budget.snapshot()["spent"]), 2)
        self.assertEqual(self.saved()["status"], "provisional")

    def test_unknown_pricing_retains_reserve(self):
        native = self.launch()
        self.response(model="future-unpriced")
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertEqual(self.saved()["status"], "provisional")

    def test_successful_exit_records_terminal_and_settles_native_cost(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.journal.flush(closed=True)
        durable(self.bridge / "inbox" / (self.id + ".terminal"), dict(id=self.id, kind="successful-exit", returncode=0))
        durable(
            self.bridge / "inbox" / (self.id + ".receipt"), dict(id=self.id, totals={"provider": "3"}, outcome="exit:0")
        )
        self.stop(self.shim)
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.assertIn("successful-exit", {e["kind"] for e in read_lifecycle(self.root, self.id)["events"]})

    def test_supervisor_restart_rebuilds_admission_sidecar(self):
        lifecycle_path(self.root, self.id).unlink()
        self.stop(self.shim)
        tick(self.budget)
        saved = read_lifecycle(self.root, self.id)
        self.assertEqual(saved["admission_id"], self.lifecycle["admission_id"])
        self.assertEqual(saved["shim"], self.lifecycle["shim"])
        self.assertEqual(self.saved()["status"], "settled")

    def test_unreadable_process_birth_is_not_proof_of_death(self):
        owner = identity()
        with patch("tauceti_worker.usage_lifecycle.identity", side_effect=PermissionError("unreadable proc")):
            self.assertTrue(alive(owner))
        with patch("tauceti_worker.usage_lifecycle.identity", side_effect=FileNotFoundError("gone")):
            self.assertFalse(alive(owner))

    def test_reused_pid_is_not_the_registered_process(self):
        owner = identity()
        self.assertFalse(alive({**owner, "start": "different-birth"}))
        self.assertIsNone(resolve({**owner, "start": "different-birth"}))

    def test_daemon_is_detached_and_restarts_to_recover_stopped_invocation(self):
        ensure_supervisor(self.root)
        first = json.loads((self.root / "usage-supervisor.json").read_text())["owner"]
        self.assertNotEqual(os.getpgid(first["pid"]), os.getpgrp())
        os.kill(first["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while alive(first) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.stop(self.shim)
        ensure_supervisor(self.root)
        deadline = time.monotonic() + 5
        while self.saved()["status"] != "settled" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.saved()["status"], "settled")

    def test_record_only_rollout_keeps_evidence_and_can_enable_recovery_later(self):
        (self.root / "usage-recovery-mode").write_text("record-only")
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        self.assertEqual(self.saved()["status"], "active")
        self.assertEqual(read_lifecycle(self.root, self.id)["phase"], "stopped")
        (self.root / "usage-recovery-mode").write_text("auto")
        tick(self.budget)
        self.assertEqual(self.saved()["status"], "provisional")

    def test_live_native_rejects_authoritative_settlement_and_manual_reconciliation(self):
        self.launch()
        with self.assertRaisesRegex(BudgetError, "live native"):
            self.budget.settle_receipt(dict(id=self.id, totals={"provider": "3"}, outcome="exit:0"))
        with self.assertRaisesRegex(BudgetError, "live accounting"):
            self.budget.reconcile(self.id, 3, "fixture")
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_invalid_recovery_policy_never_permits_inference(self):
        (self.root / "usage-recovery-mode").write_text("typo")
        native = self.sleeper()
        durable(self.bridge / "inbox" / (self.id + ".launch"), dict(id=self.id, native=identity(native.pid)))
        with patch("builtins.print"):
            tick(self.budget)
        self.assertFalse((self.bridge / "outbox" / (self.id + ".permit")).exists())

    def test_missing_sidecar_does_not_bypass_live_guards_in_budget_entrypoints(self):
        self.launch()
        self.response()
        self.journal.flush(closed=True)
        lifecycle_path(self.root, self.id).unlink()
        self.assertIsNone(self.budget.recover_usage(self.id, allow_live_owner=True))
        with self.assertRaisesRegex(BudgetError, "live native"):
            self.budget.settle_receipt(dict(id=self.id, totals={"provider": "3"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)

    def test_lost_sidecar_before_first_recovery_charges_usage_instead_of_zero(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        lifecycle_path(self.root, self.id).unlink()
        tick(self.budget)
        self.assertGreater(Decimal(self.budget.snapshot()["spent"]), 2)
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertEqual(self.saved()["status"], "provisional")

    def test_lost_sidecar_after_permit_never_refunds_a_priced_prefix(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        before = Decimal(self.budget.snapshot()["spent"])
        lifecycle_path(self.root, self.id).unlink()
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), before)
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertEqual(self.saved()["status"], "provisional")
        with self.assertRaisesRegex(BudgetError, "contradicts durable usage"):
            self.budget.settle_receipt(dict(id=self.id, totals={self.id: "0"}, outcome="launch-failed"))

    def test_no_exec_proof_releases_a_never_used_permit(self):
        native = self.launch()
        gate = read_lifecycle(self.root, self.id)["gate"]
        durable(self.bridge / "inbox" / (self.id + ".no-exec"), dict(id=self.id, native=gate, reason="permit-timeout"))
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        self.assertEqual(self.saved()["status"], "settled")
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_no_exec_proof_cannot_override_priced_responses(self):
        native = self.launch()
        self.response()
        gate = read_lifecycle(self.root, self.id)["gate"]
        durable(self.bridge / "inbox" / (self.id + ".no-exec"), dict(id=self.id, native=gate))
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        self.assertGreater(Decimal(self.budget.snapshot()["spent"]), 2)
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_restart_uses_persisted_recovery_stamp(self):
        native = self.launch()
        self.response()
        self.stop(native)
        self.stop(self.shim)
        tick(self.budget)
        with (
            patch.object(self.budget, "recover_usage") as recover,
            patch.object(self.budget, "settle_receipt") as settle,
        ):
            tick(self.budget, cache={})
            recover.assert_not_called()
            settle.assert_not_called()

    def remote_launch(self):
        self.stop(self.shim)
        tick(self.budget)
        self.id = uuid.uuid4().hex
        birth = dict(pid=20, start="123", boot="vm-boot", namespace="pid:[vm]")
        with patch("tauceti_worker.usage_supervisor.resolve", return_value=None):
            value = registration(
                self.id,
                uuid.uuid4().hex,
                self.bridge,
                birth,
                enabled=True,
                owner=self.owner,
                parent=identity(),
                allow_remote=True,
            )
        self.assertTrue(
            self.budget.admit(self.id, "fixture", MODEL, "fix", bridge=str(self.bridge), lifecycle=value)["allowed"]
        )
        register(self.root, value)
        self.journal = UsageJournal(self.journal.directory, self.id, model=MODEL)
        native = {**birth, "pid": 21, "start": "124"}
        durable(self.bridge / "inbox" / (self.id + ".launch"), dict(id=self.id, native=native))
        tick(self.budget)
        self.assertTrue((self.bridge / "outbox" / (self.id + ".permit")).exists())
        return native

    def test_vm_native_receipt_settles_without_interpreting_vm_pid_on_host(self):
        native = self.remote_launch()
        self.response()
        self.assertTrue(read_lifecycle(self.root, self.id)["external"])
        with self.assertRaisesRegex(BudgetError, "live native"):
            self.budget.settle_receipt(dict(id=self.id, totals={"provider": "3"}, outcome="exit:0"))
        durable(
            self.bridge / "inbox" / (self.id + ".terminal"),
            dict(id=self.id, native=native, kind="successful-exit", returncode=0),
        )
        durable(
            self.bridge / "inbox" / (self.id + ".receipt"), dict(id=self.id, totals={"provider": "3"}, outcome="exit:0")
        )
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_late_vm_authoritative_receipt_survives_missing_terminal_file(self):
        self.remote_launch()
        self.response()
        durable(
            self.bridge / "inbox" / (self.id + ".receipt"), dict(id=self.id, totals={"provider": "3"}, outcome="exit:0")
        )
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_vm_bridge_death_alone_preserves_unknown_liveness_and_reserve(self):
        self.remote_launch()
        self.response()
        value = read_lifecycle(self.root, self.id)
        value["owner"]["start"] = "dead-bridge"
        durable(lifecycle_path(self.root, self.id), value)
        tick(self.budget)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)
        self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertIn("remote-liveness-unconfirmed", {e["kind"] for e in read_lifecycle(self.root, self.id)["events"]})

    def test_round_end_reporting_failure_preserves_round_returncode(self):
        from types import SimpleNamespace

        from tauceti_worker import round as rounds

        process = SimpleNamespace(pid=os.getpid(), wait=lambda timeout: 7, returncode=7)
        with (
            patch("tauceti_worker.claude_api.api_mode", return_value=True),
            patch("tauceti_worker.usage_supervisor.ensure_supervisor"),
            patch("tauceti_worker.usage_supervisor.record_round_end", side_effect=OSError("storage unavailable")),
            patch.object(rounds, "spawn_round", return_value=process),
            patch.object(rounds, "reap_round_group"),
            patch.object(rounds, "log"),
        ):
            self.assertEqual(rounds.run_round_subprocess([]), 7)

    def test_bad_lifecycle_does_not_block_other_recovery(self):
        (self.root / "lifecycles" / ("0" * 32 + ".json")).write_text("bad json")
        self.stop(self.shim)
        with patch("builtins.print"):
            tick(self.budget)
        self.assertEqual(self.saved()["status"], "settled")


if __name__ == "__main__":
    unittest.main()

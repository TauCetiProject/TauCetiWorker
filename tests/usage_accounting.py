"""Crash, duplicate, pricing and estimated-settlement coverage. No paid calls."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.budget import Budget
from tauceti_worker.usage_accounting import UsageJournal, price_usage, read_usage

MODEL = "claude-opus-5-5"
AT = 1792404000


def response(mid="m", session="s", **tokens):
    return dict(
        type="assistant", session_id=session, message=dict(id=mid, model=MODEL, usage={"input_tokens": 0, **tokens})
    )


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = "a" * 32
        self.journal = UsageJournal(self.root / "usage", self.identity, model=MODEL, clock=lambda: AT)
        self.budget = Budget(self.root, clock=lambda: AT)
        self.budget.configure(grant=100)
        self.bridge = self.root / "bridges/x"
        (self.bridge / "inbox").mkdir(parents=True)
        self.budget.admit(self.identity, "worker", MODEL, "fix", bridge=str(self.bridge))

    def test_dated_decimal_pricing_with_cache_ttls(self):
        tokens = dict(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_input_tokens=1_000_000,
            cache_creation_input_tokens=2_000_000,
            cache_creation_5m_input_tokens=1_000_000,
            cache_creation_1h_input_tokens=1_000_000,
        )
        self.assertEqual(price_usage(MODEL, tokens, AT), Decimal("37.2"))
        self.assertIsNone(price_usage("future-unknown-model", tokens, AT))
        self.assertIsNone(price_usage(MODEL, tokens, 1000))

    def test_duplicates_partial_messages_and_subagents_are_separate(self):
        self.journal.observe(response(output_tokens=100))
        self.journal.observe(response(output_tokens=100))
        self.journal.observe(response(output_tokens=200))
        self.journal.observe(response(session="child", output_tokens=100))
        saved = read_usage(self.journal.path)
        self.assertEqual(len(saved["messages"]), 2)
        self.assertEqual(sum(Decimal(m["cost"]) for m in saved["messages"].values()), Decimal("0.006"))
        self.assertNotIn("content", self.journal.path.read_text())

    def test_interleaved_partial_messages_retain_inflight_uncertainty(self):
        def event(session, inner):
            self.journal.observe(dict(type="stream_event", session_id=session, event=inner))

        event(
            "s",
            dict(
                type="message_start", message=dict(id="main", model=MODEL, usage=dict(input_tokens=10, output_tokens=0))
            ),
        )
        event(
            "child",
            dict(
                type="message_start",
                message=dict(id="child", model=MODEL, usage=dict(input_tokens=20, output_tokens=0)),
            ),
        )
        event("s", dict(type="message_stop"))
        self.assertTrue(read_usage(self.journal.path)["inflight"])
        event("child", dict(type="message_delta", usage=dict(output_tokens=100)))
        event("child", dict(type="message_stop"))
        self.assertFalse(read_usage(self.journal.path)["inflight"])
        self.assertEqual(read_usage(self.journal.path)["messages"]["child:child"]["usage"]["output_tokens"], 100)

    def test_subagents_share_cli_session_but_not_stream_lane(self):
        for parent, mid, tokens in ((None, "main", 10), ("tool-id", "child", 20)):
            self.journal.observe(
                dict(
                    type="stream_event",
                    session_id="same",
                    parent_tool_use_id=parent,
                    event=dict(
                        type="message_start",
                        message=dict(id=mid, model=MODEL, usage=dict(input_tokens=tokens, output_tokens=0)),
                    ),
                )
            )
        self.journal.observe(
            dict(
                type="stream_event", session_id="same", event=dict(type="message_delta", usage=dict(output_tokens=100))
            )
        )
        self.journal.observe(dict(type="stream_event", session_id="same", event=dict(type="message_stop")))
        saved = read_usage(self.journal.path)
        self.assertEqual(saved["messages"]["same:main"]["usage"]["output_tokens"], 100)
        self.assertEqual(saved["messages"]["same:child"]["usage"]["output_tokens"], 0)
        self.assertTrue(saved["inflight"])

    def test_empty_or_missing_counters_remain_incomplete(self):
        self.journal.observe(dict(type="assistant", session_id="s", message=dict(id="empty", model=MODEL)))
        self.journal.flush(closed=True)
        saved = read_usage(self.journal.path)
        self.assertIsNone(saved["messages"]["s:empty"]["cost"])
        self.assertFalse(saved["messages"]["s:empty"]["complete"])
        action = self.budget.recover_usage(self.identity)
        self.assertNotEqual(action["reserve_usd"], "0")
        self.assertEqual(self.budget.snapshot()["provisional"], [self.identity])

    def test_atomic_checkpoint_survives_sigkill_and_torn_journal_suffix(self):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import json, sys, time
from tauceti_worker.usage_accounting import UsageJournal
j=UsageJournal(sys.argv[1],sys.argv[2],model='claude-opus-5-5')
j.observe(dict(type='assistant',session_id='s',message=dict(id='m',model='claude-opus-5-5',usage=dict(output_tokens=100))))
print('ready',flush=True)
time.sleep(100)
""",
                str(self.root / "child"),
                self.identity,
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
        path = self.root / "child" / (self.identity + ".json")
        self.assertIsNotNone(read_usage(path))
        path.write_text('{"torn":')
        with path.with_suffix(".jsonl").open("a") as stream:
            stream.write('{"torn":')
        self.assertEqual(read_usage(path)["messages"]["s:m"]["usage"]["output_tokens"], 100)

    def test_missing_unknown_and_unsafe_evidence_never_settles_as_zero(self):
        self.journal.observe(
            dict(type="assistant", message=dict(id="m", model="unknown", usage=dict(output_tokens=100)))
        )
        result = self.budget.recover_usage(self.identity)
        self.assertNotEqual(result["reserve_usd"], "0")
        self.assertEqual(self.budget.snapshot()["provisional"], [self.identity])
        self.journal.path.unlink()
        self.journal.log.unlink()
        self.journal.path.symlink_to(self.budget.ledger)
        self.assertIsNone(read_usage(self.journal.path))

    def test_dry_run_does_not_change_ledger_or_key(self):
        self.journal.observe(response(output_tokens=100000))
        self.journal.flush(closed=True)
        (self.bridge / "key").write_text("not-a-real-key")
        before = self.budget.ledger.read_bytes()
        plan = self.budget.recover("", dry_run=True)
        self.assertEqual(plan["recovery_actions"][0]["estimated_usd"], "2.0")
        self.assertEqual(self.budget.ledger.read_bytes(), before)
        self.assertTrue((self.bridge / "key").exists())
        self.budget.recover("apply verified usage")
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 2)
        self.assertFalse((self.bridge / "key").exists())

    def test_complete_saved_usage_is_correctable_and_idempotent(self):
        self.journal.observe(response(output_tokens=100000))
        self.journal.flush(closed=True)
        self.budget.recover_usage(self.identity)
        self.budget.recover_usage(self.identity)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 2)
        self.budget.settle_receipt(dict(id=self.identity, totals={"s": "3"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.budget.settle_receipt(dict(id=self.identity, totals={"s": "3"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)

    def test_incomplete_prefix_is_charged_once_with_separate_reservation(self):
        self.journal.observe(response(output_tokens=100000))
        self.budget.recover_usage(self.identity)
        self.budget.recover_usage(self.identity)
        snapshot = self.budget.snapshot()
        self.assertEqual(Decimal(snapshot["spent"]), 2)
        self.assertGreaterEqual(Decimal(snapshot["pending"]), 2)
        self.budget.settle_receipt(dict(id=self.identity, totals={"s": "3"}, outcome="exit:0"))
        snapshot = self.budget.snapshot()
        self.assertEqual(Decimal(snapshot["spent"]), 3)
        self.assertEqual(Decimal(snapshot["balance"]), 97)
        self.assertEqual(Decimal(snapshot["pending"]), 0)

    def test_manual_reconciliation_replaces_an_estimated_prefix(self):
        self.journal.observe(response(output_tokens=100000))
        self.budget.recover_usage(self.identity)
        self.budget.reconcile(self.identity, "3", "verified provider receipt")
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 3)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)

    def test_replayed_response_on_resume_is_not_charged_twice(self):
        self.journal.observe(response(output_tokens=100000))
        self.journal.flush(closed=True)
        self.budget.recover_usage(self.identity)
        identity = "b" * 32
        self.budget.admit(identity, "worker", MODEL, "fix", bridge=str(self.bridge), resume="s")
        resumed = UsageJournal(self.root / "usage", identity, model=MODEL, clock=lambda: AT + 1)
        resumed.observe(response(output_tokens=100000))
        resumed.observe(response(mid="new", output_tokens=100000))
        resumed.flush(closed=True)
        self.budget.recover_usage(identity)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 4)
        self.budget.settle_receipt(dict(id=identity, totals={"s": "5"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)
        self.budget.settle_receipt(dict(id=self.identity, totals={"s": "1"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)
        with self.budget.locked():
            self.assertEqual(Decimal(self.budget.replay()["sessions"][identity]["cost"]), 4)

    def test_native_total_below_estimated_resume_baseline_corrects_estimate(self):
        self.journal.observe(response(output_tokens=100000))
        self.journal.flush(closed=True)
        self.budget.recover_usage(self.identity)
        identity = "b" * 32
        self.budget.admit(identity, "worker", MODEL, "fix", bridge=str(self.bridge), resume="s")
        self.budget.settle_receipt(dict(id=identity, totals={"s": "1"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 1)
        self.assertEqual(self.budget.snapshot()["unresolved"], [])

    def test_legacy_transcript_requires_receipt_provider_id_and_exactly_one_file(self):
        provider = "12345678-1234-1234-1234-123456789abc"
        project = self.root / "transcripts/project"
        project.mkdir(parents=True)
        native = project / (provider + ".jsonl")
        events = [
            dict(
                type="assistant",
                sessionId=provider,
                timestamp=datetime.fromtimestamp(at, UTC).isoformat(),
                message=dict(id=mid, model=MODEL, usage=dict(input_tokens=0, output_tokens=100000)),
            )
            for at, mid in ((AT - 100, "old"), (AT + 1, "new"))
        ]
        native.write_text("".join(json.dumps(e) + "\n" for e in events))
        self.journal.path.unlink()
        self.journal.log.unlink()
        receipt = self.bridge / "inbox" / (self.identity + ".receipt")
        receipt.write_text(
            json.dumps(
                dict(
                    id=self.identity,
                    totals=None,
                    lower_bound_totals={provider: "1"},
                    outcome="interrupted",
                    provisional=True,
                )
            )
        )
        with patch.dict(os.environ, TAUCETI_USAGE_TRANSCRIPT_ROOTS=str(project.parent)):
            before = self.budget.ledger.read_bytes()
            action = self.budget.recover_usage(self.identity, dry_run=True)
            self.assertEqual(action["source"], "receipt-linked-native-transcript")
            self.assertEqual(Decimal(action["estimated_usd"]), 2)
            self.assertEqual(before, self.budget.ledger.read_bytes())
            self.assertFalse(self.journal.path.exists())
            duplicate = project.parent / "copy"
            duplicate.mkdir()
            (duplicate / native.name).write_bytes(native.read_bytes())
            self.assertIsNone(self.budget.recover_usage(self.identity, dry_run=True))
            (duplicate / native.name).unlink()
            self.budget.recover_usage(self.identity)
            self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 2)
            self.assertGreater(Decimal(self.budget.snapshot()["pending"]), 0)
            saved = read_usage(self.journal.path)
            self.assertEqual(len(saved["messages"]), 1)
            self.assertNotIn("timestamp", saved["messages"][provider + ":new"])
            self.assertNotIn("content", self.journal.path.read_text())

    def test_live_owner_is_not_recovered(self):
        self.journal.observe(response(output_tokens=100000))
        with patch("tauceti_worker.budget.bridge_owner_alive", return_value=True):
            self.assertIsNone(self.budget.recover_usage(self.identity))
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 0)


if __name__ == "__main__":
    unittest.main()

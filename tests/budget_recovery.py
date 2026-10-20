"""Stopped-session policy, evidence priority, identity protection and audit reconciliation."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.budget import Budget, process_identity
from tauceti_worker.usage_accounting import UsageJournal

NOW = 1792404000
MODEL = "claude-opus-5-5"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.budget = Budget(self.root, clock=lambda: NOW)
        self.budget.configure(grant=10000)
        self.count = 0

    def session(
        self,
        *,
        cost=None,
        phase="fix",
        model=MODEL,
        duration=100,
        estimate="7",
        age=1000,
        provenance="native-reported",
        prefix=None,
        reserve=None,
        owner=None,
        resume=None,
    ):
        self.count += 1
        key = f"{self.count:032x}"
        bridge = self.root / "bridges" / key
        (bridge / "inbox").mkdir(parents=True)
        start = NOW - age
        event = dict(
            type="admit",
            id=key,
            worker="w",
            model=model,
            phase=phase,
            estimate=estimate,
            status="active",
            started=start,
            bridge=str(bridge),
            owner=owner,
            resume=resume,
        )
        with self.budget.locked():
            self.budget.append(self.budget.replay(), [event])
            events = []
            if cost is not None:
                events.append(
                    dict(
                        type="settle",
                        id=key,
                        cost=str(cost),
                        outcome="completed",
                        cost_provenance=provenance,
                        finished=start + duration if duration is not None else None,
                    )
                )
            elif duration is not None:
                events.append(dict(type="unresolved", id=key, outcome="stopped", finished=start + duration))
            if prefix is not None:
                events.append(
                    dict(
                        type="correction",
                        id=key,
                        cost=str(prefix),
                        delta=str(-Decimal(prefix)),
                        cost_provenance="token-priced-prefix",
                    )
                )
            if reserve is not None:
                events.append(
                    dict(
                        type="unresolved",
                        id=key,
                        provisional=True,
                        reserve=str(reserve),
                        lower_bound_cost="0",
                        lower_bound_totals={},
                        outcome="stopped",
                    )
                )
            if events:
                # A stopped invocation's terminal ledger timestamp records its duration.
                clock = self.budget.clock
                self.budget.clock = lambda: start + duration if duration is not None else NOW
                self.budget.append(self.budget.replay(), events)
                self.budget.clock = clock
        return key

    def saved(self, key, *, closed=False, inflight=False, output=100000, complete=True, provider="s", mid="m"):
        start = self.state(key)["started"]
        j = UsageJournal(self.root / "usage", key, model=MODEL, clock=lambda: start + 100)
        j.observe(
            dict(
                type="assistant",
                session_id=provider,
                message=dict(id=mid, model=MODEL, usage=dict(input_tokens=0, output_tokens=output)),
            )
        )
        j.value["inflight"] = inflight
        j.value["messages"][provider + ":" + mid]["complete"] = complete
        j.flush(closed=closed)
        return j

    def receipt(self, key, **values):
        bridge = Path(self.state(key)["bridge"])
        (bridge / "inbox" / (key + ".receipt")).write_text(json.dumps(dict(id=key, outcome="exit:0", **values)))

    def state(self, key):
        return self.budget.replay()["sessions"][key]

    def repair(self, dry=False):
        result = self.budget.recover("test deterministic policy", dry_run=dry)
        self.assertEqual(result["recovery_errors"], [])
        manifest = json.loads(Path(result["recovery_manifest"]).read_text())
        self.assertEqual(
            sum((Decimal(r["delta_usd"]) for r in manifest["invocations"]), Decimal(0)),
            Decimal(manifest["after_spent_usd"]) - Decimal(manifest["before_spent_usd"]),
        )
        self.assertEqual(
            sum((Decimal(r["released_reservation_usd"]) for r in manifest["invocations"]), Decimal(0)),
            Decimal(manifest["before_reserved_usd"]) - Decimal(manifest["after_reserved_usd"]),
        )
        return result, {r["id"]: r for r in manifest["invocations"]}

    def test_three_comparable_boundary_and_median_rate(self):
        self.session(cost=10, age=3000)
        self.session(cost=20, age=2000)
        low = self.session(age=1500, duration=200)
        _, rows = self.repair(True)
        self.assertEqual(rows[low]["confidence"], "prelaunch-estimate")
        third = self.session(cost=30, age=1800)
        _, rows = self.repair(True)
        self.assertEqual(rows[low]["confidence"], "historical-estimate")
        self.assertEqual(Decimal(rows[low]["final_charge_usd"]), 40)
        self.assertIn(third, rows[low]["audit_inputs"]["comparable_ids"])
        self.assertEqual(rows[low]["audit_inputs"]["tier"], "model-phase")

    def test_phase_fallback_filters_estimates_models_intervals_window_and_self(self):
        good = [self.session(cost=c, phase="review", age=4000 + c) for c in (10, 20, 30)]
        for kwargs in (
            dict(provenance="historical-estimate"),
            dict(model="other"),
            dict(duration=0),
            dict(duration=-1),
            dict(duration=None),
            dict(age=8 * 86400),
        ):
            self.session(cost=1000, **kwargs)
        target = self.session(duration=200)
        _, rows = self.repair(True)
        self.assertEqual(Decimal(rows[target]["final_charge_usd"]), 40)
        self.assertEqual(rows[target]["audit_inputs"]["tier"], "model")
        self.assertEqual(rows[target]["audit_inputs"]["comparable_ids"], good)

    def test_missing_invalid_duration_uses_saved_prelaunch(self):
        for c in (10, 20, 30):
            self.session(cost=c, age=5000 + c)
        for duration in (None, 0, -10, 2000):
            key = self.session(duration=duration, age=1000, estimate="9")
            _, rows = self.repair(True)
            self.assertEqual(rows[key]["confidence"], "prelaunch-estimate")
            self.assertEqual(Decimal(rows[key]["final_charge_usd"]), 9)

    def test_orphan_detection_time_is_not_a_recorded_duration(self):
        for c in (10, 20, 30):
            self.session(cost=c, age=5000 + c)
        key = self.session(duration=None, estimate="7")
        with self.budget.locked():
            self.budget.append(self.budget.replay(), [dict(type="unresolved", id=key, outcome="orphan detected")])
        _, rows = self.repair()
        self.assertEqual(rows[key]["confidence"], "prelaunch-estimate")
        self.assertEqual(Decimal(rows[key]["final_charge_usd"]), 7)
        self.assertIsNone(rows[key]["audit_inputs"]["duration"]["seconds"])

    def test_native_receipt_repair_does_not_count_downtime_as_duration(self):
        key = self.session(duration=None)
        self.receipt(key, totals={"s": "4"})
        self.repair()
        self.assertIsNone(self.state(key)["finished"])

    def test_recovered_native_comparables_available_to_later_invocation(self):
        good = [self.session(age=5000) for _ in range(3)]
        for i, key in enumerate(good):
            self.receipt(key, totals={f"s{i}": "20"})
        target = self.session(duration=200)
        _, rows = self.repair()
        self.assertEqual(rows[target]["confidence"], "historical-estimate")
        self.assertEqual(rows[target]["audit_inputs"]["comparable_ids"], good)
        self.assertEqual(Decimal(rows[target]["final_charge_usd"]), 40)

    def test_reservation_is_not_an_estimated_charge(self):
        key = self.session(estimate="7", reserve="221")
        _, rows = self.repair()
        self.assertEqual(Decimal(rows[key]["delta_usd"]), 7)
        self.assertEqual(Decimal(rows[key]["released_reservation_usd"]), 221)

    def test_existing_308_charge_and_221_reservation_are_separate(self):
        charged = self.session(prefix="308", reserve="308")
        stale = self.session(estimate="221", reserve="221")
        before = self.budget.ledger.read_bytes()
        _, preview = self.repair(True)
        self.assertEqual(self.budget.ledger.read_bytes(), before)
        self.assertEqual(Decimal(preview[charged]["delta_usd"]), 0)
        self.assertEqual(Decimal(preview[stale]["delta_usd"]), 221)
        _, applied = self.repair()
        self.assertEqual(applied, preview)
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 529)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)
        before = self.budget.ledger.read_bytes()
        _, again = self.repair()
        self.assertEqual(self.budget.ledger.read_bytes(), before)
        self.assertTrue(all(Decimal(r["delta_usd"]) == 0 for r in again.values()))

    def test_receipt_linked_transcript_is_incomplete_and_content_free(self):
        key = self.session(estimate="7")
        provider = "12345678-1234-1234-1234-123456789abc"
        self.receipt(key, totals=None, provisional=True, lower_bound_totals={provider: "1"})
        project = self.root / "transcripts/project"
        project.mkdir(parents=True)
        path = project / (provider + ".jsonl")
        event = dict(
            type="assistant",
            sessionId=provider,
            timestamp=datetime.fromtimestamp(NOW - 950, UTC).isoformat(),
            message=dict(
                id="m",
                model=MODEL,
                content="private-content-sentinel",
                usage=dict(input_tokens=0, output_tokens=100000),
            ),
        )
        path.write_text(json.dumps(event) + "\n")
        with patch.dict(os.environ, TAUCETI_USAGE_TRANSCRIPT_ROOTS=str(project.parent)):
            result, rows = self.repair(True)
            self.assertEqual(rows[key]["source"], "receipt-linked-native-transcript")
            self.assertEqual(rows[key]["confidence"], "prelaunch-estimate")
            self.assertEqual(Decimal(rows[key]["known_prefix_usd"]), 2)
            self.assertNotIn("private-content-sentinel", Path(result["recovery_manifest"]).read_text())
            copy = project.parent / "copy"
            copy.mkdir()
            (copy / path.name).write_bytes(path.read_bytes())
            _, rows = self.repair(True)
            self.assertEqual(rows[key]["source"], "native-prefix")
            self.assertEqual(Decimal(rows[key]["final_charge_usd"]), 7)
            (copy / path.name).unlink()
            self.repair()
        self.assertEqual(self.state(key)["totals"], {provider: "7"})

    def test_nonterminating_rate_reconciles_and_native_corrections_become_comparables(self):
        for _ in range(3):
            self.session(cost=1, duration=3, age=5000)
        key = self.session(duration=10)
        _, rows = self.repair()
        self.assertEqual(rows[key]["final_charge_usd"], "3.333333333333")
        self.receipt(key, totals={"s": "4"})
        self.repair()
        later = self.session(duration=10, age=500)
        _, rows = self.repair(True)
        self.assertIn(key, rows[later]["audit_inputs"]["comparable_ids"])

    def test_native_receipt_precedes_usage_and_historical_estimate(self):
        key = self.session(prefix="308", reserve="221")
        self.saved(key)
        self.receipt(key, totals={"s": "310"})
        _, rows = self.repair()
        self.assertEqual(rows[key]["source"], "native-receipt")
        self.assertEqual(Decimal(rows[key]["delta_usd"]), 2)
        self.assertEqual(Decimal(self.state(key)["cost"]), 310)

    def test_unclosed_no_inflight_and_closed_interrupted_use_policy(self):
        for closed, inflight, complete in ((False, False, True), (False, True, False), (True, False, False)):
            key = self.session(estimate="7")
            self.saved(key, closed=closed, inflight=inflight, complete=complete)
            _, rows = self.repair(True)
            self.assertEqual(rows[key]["confidence"], "prelaunch-estimate")
            self.assertFalse(rows[key]["audit_inputs"]["complete"])
            self.assertEqual(Decimal(rows[key]["final_charge_usd"]), 7)
        key = self.session()
        self.saved(key, closed=True)
        _, rows = self.repair(True)
        self.assertEqual(rows[key]["confidence"], "token-priced")
        self.assertEqual(Decimal(rows[key]["final_charge_usd"]), 2)

    def test_no_journal_nonzero_and_late_native_correction_idempotent(self):
        key = self.session(estimate="7")
        self.repair()
        self.assertEqual(Decimal(self.state(key)["cost"]), 7)
        self.receipt(key, totals={"s": "3"})
        self.repair()
        self.assertEqual(Decimal(self.state(key)["cost"]), 3)
        self.assertEqual(self.state(key)["cost_provenance"], "native-reported")
        before = self.budget.ledger.read_bytes()
        self.repair()
        self.assertEqual(before, self.budget.ledger.read_bytes())

    def test_historical_estimate_late_native_correction(self):
        for c in (10, 20, 30):
            self.session(cost=c, age=5000 + c)
        key = self.session()
        self.repair()
        self.assertEqual(self.state(key)["cost_provenance"], "historical-estimate")
        self.budget.settle_receipt(dict(id=key, totals={"s": "3"}, outcome="exit:0"))
        self.assertEqual(Decimal(self.state(key)["cost"]), 3)

    def test_duplicate_resumed_responses_and_late_cumulative_correction(self):
        first = self.session()
        self.saved(first, closed=True)
        self.repair()
        second = self.session(resume="s")
        j = self.saved(second, closed=True)
        j.observe(
            dict(
                type="assistant",
                session_id="s",
                message=dict(id="new", model=MODEL, usage=dict(input_tokens=0, output_tokens=100000)),
            )
        )
        j.flush(closed=True)
        self.repair()
        self.assertEqual(Decimal(self.state(second)["cost"]), 2)
        self.receipt(second, totals={"s": "5"})
        self.repair()
        self.receipt(first, totals={"s": "1"})
        self.repair()
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)
        self.assertEqual(Decimal(self.state(second)["cost"]), 4)

    def test_live_owner_and_heartbeat_protect_all_statuses_and_keys(self):
        key = self.session(owner=process_identity())
        bridge = Path(self.state(key)["bridge"])
        (bridge / "key").write_text("secret-key-sentinel")
        self.receipt(key, totals={"s": "5"})
        with patch("tauceti_worker.budget.bridge_owner_alive", return_value=True):
            before = self.budget.ledger.read_bytes()
            result, rows = self.repair()
        self.assertEqual(before, self.budget.ledger.read_bytes())
        self.assertEqual(rows[key]["confidence"], "live-untouched")
        self.assertTrue((bridge / "key").exists())
        self.assertNotIn("secret-key-sentinel", Path(result["recovery_manifest"]).read_text())
        (bridge / "outbox").mkdir()
        heartbeat = bridge / "outbox/heartbeat"
        heartbeat.touch()
        os.utime(heartbeat, (NOW, NOW))
        with patch("tauceti_worker.budget.bridge_owner_alive", return_value=False):
            self.repair()
        self.assertEqual(before, self.budget.ledger.read_bytes())

    def test_late_correction_does_not_modify_a_live_successor(self):
        first = self.session()
        self.saved(first, closed=True)
        self.repair()
        second = self.session(resume="s", owner=process_identity())
        self.saved(second, closed=True)
        with patch("tauceti_worker.budget.bridge_owner_alive", return_value=False):
            self.repair()
        # First estimate's late receipt would rebase the second estimate's totals.
        self.receipt(first, totals={"s": "1"})
        before = self.budget.ledger.read_bytes()
        result = self.budget.recover("live successor guard")
        self.assertTrue(result["recovery_errors"])
        self.assertEqual(before, self.budget.ledger.read_bytes())
        self.assertEqual(self.state(second)["totals"], {"s": "9.0"})

    def test_apply_rechecks_owner_after_planning(self):
        key = self.session()
        original = self.budget.ledger.read_bytes()
        from tauceti_worker.budget import BudgetError

        def identity(session, now):
            return dict(live=now > NOW, owner=None, owner_alive=now > NOW, heartbeat=None)

        with patch("tauceti_worker.budget_recovery.live_identity", side_effect=identity):
            ticks = iter([NOW, NOW + 1])
            self.budget.clock = lambda: next(ticks)
            with self.assertRaisesRegex(BudgetError, "became live"):
                self.budget.recover("identity changed")
        self.assertEqual(original, self.budget.ledger.read_bytes())
        self.assertEqual(self.state(key)["status"], "unresolved")

    def test_accrual_is_not_applied_twice(self):
        self.budget.configure(rate="3600")
        key = self.session()
        self.receipt(key, totals={"s": "5"})
        self.budget.clock = lambda: NOW + 100
        self.repair()
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 10095)


if __name__ == "__main__":
    unittest.main()

"""Incremental projection preserves financial evidence across process/crash boundaries."""

import concurrent.futures
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.budget import Budget, BudgetError


def settle(directory, invocation):
    Budget(Path(directory), clock=lambda: 1100).settle(invocation, "2")


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.budget = Budget(self.root, clock=lambda: 1000)
        self.budget.configure(grant=100, rate=150)

    def tearDown(self):
        self.tmp.cleanup()

    def equal_replay(self):
        with self.budget.locked():
            full = self.budget.replay()
            projected = self.budget.current()
            for key in full.keys() - {"samples", "sessions"}:
                self.assertEqual(projected[key], full[key], key)
            self.assertEqual(dict(projected["sessions"]), full["sessions"])
            for model in ["opus", "other"]:
                for phase in ["review", "fix", None]:
                    self.assertEqual(
                        self.budget.estimate(projected, model, phase), self.budget.estimate(full, model, phase)
                    )

    def test_full_parity_and_incremental_old_writer(self):
        self.budget.admit("first", "w", "opus", "fix")
        self.budget.settle("first", "3", totals={"provider": "3"})
        self.budget.admit("resume", "w", "opus", "review", resume="provider")
        self.budget.settle_receipt(dict(id="resume", totals={"provider": "8"}, outcome="done"))
        self.budget.reconcile("first", "4", "correction")
        self.equal_replay()
        before = self.budget.ledger.stat().st_size
        with self.budget.locked():
            state = self.budget.replay()
            # Simulate an older client or a crash after fsync, before projection.
            self.budget.append(state, [dict(type="grant", delta="17")])
        after = self.budget.ledger.stat().st_size
        with patch.object(self.budget, "records", wraps=self.budget.records) as records:
            self.budget.snapshot()
            self.budget.snapshot()
        self.assertEqual([len(call.args[0]) for call in records.call_args_list], [after - before, 0])
        self.equal_replay()

    def test_cache_loss_corruption_and_ledger_replacement_rebuild(self):
        self.budget.admit("a", "w", "opus", "review")
        self.budget.settle("a", "7")
        self.budget.snapshot()
        for corruption in [None, b"not sqlite"]:
            cache = self.root / "projection.sqlite"
            cache.unlink()
            if corruption:
                cache.write_bytes(corruption)
            self.equal_replay()
        old = self.budget.ledger.read_bytes()
        replacement = self.root / "replacement"
        replacement.write_bytes(old)
        replacement.replace(self.budget.ledger)
        self.equal_replay()
        self.assertEqual(old, self.budget.ledger.read_bytes())

    def test_query_failure_uses_ledger_immediately_and_next_operation_rebuilds(self):
        self.budget.admit("a", "w", "opus", "fix")
        self.budget.settle("a", 7)
        with self.budget.locked():
            state = self.budget.current()
            state["sessions"].db.close()  # Query error after load has already succeeded.
            self.assertEqual(self.budget.estimate(state, "opus", "fix")[1], Decimal(7))
            self.assertEqual(state["sessions"]["a"]["cost"], "7")
            self.assertFalse((self.root / "projection.sqlite").exists())
        self.equal_replay()
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 93)

    def test_lost_cached_session_during_tail_rebuilds_without_blocking_admission(self):
        self.budget.admit("a", "w", "opus", "fix")
        self.budget.snapshot()
        with sqlite3.connect(self.root / "projection.sqlite") as db:
            db.execute("DELETE FROM sessions WHERE id='a'")
        with self.budget.locked():
            state = self.budget.replay()
            self.budget.append(state, [dict(type="settle", id="a", cost="7", outcome="done")])
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 93)
        self.equal_replay()

    def test_invalid_cached_amount_rebuilds_from_ledger(self):
        for value in ["broken", "NaN"]:
            self.budget.snapshot()
            with sqlite3.connect(self.root / "projection.sqlite") as db:
                metadata = json.loads(db.execute("SELECT data FROM meta").fetchone()[0])
                metadata["state"]["balance"] = value
                db.execute("UPDATE meta SET data=?", (json.dumps(metadata),))
            self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 100)
            self.equal_replay()

    def test_cached_projection_does_not_hide_corruption_or_torn_tail(self):
        self.budget.snapshot()
        good = self.budget.ledger.read_bytes()
        for tail in [b'{"seq":', b'{"version":1,"seq":99,"events":[]}\n']:
            self.budget.ledger.write_bytes(good + tail)
            with self.assertRaises(BudgetError):
                self.budget.admit("must-not-start", "w", "opus", "fix")
        self.budget.ledger.write_bytes(good.replace(b'"version":1', b'"version":9'))
        with self.assertRaises(BudgetError):
            self.budget.snapshot()

    def test_failed_tail_projection_rolls_back_without_losing_committed_money(self):
        self.budget.snapshot()
        with self.budget.locked():
            state = self.budget.replay()
            self.budget.append(state, [dict(type="grant", delta="13")])
            state = self.budget.replay()
            self.budget.append(state, [dict(type="grant", delta="7")])
        original = self.budget.replay
        count = 0

        def interrupted(records=None, **kwargs):
            nonlocal count
            value = original(records, **kwargs)
            if kwargs.get("_samples") is False:
                count += 1
                if count == 2:
                    raise RuntimeError("process stopped before projection commit")
            return value

        with patch.object(self.budget, "replay", side_effect=interrupted), self.budget.locked():
            with self.assertRaises(RuntimeError):
                self.budget.current()
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 120)
        self.equal_replay()

    def test_multiple_admissions_in_one_record_keep_cumulative_receipt_order(self):
        with self.budget.locked():
            state = self.budget.replay()
            self.budget.append(
                state,
                [
                    dict(
                        type="admit",
                        id=key,
                        model="opus",
                        phase="fix",
                        status="active",
                        worker=key,
                        estimate="1",
                        started=1000,
                    )
                    for key in ["z", "a"]
                ],
            )
        self.budget.settle("z", "2", totals={"provider": "2"})
        self.budget.settle_receipt(dict(id="a", totals={"provider": "5"}, outcome="done"))
        self.equal_replay()
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 5)

    def test_cache_failure_falls_back_to_authoritative_ledger(self):
        with patch("tauceti_worker.budget_projection.Projection.load", side_effect=OSError("cache unavailable")):
            self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 100)
            self.budget.admit("a", "w", "opus", "fix")
            self.budget.settle("a", 4)
        self.assertEqual(Decimal(self.budget.snapshot()["balance"]), 96)
        self.equal_replay()

    def test_concurrent_settlement_and_reservation_serialization(self):
        for i in range(12):
            self.budget.admit(str(i), "w" + str(i), "opus" + str(i), "fix")
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            list(pool.map(settle, [str(self.root)] * 12, map(str, range(12))))
        self.equal_replay()
        self.assertEqual(Decimal(self.budget.snapshot()["spent"]), 24)
        self.assertEqual(Decimal(self.budget.snapshot()["pending"]), 0)
        self.assertEqual(os.stat(self.root / "projection.sqlite").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

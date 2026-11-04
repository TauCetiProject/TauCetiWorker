#!/usr/bin/env python3
"""The queue store (tauceti_queue.store): schema, transactions, row helpers, settings, journal."""

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue.store import Store, dumps, loads  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "queue.sqlite")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_schema_creates_every_table_in_wal_mode(self):
        names = {row["name"] for row in self.store.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in (
            "settings",
            "inputs",
            "batches",
            "members",
            "claims",
            "tasks",
            "submissions",
            "ci_attempts",
            "outbox",
            "journal",
        ):
            self.assertIn(table, names)
        self.assertEqual(self.store.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        # Reopening the same file is idempotent.
        again = Store(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.count("batches"), 0)

    def test_insert_update_rows_and_where_forms(self):
        first = self.store.insert("batches", repository="o/r", base="a", tip="a", state="open", created_at=1.0)
        second = self.store.insert("batches", repository="o/r", base="b", tip="b", state="head", created_at=2.0)
        self.assertEqual([r["id"] for r in self.store.rows("batches", order="id")], [first, second])
        self.assertEqual(self.store.row("batches", {"state": "head"})["id"], second)
        self.assertEqual(self.store.count("batches", {"state": ["open", "head"]}), 2)
        self.assertEqual(self.store.count("batches", {"state": []}), 0)
        self.assertEqual(self.store.count("batches", {"sealed_at": None}), 2)
        self.assertEqual(self.store.update("batches", {"id": first}, state="landed", landed_at=3.0), 1)
        self.assertEqual(self.store.row("batches", {"id": first})["state"], "landed")
        self.assertEqual(self.store.update("batches", {"id": first}), 0)
        self.assertEqual(self.store.rows("batches", {"state": "open"}), [])

    def test_transaction_rolls_back_on_error_and_nests(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.insert("journal", at=1.0, kind="x")
                with self.store.transaction():
                    self.store.insert("journal", at=2.0, kind="y")
                raise RuntimeError("abort")
        self.assertEqual(self.store.count("journal"), 0)
        with self.store.transaction():
            self.store.insert("journal", at=1.0, kind="x")
            with self.store.transaction():
                self.store.insert("journal", at=2.0, kind="y")
        self.assertEqual(self.store.count("journal"), 2)

    def test_outbox_key_is_unique(self):
        self.store.insert("outbox", kind="comment", key="k", payload="{}", next_attempt_at=0.0, created_at=0.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert("outbox", kind="comment", key="k", payload="{}", next_attempt_at=0.0, created_at=0.0)
        self.assertEqual(self.store.count("outbox"), 1)

    def test_settings_round_trip_json_values(self):
        self.assertIsNone(self.store.get_setting("missing"))
        self.assertEqual(self.store.get_setting("missing", 3), 3)
        self.store.set_setting("insertion_age_window", 16, "kim", 10.0)
        self.store.set_setting("inference_hold", {"since": 1.5, "last_error": "quota"}, "controller", 11.0)
        self.store.set_setting("insertion_age_window", 8, "kim", 12.0)
        self.assertEqual(self.store.get_setting("insertion_age_window"), 8)
        self.assertEqual(self.store.get_setting("inference_hold")["last_error"], "quota")
        settings = self.store.settings()
        self.assertEqual(settings["insertion_age_window"], {"value": 8, "actor": "kim", "updated_at": 12.0})
        self.store.set_setting("inference_hold", None, "controller", 13.0)
        self.assertIsNone(self.store.get_setting("inference_hold", "unset"))

    def test_journal_entries_decode_detail(self):
        self.store.journal(1.0, "sealed", 4, size=3, urgent=False)
        self.store.journal(2.0, "landed", 4, members=[1, 2])
        self.store.journal(3.0, "sealed", 5, size=1)
        self.assertEqual([e["detail"]["size"] for e in self.store.journal_entries("sealed")], [3, 1])
        self.assertEqual(self.store.journal_entries(batch_id=4)[1]["detail"], {"members": [1, 2]})
        self.assertEqual(loads(dumps({"b": 1, "a": [None]})), {"a": [None], "b": 1})
        self.assertEqual(loads(None, []), [])
        self.assertEqual(loads("", "d"), "d")

    def test_concurrent_writers_serialise(self):
        def writer(n):
            for i in range(20):
                with self.store.transaction():
                    self.store.insert("journal", at=float(i), kind=f"t{n}")

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.count("journal"), 80)


if __name__ == "__main__":
    unittest.main()

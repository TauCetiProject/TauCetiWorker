#!/usr/bin/env python3
"""Incremental accounting correctness, crash recovery and history-independent idle work."""

import contextlib
import importlib.util
import json
import random
import sqlite3
import sys
import time
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity, activity_cost_prepare, activity_costs
from tauceti_worker import activity_rollup as rollup
from tauceti_worker.budget_projection import Sessions
from tauceti_worker.usage_accounting import UsageJournal

NOW = 1_800_000_000
DAY = 86400


class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        rollup.install(self.db)

    def put(self, identity, start, finish, cost):
        with localcontext() as ctx:
            ctx.prec = 80
            rows = (
                [rollup.point(start, {"cost": cost})]
                if start == finish
                else rollup.interval(start, finish, {"cost": Decimal(cost) / Decimal(str(finish - start))})
            )
        rollup.replace(self.db, "fixture", identity, rollup.events([rollup.subject()], rows))

    def amount(self, now, since):
        _, rows = rollup.snapshot(self.db, now, {"window": now - since})
        return rows["window"].get("cost", Decimal(0))

    def test_inclusive_point_boundaries_and_continuous_intervals(self):
        self.put("point", NOW, NOW, "3")
        self.put("interval", NOW - 10, NOW + 10, "4")
        for now, since, expected in (
            (NOW, NOW, 3),
            (NOW, NOW - 10, 5),
            (NOW + 1, NOW, Decimal("3.2")),
            (NOW + 11, NOW + 1, Decimal("1.8")),
        ):
            self.assertEqual(self.amount(now, since), expected)

    def test_late_correction_and_removal_update_existing_prefixes(self):
        self.put("old", NOW - DAY, NOW - DAY, "10")
        self.assertEqual(self.amount(NOW, 0), 10)
        self.put("old", NOW - DAY, NOW - DAY, "4")
        self.assertEqual(self.amount(NOW, 0), 4)
        self.assertEqual(self.amount(NOW, NOW - 3600), 0)
        rollup.replace(self.db, "fixture", "old", [])
        self.assertEqual(self.amount(NOW + 60, 0), 0)

    def test_future_events_and_forward_cursors_match_direct_interval_reference(self):
        rng = random.Random(71)
        facts = []
        for i in range(150):
            start = NOW + rng.randrange(-2 * DAY, 1000)
            finish = start + (rng.randrange(1, 4 * DAY) if i % 3 else 0)
            cost = str(Decimal(rng.randrange(1, 1000)) / 100)
            facts.append((start, finish, Decimal(cost)))
            self.put(str(i), start, finish, cost)
        for now in (NOW, NOW + 60, NOW + 3600, NOW + 3 * DAY):
            for width in (3600, 3 * 3600, DAY, 7 * DAY):
                since = now - width
                expected = sum(
                    (
                        cost
                        if start == finish and since <= start <= now
                        else Decimal(0)
                        if start == finish
                        else cost * max(0, min(finish, now) - max(start, since)) / (finish - start)
                        for start, finish, cost in facts
                    ),
                    Decimal(0),
                )
                actual = self.amount(now, since)
                self.assertLess(abs(actual - expected), Decimal("1e-20"))

    def test_new_installation_advances_clamped_window_cursors_before_they_are_needed(self):
        self.put("first", NOW, NOW, "1")
        rollup.snapshot(self.db, NOW + 1, {"1h": 3600}, cutoff=NOW)
        queries = []
        self.db.set_trace_callback(queries.append)
        rollup.snapshot(self.db, NOW + 3601, {"1h": 3600}, cutoff=NOW)
        self.db.set_trace_callback(None)
        event_queries = [q for q in queries if "FROM report_events WHERE at" in q]
        self.assertTrue(event_queries)
        self.assertTrue(all("AND at" in q for q in event_queries), event_queries)


class PreparationTests(unittest.TestCase):
    def fixture(self):
        spec = importlib.util.spec_from_file_location("cost_fixture", Path(__file__).with_name("activity_costs.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = module.CostTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.record(fixture.now - 600, dict(type="settle", id="a" * 32, cost="40", outcome="completed"))
        fixture.write_ledger()
        fixture.summary()
        return fixture

    def funding(self, db):
        return json.loads(db.execute("SELECT value FROM meta WHERE key='activity_funding'").fetchone()[0])

    def test_idle_prepare_never_enumerates_or_loads_settled_sessions(self):
        fixture = self.fixture()
        with contextlib.closing(activity._connect()) as db, db:
            with (
                patch.object(Sessions, "__iter__", side_effect=AssertionError("enumerated history")),
                patch.object(Sessions, "__getitem__", side_effect=AssertionError("loaded historical session")),
                patch.object(
                    activity_cost_prepare, "stored_usage", side_effect=AssertionError("decoded historical usage")
                ),
            ):
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
                activity._summary(db, fixture.now + 61, "scope", "machine")
            metrics = self.funding(db)["preparation_metrics"]
            for key in (
                "processed_invocations",
                "response_owners_reallocated",
                "usage_metadata_checks",
                "usage_decodes",
                "replayed_bytes",
            ):
                self.assertEqual(metrics[key], 0, (key, metrics))

    def test_single_historical_correction_loads_only_that_invocation(self):
        fixture = self.fixture()
        fixture.record(fixture.now, dict(type="correction", id="old", cost="15", delta="-5"))
        fixture.write_ledger()
        with contextlib.closing(activity._connect()) as db, db:
            with patch.object(activity_cost_prepare, "stored_usage", wraps=activity_cost_prepare.stored_usage) as reads:
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
            self.assertEqual({call.args[1] for call in reads.call_args_list}, {"old"})
            metrics = self.funding(db)["preparation_metrics"]
            self.assertEqual(metrics["processed_invocations"], 1)
            summary = activity._summary(db, fixture.now + 61, "scope", "machine")
            self.assertEqual(Decimal(summary["costs"]["windows"]["total"]["estimated_usd"]), 105)
            self.assertEqual(Decimal(summary["costs"]["windows"]["7d"]["estimated_usd"]), 90)

    def test_projection_replay_cannot_acknowledge_rolled_back_cost_changes(self):
        fixture = self.fixture()
        fixture.record(fixture.now, dict(type="correction", id="old", cost="15", delta="-5"))
        fixture.write_ledger()
        with contextlib.closing(activity._connect()) as db:
            with self.assertRaisesRegex(RuntimeError, "crash"):
                with db, patch.object(activity_cost_prepare, "allocate", side_effect=RuntimeError("crash")):
                    activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
            with db:
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
                self.assertFalse(self.funding(db)["preparation_metrics"]["rebuilt"])
                self.assertEqual(self.funding(db)["preparation_metrics"]["processed_invocations"], 1)
                result = activity._summary(db, fixture.now + 61, "scope", "machine")
                self.assertEqual(Decimal(result["costs"]["windows"]["total"]["estimated_usd"]), 105)

    def test_one_bad_invocation_does_not_pin_the_cursor_or_reprocess_successful_updates(self):
        fixture = self.fixture()
        fixture.record(fixture.now, dict(type="correction", id="old", cost="15", delta="-5"))
        fixture.session("new-good", 10, "author", "7", settled=True)
        fixture.write_ledger()
        allocate = activity_cost_prepare.allocate

        def fail_old(db, budget, session, now, rates):
            if session["id"] == "old":
                raise ValueError("broken evidence")
            return allocate(db, budget, session, now, rates)

        with contextlib.closing(activity._connect()) as db, db:
            with patch.object(activity_cost_prepare, "allocate", side_effect=fail_old):
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
            self.assertEqual(db.execute("SELECT id FROM cost_failed").fetchone()[0], "old")
            self.assertEqual(db.execute("SELECT cost FROM conversation_costs WHERE id='new-good'").fetchone()[0], "7")
            activity_costs.prepare(db, fixture.now + 62, lambda worker: activity._worker_scope(db, worker))
            self.assertEqual(self.funding(db)["preparation_metrics"]["processed_invocations"], 0)
            self.assertIsNone(
                activity_costs.summarize(db, fixture.now + 62, fixture.now - 10 * DAY)["spending"]["total"]["spent_usd"]
            )
            activity_costs.prepare(db, fixture.now + 122, lambda worker: activity._worker_scope(db, worker))
            self.assertEqual(self.funding(db)["preparation_metrics"]["processed_invocations"], 1)
            self.assertIsNone(db.execute("SELECT id FROM cost_failed").fetchone())
            result = activity._summary(db, fixture.now + 122, "scope", "machine")
            self.assertEqual(Decimal(result["costs"]["windows"]["total"]["estimated_usd"]), 112)

    def test_replacement_epoch_is_committed_with_projection_rebuild_even_if_the_process_crashes(self):
        from tauceti_worker.activity_ledger import ReportingProjection

        fixture = self.fixture()
        fixture.records = []
        fixture.record(fixture.now - 30, dict(type="grant", delta="1000"))
        fixture.session("replacement", 10, "author", "7", settled=True)
        fixture.write_ledger()
        original = ReportingProjection.load

        def rebuild_then_crash(projection):
            original(projection)
            raise RuntimeError("crash after projection commit")

        with contextlib.closing(activity._connect()) as db:
            with self.assertRaisesRegex(RuntimeError, "projection commit"):
                with db, patch.object(ReportingProjection, "load", rebuild_then_crash):
                    activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
            with db:
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
                self.assertTrue(self.funding(db)["preparation_metrics"]["rebuilt"])
                self.assertEqual([row[0] for row in db.execute("SELECT id FROM conversation_costs")], ["replacement"])
                result = activity._summary(db, fixture.now + 61, "scope", "machine")
                self.assertEqual(Decimal(result["costs"]["windows"]["total"]["estimated_usd"]), 7)

    def test_report_frontiers_persist_across_connections_without_reseeding_history(self):
        fixture = self.fixture()
        fixture.summary()
        with contextlib.closing(activity._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM report_dirty").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT count(*) FROM report_frontiers").fetchone()[0], 0)
        with contextlib.closing(activity._connect()) as db, db:
            queries = []
            db.set_trace_callback(queries.append)
            activity._summary(db, fixture.now + 61, "scope", "machine")
            db.set_trace_callback(None)
            reads = [query for query in queries if "FROM report_events WHERE at" in query]
            self.assertTrue(reads)
            self.assertTrue(all("AND at" in query for query in reads), reads)

    def test_new_earlier_response_owner_reallocates_only_linked_invocations(self):
        fixture = self.fixture()
        first, later, earliest = "1" * 32, "2" * 32, "3" * 32
        for identity, age, messages, cost in (
            (first, 2000, [("shared", 100000), ("first-only", 200000)], "5"),
            (later, 1000, [("shared", 100000), ("later-only", 300000)], "7"),
        ):
            journal = UsageJournal(
                fixture.root / "budget/usage",
                identity,
                model="claude-opus-5-5",
                clock=lambda age=age: fixture.now - age,
            )
            for message, output in messages:
                journal.observe(
                    dict(
                        type="assistant",
                        session_id="provider",
                        message=dict(
                            id=message, model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=output)
                        ),
                    )
                )
            journal.flush(closed=True)
            fixture.session(identity, age, "author", cost, settled=True)
        fixture.write_ledger()
        fixture.summary()
        journal = UsageJournal(
            fixture.root / "budget/usage", earliest, model="claude-opus-5-5", clock=lambda: fixture.now - 3000
        )
        journal.observe(
            dict(
                type="assistant",
                session_id="provider",
                message=dict(id="shared", model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=100000)),
            )
        )
        journal.flush(closed=True)
        fixture.session(earliest, 3000, "author", "2", settled=True)
        fixture.write_ledger()
        with contextlib.closing(activity._connect()) as db, db:
            with patch.object(activity_cost_prepare, "allocate", wraps=activity_cost_prepare.allocate) as allocated:
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
            self.assertEqual({call.args[2]["id"] for call in allocated.call_args_list}, {first, earliest})
            first_rows = db.execute("SELECT cost FROM cost_accruals WHERE id=?", (first,)).fetchall()
            self.assertEqual([Decimal(row[0]) for row in first_rows], [Decimal(5)])
            result = activity._summary(db, fixture.now + 61, "scope", "machine")
            self.assertEqual(Decimal(result["costs"]["windows"]["total"]["estimated_usd"]), 114)

    def test_idle_sql_work_is_bounded_at_1x_10x_and_100x_history(self):
        counts = []
        fixture = self.fixture()
        previous = 0
        for population in (100, 1000, 10000):
            for i in range(previous, population):
                fixture.session(f"history-{i}", 2 * DAY, "author", "1", settled=True)
            fixture.write_ledger()
            fixture.summary()
            with contextlib.closing(activity._connect()) as db, db:
                activity_costs.prepare(db, fixture.now + 1, lambda worker: activity._worker_scope(db, worker))
                activity._summary(db, fixture.now + 1, "scope", "machine")
                steps = [0]
                current, by_query = [""], {}

                def trace(query, current=current):
                    current[0] = query

                def progress(steps=steps, by_query=by_query, current=current):
                    steps[0] += 100
                    by_query[current[0]] = by_query.get(current[0], 0) + 100
                    return 0

                db.set_trace_callback(trace)
                db.set_progress_handler(progress, 100)
                start = time.monotonic()
                activity_costs.prepare(db, fixture.now + 61, lambda worker: activity._worker_scope(db, worker))
                result = activity._summary(db, fixture.now + 61, "scope", "machine")
                elapsed = time.monotonic() - start
                db.set_progress_handler(None, 0)
                db.set_trace_callback(None)
                metrics = self.funding(db)["preparation_metrics"]
                self.assertEqual(metrics["processed_invocations"], 0)
                self.assertEqual(Decimal(result["costs"]["windows"]["total"]["estimated_usd"]), 100 + population)
                counts.append(steps[0])
                print(
                    json.dumps(
                        dict(
                            history=population,
                            idle_sql_steps=steps[0],
                            seconds=elapsed,
                            top_queries=sorted(by_query.items(), key=lambda p: p[1], reverse=True)[:3],
                        )
                    )
                )
            previous = population
        self.assertLessEqual(max(counts), counts[0] * 2 + 5000, counts)


if __name__ == "__main__":
    unittest.main()

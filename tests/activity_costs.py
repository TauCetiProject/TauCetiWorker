#!/usr/bin/env python3
"""Pooled costs, initiated-PR cohorts, half-credit open output and cached readers."""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity as a
from tauceti_worker import activity_costs, activity_summary, fleet_events
from tauceti_worker.usage_accounting import UsageJournal

DAY = 86400


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()
        env = patch.dict(
            os.environ,
            {
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_BUDGET_DIR": str(self.root / "budget"),
                "TAUCETI_ACTIVITY_CI_DB": str(self.root / "ci.sqlite"),
                "TAUCETI_FLEET_MACHINE_ID": "machine",
                "TAUCETI_WORKER_ID": "author",
                "TAUCETI_FLEET_EVENTS": str(self.root / "worker-events.jsonl"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        with patch.object(a.time, "time", return_value=self.now - 10 * DAY):
            a.emit("round_started", round_id="initial")
            a.emit("round_finished", round_id="initial", round_type="fix", outcome="completed")
        a.emit("round_started", round_id="reviewer", worker="reviewer")
        a.emit("round_finished", round_id="reviewer", worker="reviewer", round_type="review", outcome="completed")
        self.records = []
        self.record(self.now - 10 * DAY, dict(type="grant", delta="1000"))
        self.session("old", 8 * DAY, "author", "10", settled=True)
        self.session("review", DAY - 60, "reviewer", "20", settled=True, phase="review")
        self.session("fix", DAY - 60, "author", "30", settled=True)
        self.session("a" * 32, 60, "reviewer", "40", settled=False, phase="review")
        journal = UsageJournal(
            self.root / "budget/usage", "a" * 32, model="claude-opus-5-5", clock=lambda: self.now - 60
        )
        journal.observe(
            dict(
                type="assistant",
                session_id="active",
                message=dict(id="r1", model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=2_000_000)),
            )
        )
        self.session("external", DAY, "not-in-fleet", "99", settled=True)
        self.write_ledger()
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            for n, age, state, loc, landed in (
                (1, 8 * DAY, "MERGED", 100, True),
                (2, 2 * DAY, "MERGED", 200, True),
                (3, DAY, "OPEN", 300, False),
                (4, 8 * DAY, "OPEN", 400, False),
                (5, DAY, "CLOSED", 10000, False),
            ):
                db.execute(
                    "INSERT INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "org/repo",
                        n,
                        "author",
                        "machine",
                        self.now - age,
                        state,
                        self.now - 10 if landed else None,
                        self.now,
                        self.now,
                        "head",
                        None,
                    ),
                )
                db.execute("INSERT INTO pr_loc VALUES (?,?,?,?,?,?)", ("org/repo", n, "head", loc, 0, self.now))
        a.emit("pr_activity", repository="org/repo", pr=99, activity="review", worker="reviewer")

    def record(self, at, event):
        self.records.append(dict(version=1, seq=len(self.records) + 1, timestamp=at, events=[event]))

    def session(self, key, age, worker, cost, *, settled, phase="fix"):
        start = self.now - age
        self.record(
            start,
            dict(
                type="admit",
                id=key,
                worker=worker,
                model="m",
                phase=phase,
                estimate=cost,
                status="active",
                started=start,
            ),
        )
        if settled:
            self.record(start + 1, dict(type="settle", id=key, cost=cost, outcome="completed"))

    def write_ledger(self):
        p = self.root / "budget/events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in self.records))

    def summary(self):
        a.prepare(now=self.now + 1)
        return a.cached(group="machine")["summary"]

    def test_all_fleet_costs_include_reviews_but_only_initiated_output_counts(self):
        s = self.summary()
        rows = s["costs"]["windows"]
        self.assertEqual(set(rows), {"total", "7d"})
        self.assertEqual(Decimal(rows["total"]["spent_usd"]), Decimal(60))
        self.assertEqual(Decimal(rows["total"]["active_accrued_usd"]), Decimal(40))
        self.assertEqual(Decimal(rows["total"]["weighted_loc"]), Decimal(650))
        self.assertEqual(Decimal(rows["total"]["usd_per_loc"]), Decimal(100) / 650)
        # An old PR landing yesterday does not enter the seven-day initiation cohort.
        self.assertEqual(Decimal(rows["7d"]["estimated_usd"]), Decimal(90))
        self.assertEqual(Decimal(rows["7d"]["weighted_loc"]), Decimal(350))
        self.assertEqual(Decimal(rows["7d"]["usd_per_loc"]), Decimal(90) / 350)
        self.assertEqual(s["pr_windows"]["total"]["reviews"], 1)
        self.assertEqual(s["pr_windows"]["total"]["assisted"], 1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=s))
        self.assertIn("All time", output.getvalue())
        self.assertIn("7 days", output.getvalue())
        self.assertIn("$0.1538", output.getvalue())
        self.assertIn("$0.2571", output.getvalue())

    def test_odd_open_LOC_gets_exactly_half_credit(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE pr_loc SET added=301 WHERE number=3")
        row = self.summary()["costs"]["windows"]["7d"]
        self.assertEqual(Decimal(row["weighted_loc"]), Decimal("350.5"))
        self.assertEqual(Decimal(row["usd_per_loc"]), Decimal(90) / Decimal("350.5"))

    def test_unchanged_usage_is_cached_and_new_checkpoint_invalidates_it(self):
        self.summary()
        with patch.object(activity_costs, "session_usage", side_effect=AssertionError("unchanged journal reread")):
            self.assertEqual(Decimal(self.summary()["costs"]["windows"]["total"]["estimated_usd"]), 100)
        journal = UsageJournal(
            self.root / "budget/usage", "b" * 32, model="claude-opus-5-5", clock=lambda: self.now - 1
        )
        journal.observe(
            dict(
                type="assistant",
                session_id="new",
                message=dict(id="new", model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=100000)),
            )
        )
        self.session("b" * 32, 60, "author", "500", settled=False)
        self.write_ledger()
        self.assertEqual(Decimal(self.summary()["costs"]["windows"]["total"]["estimated_usd"]), 102)

    def test_timestamped_running_usage_and_final_correction_do_not_spike(self):
        key = "b" * 32
        self.session(key, 5 * 3600, "author", "500", settled=False)
        self.write_ledger()
        journal = UsageJournal(
            self.root / "budget/usage", key, model="claude-opus-5-5", clock=lambda: self.now - 4 * 3600
        )
        journal.observe(
            dict(
                type="assistant",
                session_id="session",
                message=dict(id="early", model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=200000)),
            )
        )
        journal.clock = lambda: self.now - 3600
        journal.observe(
            dict(
                type="assistant",
                session_id="session",
                message=dict(id="late", model="claude-opus-5-5", usage=dict(input_tokens=0, output_tokens=300000)),
            )
        )
        before = self.summary()["costs"]
        self.assertEqual(Decimal(before["spending"]["3h"]["spent_usd"]), 46)  # existing active 40 plus recent 6
        self.assertEqual(Decimal(before["spending"]["total"]["spent_usd"]), 110)
        self.assertEqual(
            Decimal(before["windows"]["7d"]["estimated_usd"]), Decimal(before["spending"]["7d"]["spent_usd"])
        )
        self.assertGreater(Decimal(before["funding"]["pending"]), 500)
        self.record(self.now, dict(type="settle", id=key, cost="20", outcome="completed"))
        self.write_ledger()
        final = self.summary()["costs"]
        self.assertEqual(Decimal(final["spending"]["3h"]["spent_usd"]), 52)  # recent share is 12, not the full 20
        self.assertEqual(Decimal(final["spending"]["total"]["spent_usd"]), 120)
        with contextlib.closing(a._connect()) as db:
            db.execute("DELETE FROM cost_accruals WHERE id!=?", (key,))
            window = activity_costs.summarize(db, self.now, self.now - 1800, "scope", "machine")
            self.assertEqual(Decimal(window["spending"]["3h"]["spent_usd"]), 0)

    def test_hourly_spending_includes_running_usage_and_excludes_reservations(self):
        self.session("recent", 3600, "author", "12", settled=True)
        self.write_ledger()
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE rounds SET started=finished-1800 WHERE id='reviewer'")
        s = self.summary()
        rows = s["costs"]["spending"]
        self.assertEqual(set(rows), {"3h", "24h", "7d", "total"})
        for window, spent, seconds in (
            ("3h", 52, 10800),
            ("24h", 102, DAY),
            ("7d", 102, 7 * DAY),
            ("total", 112, 10 * DAY + 1),
        ):
            self.assertEqual(Decimal(rows[window]["spent_usd"]), spent)
            self.assertEqual(rows[window]["covered_seconds"], seconds)
            self.assertEqual(Decimal(rows[window]["usd_per_hour"]), Decimal(spent) * 3600 / seconds)
            self.assertEqual(
                Decimal(rows[window]["usd_per_worker_hour"]),
                Decimal(spent) * 3600 / Decimal(str(rows[window]["worker_seconds"])),
            )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=s))
        self.assertIn("Spending (recorded usage)", output.getvalue())
        self.assertIn("API spend ($/elapsed hour)", output.getvalue())
        self.assertIn("Worker-hours recorded", output.getvalue())
        self.assertIn("$17.33", output.getvalue())

    def test_new_recording_uses_elapsed_time_not_the_entire_trailing_window(self):
        self.session("recent", 900, "author", "12", settled=True)
        self.write_ledger()
        self.summary()
        with contextlib.closing(a._connect()) as db:
            value = activity_costs.summarize(db, self.now, self.now - 1800, "scope", "machine")
            for row in value["spending"].values():
                self.assertEqual(row["covered_seconds"], 1800)
                self.assertEqual(Decimal(row["usd_per_hour"]), 104)
            zero = activity_costs.summarize(db, self.now, self.now, "scope", "machine")
            self.assertTrue(all(r["usd_per_hour"] is None for r in zero["spending"].values()))

    def test_worker_hours_sum_clipped_rounds_and_exclude_waiting_or_unknown_ends(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("DELETE FROM rounds")
            for key, start, end, outcome, scope in (
                ("overlap", -14400, -7200, "completed", "machine"),
                ("done", -1800, -600, "completed", "machine"),
                ("live", -900, None, None, "machine"),
                ("waiting", -3600, None, "waiting", "machine"),
                ("unknown-end", -7200, None, "interrupted-unfinished", "machine"),
                ("interrupted", -1200, -1000, "interrupted", "machine"),
                ("survey", -300, -200, "completed", "machine"),
                ("other-scope", -10000, -1, "completed", "local"),
                ("future", 10, None, None, "machine"),
            ):
                db.execute(
                    "INSERT INTO rounds(id,worker,scope,kind,started,finished,outcome) VALUES (?,?,?,'fix',?,?,?)",
                    (key, key, scope, self.now + start, self.now + end if end is not None else None, outcome),
                )
            rows = activity_costs.summarize(db, self.now, self.now - 10 * DAY, "scope", "machine")["spending"]
        self.assertEqual(rows["3h"]["worker_seconds"], 6000)
        self.assertEqual(rows["24h"]["worker_seconds"], 9600)
        self.assertEqual(rows["total"]["worker_seconds"], 9600)
        self.assertEqual(Decimal(rows["total"]["usd_per_worker_hour"]), Decimal(100) * 3600 / 9600)
        with contextlib.closing(a._connect()) as db, db:
            db.execute("DELETE FROM rounds")
            rows = activity_costs.summarize(db, self.now, self.now - 10 * DAY, "scope", "machine")["spending"]
            self.assertTrue(all(row["usd_per_worker_hour"] is None for row in rows.values()))

    def test_fleet_hourly_rate_pools_spending_over_wall_time_not_machine_hours(self):
        s = self.summary()
        other = json.loads(json.dumps(s))
        for row in other["costs"]["spending"].values():
            row.update(spent_usd="30", covered_seconds=3600, worker_seconds=7200, usd_per_hour="30")
        values = [dict(available=True, summary=s), dict(available=True, summary=other)]
        combined = activity_summary.combine(values)["summary"]["costs"]["spending"]
        self.assertEqual(Decimal(combined["3h"]["usd_per_hour"]), Decimal(70) / 3)
        self.assertEqual(combined["3h"]["worker_seconds"], s["costs"]["spending"]["3h"]["worker_seconds"] + 7200)
        self.assertEqual(
            Decimal(combined["3h"]["usd_per_worker_hour"]),
            Decimal(70) * 3600 / Decimal(str(combined["3h"]["worker_seconds"])),
        )
        self.assertEqual(Decimal(combined["total"]["usd_per_hour"]), Decimal(130) * 3600 / (10 * DAY + 1))
        # Missing/older hosts cannot silently turn unavailable spending into zero.
        other["costs"].pop("spending")
        missing = activity_summary.combine(values)["summary"]["costs"]["spending"]
        self.assertTrue(all(r["usd_per_hour"] is None for r in missing.values()))

    def test_different_accounting_versions_are_not_silently_pooled(self):
        modern = self.summary()
        legacy = json.loads(json.dumps(modern))
        legacy["costs"]["accounting_version"] = 1
        for row in legacy["costs"]["windows"].values():
            row["pending_usd"] = row.pop("active_accrued_usd")
        combined = activity_summary.combine(
            [dict(available=True, summary=modern), dict(available=True, summary=legacy)]
        )
        self.assertTrue(any("accounting versions" in error for error in combined["errors"]))
        self.assertIsNone(combined["summary"]["costs"]["spending"]["3h"]["usd_per_hour"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            activity_summary.display(dict(available=True, summary=legacy), [])
        self.assertIn("API cost: active estimates", output.getvalue())
        self.assertNotIn("Reservations excluded", output.getvalue())

    def test_reconciliations_restate_the_original_cost_window(self):
        self.record(self.now, dict(type="correction", id="old", cost="15", delta="-5"))
        self.write_ledger()
        rows = self.summary()["costs"]["windows"]
        self.assertEqual(Decimal(rows["total"]["estimated_usd"]), 105)
        self.assertEqual(Decimal(rows["7d"]["estimated_usd"]), 90)
        spending = self.summary()["costs"]["spending"]
        self.assertEqual(Decimal(spending["7d"]["spent_usd"]), 90)
        self.assertEqual(Decimal(spending["total"]["spent_usd"]), 105)

    def test_missing_or_changed_head_LOC_is_extrapolated_with_half_weight_open_prs(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET head='new-head' WHERE number=3")
        rows = self.summary()["costs"]["windows"]
        self.assertEqual(Decimal(rows["total"]["measured_weighted_loc"]), 500)
        self.assertEqual(Decimal(rows["total"]["weighted_loc"]), 600)
        self.assertEqual(Decimal(rows["total"]["usd_per_loc"]), Decimal(100) / 600)
        self.assertEqual(Decimal(rows["7d"]["weighted_loc"]), 300)
        self.assertEqual(Decimal(rows["7d"]["usd_per_loc"]), Decimal("0.3"))
        self.assertEqual(rows["7d"]["missing_loc_prs"], 1)
        self.assertTrue(rows["7d"]["loc_estimated"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=self.summary()))
        self.assertIn("~$0.1667", output.getvalue())
        self.assertIn("~$0.3000", output.getvalue())
        self.assertIn("~ Missing LOC estimated", output.getvalue())

    def test_missing_all_LOC_keeps_rate_visible_without_inventing_output(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("DELETE FROM pr_loc")
        s = self.summary()
        for row in s["costs"]["windows"].values():
            self.assertEqual(Decimal(row["weighted_loc"]), 0)
            self.assertEqual(row["usd_per_loc"], "Infinity")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=s))
        rate = next(line for line in output.getvalue().splitlines() if line.startswith("Estimated $ / weighted LOC"))
        self.assertEqual(rate.count("~$∞"), 2)

    def test_unknown_pr_state_contributes_zero_without_hiding_rate(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET state=NULL WHERE number=3")
        row = self.summary()["costs"]["windows"]["7d"]
        self.assertTrue(row["loc_estimated"])
        self.assertEqual(Decimal(row["weighted_loc"]), 200)
        self.assertEqual(Decimal(row["usd_per_loc"]), Decimal("0.45"))

    def test_partial_fleet_estimate_pools_measurements_before_dividing(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET head='new-head' WHERE number=3")
        s = self.summary()
        other = json.loads(json.dumps(s))
        for row in other["costs"]["windows"].values():
            row.update(
                estimated_usd="20",
                landed_loc_added=200,
                open_loc_added=0,
                landed_prs=1,
                open_prs=0,
                missing_loc_prs=0,
                known_loc_prs=1,
                measured_pr_weight=1,
            )
        combined = activity_summary.combine([dict(available=True, summary=s), dict(available=True, summary=other)])
        row = combined["summary"]["costs"]["windows"]["total"]
        self.assertEqual(Decimal(row["weighted_loc"]), 800)  # 700 measured LOC * 4 / 3.5
        self.assertEqual(Decimal(row["usd_per_loc"]), Decimal("0.15"))
        self.assertTrue(row["loc_estimated"])

    def test_old_prepared_summary_gets_numeric_fallback_without_refresh(self):
        s = self.summary()
        for row in s["costs"]["windows"].values():
            for key in ("loc_estimated", "measured_pr_weight"):
                row.pop(key)
            row.update(usd_per_loc=None, missing_loc_prs=1, known_loc_prs=3)
        with (
            patch.object(a, "_connect", side_effect=AssertionError("SQL in status")),
            patch.object(a, "observe", side_effect=AssertionError("network in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            a.display(dict(available=True, summary=s))
        rate = next(line for line in output.getvalue().splitlines() if line.startswith("Estimated $ / weighted LOC"))
        self.assertEqual(rate.count("~$"), 2)
        self.assertNotIn("—", rate)

    def test_zero_output_and_failed_ledger_are_not_zero_dollars_per_LOC(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET state='CLOSED',landed=NULL")
        self.assertEqual(self.summary()["costs"]["windows"]["total"]["usd_per_loc"], "Infinity")
        self.assertEqual(activity_summary.loc_estimate(dict(estimated_usd="0"))["usd_per_loc"], "0")
        p = self.root / "budget/events.jsonl"
        p.write_text(p.read_text() + '{"torn":')
        s = self.summary()
        self.assertIsNone(s["costs"]["windows"]["total"]["spent_usd"])
        self.assertTrue(all(r["usd_per_hour"] is None for r in s["costs"]["spending"].values()))
        self.assertIsNotNone(s["costs"]["funding"])

    def test_shared_bors_promotion_turns_closed_pr_into_landed_output(self):
        self.summary()
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET checked=0 WHERE number=5")
            db.execute("UPDATE pr_observations SET checked=0 WHERE number=5")

        def observe(url, token, repo, numbers):
            return dict(
                prs=[
                    dict(
                        number=5,
                        state="CLOSED",
                        head_sha="head",
                        landed_at=datetime.fromtimestamp(self.now - 5, UTC).isoformat(),
                        landing_source="bors-main-push-receipt",
                        observed_at=self.now * 1000,
                        lean_loc=dict(head_sha="head", added=10000, deleted=0),
                    )
                ]
                if 5 in numbers
                else [],
                missing=[n for n in numbers if n != 5],
            )

        with (
            patch.dict(
                os.environ,
                TAUCETI_GITHUB_STATE_URL="https://observations.invalid",
                TAUCETI_GITHUB_STATE_TOKEN_FILE="unused",
            ),
            patch.object(a, "observe", side_effect=observe),
        ):
            s = self.summary()
        self.assertEqual(s["unknown_landings"], 0)
        self.assertEqual(s["costs"]["windows"]["7d"]["landed_loc_added"], 10200)
        self.assertEqual(Decimal(s["costs"]["windows"]["7d"]["weighted_loc"]), 10350)

    def test_native_and_compatibility_review_receipts_are_not_double_counted(self):
        # Clear the direct fixture receipt so just the dual-written review remains.
        self.summary()
        with contextlib.closing(a._connect()) as db, db:
            db.execute("DELETE FROM pr_contributions")
        fleet_events.emit("pr_activity", repository="org/repo", pr=100, activity="review")
        s = self.summary()
        self.assertEqual(s["pr_windows"]["total"]["reviews"], 1)
        self.assertEqual(s["pr_windows"]["total"]["assisted"], 1)
        with contextlib.closing(a._connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pr_contributions").fetchone()[0], 1)

    def test_fleet_ratio_divides_pooled_costs_by_pooled_LOC(self):
        s = self.summary()
        other = json.loads(json.dumps(s))
        for row in other["costs"]["windows"].values():
            row.update(spent_usd="0", pending_usd="0", estimated_usd="0", landed_loc_added=1000, open_loc_added=0)
        v = activity_summary.combine([dict(available=True, summary=s), dict(available=True, summary=other)])
        row = v["summary"]["costs"]["windows"]["total"]
        self.assertEqual(Decimal(row["usd_per_loc"]), Decimal(100) / 1650)

    def test_CI_history_is_local_incremental_and_accepts_late_receipts(self):
        p = self.root / "ci.sqlite"
        with contextlib.closing(sqlite3.connect(p)) as source, source:
            source.execute("CREATE TABLE events(id TEXT PRIMARY KEY,at REAL,kind TEXT,data TEXT)")
            source.execute(
                "INSERT INTO events VALUES ('one',?,'ci_result',?)",
                (self.now - 5, json.dumps(dict(conclusion="success", wait_seconds=10, run_seconds=100))),
            )
        self.assertEqual(self.summary()["ci_windows"]["total"]["attempts"], 1)
        with contextlib.closing(sqlite3.connect(p)) as source, source:
            source.execute(
                "INSERT INTO events VALUES ('late',?,'ci_result',?)",
                (self.now - 100, json.dumps(dict(conclusion="failure", wait_seconds=20, run_seconds=50))),
            )
        s = self.summary()
        self.summary()
        self.assertEqual(
            s["ci_windows"]["total"], dict(attempts=2, successes=1, failures=1, wait_seconds=30, run_seconds=150)
        )


if __name__ == "__main__":
    unittest.main()

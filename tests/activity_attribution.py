#!/usr/bin/env python3
"""Measured cost shares, unchanged spending, bounded local records and cheap readers."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity as a
from tauceti_worker import activity_attribution as attr
from tauceti_worker import activity_attribution_prepare as adapter
from tauceti_worker import activity_costs, activity_summary
from tauceti_worker.budget import Budget

REPO = "org/project"


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 2_000_000
        env = patch.dict(
            os.environ,
            dict(
                TAUCETI_ACTIVITY_DIR=str(self.root / "activity"),
                TAUCETI_BUDGET_DIR=str(self.root / "budget"),
                TAUCETI_FLEET_EVENTS=str(self.root / "events.jsonl"),
                TAUCETI_FLEET_MACHINE_ID="machine",
                TAUCETI_WORKER_ID="worker",
                TAUCETI_ACTIVITY_REVIEW_ARCHIVES=str(self.root / "archive"),
                XDG_CACHE_HOME=str(self.root / "cache"),
            ),
        )
        env.start()
        self.addCleanup(env.stop)
        self.db = a._connect()
        self.addCleanup(self.db.close)
        self.ledger = []
        git = self.root / "archive" / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("a" * 40 + "\n")
        (git / "FETCH_HEAD").write_text("a" * 40 + "\tbranch main\n")
        os.utime(git / "FETCH_HEAD", (self.now, self.now))

    def event(self, kind, **fields):
        value = dict(version=1, id=str(fields), kind=kind, at=self.now - 190, worker="worker", group="machine")
        value.update(fields)
        a._ingest(self.db, value)

    def pr(self, number, loc, *, owned=True, age=100):
        table = "prs" if owned else "pr_observations"
        columns = "repo,number,head,state,landed" + (",worker,scope,created" if owned else "")
        values = [REPO, number, "head", "MERGED", self.now - age]
        if owned:
            values += ["worker", "machine", self.now - 1000]
        self.db.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})", values)
        self.db.execute("INSERT INTO pr_loc VALUES (?,?,?,?,?,?)", (REPO, number, "head", loc, 0, self.now))
        self.event("pr_activity", repository=REPO, pr=number, activity="fix")

    def cost(self, identity, number, phase, value, *, rid=None, at=None, activity=True, worker="worker"):
        at = self.now - 200 if at is None else at
        rid = rid or identity
        self.event("round_started", round_id=rid, worker=worker)
        self.db.execute(
            "UPDATE rounds SET started=?,finished=?,kind=?,outcome='completed' WHERE id=?", (at - 1, at + 2, phase, rid)
        )
        context = dict(round_id=rid, repository=REPO, pr=number)
        event = dict(
            type="admit",
            id=identity,
            started=at,
            worker=worker,
            phase=phase,
            model="m",
            estimate=str(value),
            status="active",
        )
        if activity:
            event["activity"] = context
        for stamp, e in ((at, event), (at + 1, dict(type="settle", id=identity, cost=str(value), outcome="completed"))):
            self.ledger.append(dict(version=1, seq=len(self.ledger) + 1, timestamp=stamp, events=[e]))

    def reviews(self, number, local, total, *, rid, archived=True):
        runs = [f"{number}-run-{n}" for n in range(total)]
        if archived:
            for index, run in enumerate(runs):
                path = self.root / "archive" / "records" / "rounds" / str(number) / f"round-{index}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        dict(
                            repo=REPO,
                            pr=number,
                            arm="production",
                            run_ids=[run],
                            ts=datetime.fromtimestamp(self.now - 190, UTC).isoformat(),
                        )
                    )
                )

    def prepare(self):
        path = self.root / "budget" / "events.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.ledger))
        activity_costs.prepare(self.db, self.now, lambda _: "machine")
        attr.prepare(self.db, self.now)
        return a._summary(self.db, self.now)

    def archive_count(self, number):
        rows = [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM review_archive_records WHERE repo=? AND number=?", (REPO, number)
            )
        ]
        return len({r["review"] for r in rows if r and r["review"]})

    def example(self):
        self.pr(1, 15000)
        self.pr(2, 10000, owned=False)
        self.cost("author", 1, "roadmap", 10)
        self.cost("review-own", 1, "review", 7)
        for index, amount in enumerate([3, 3, 3, 3, 3, 5]):
            self.cost(f"review-other-{index}", 2, "review", amount)
        self.reviews(1, 1, 3, rid="review-own")
        self.reviews(2, 6, 24, rid="review-other")

    def test_worked_example_uses_measured_shares(self):
        self.example()
        summary = self.prepare()
        expected = Decimal(15000) * 17 / 31 + Decimal(10000) * 20 / (80 + Decimal(10000) * 10 / 15000)
        for row in summary["costs"]["windows"].values():
            self.assertAlmostEqual(Decimal(row["attributed_loc"]), expected)
            self.assertEqual(Decimal(row["estimated_usd"]), 37)
            self.assertEqual(row["attribution_known_prs"], 2)
            self.assertEqual(row["attribution_missing_prs"], 0)
            self.assertAlmostEqual(Decimal(activity_summary.attributed_rate(row)), 37 / expected)
            self.assertEqual(row["landed_loc_added"], 15000)

    def test_fixes_add_credit_and_overhead_and_failed_work_remain_charged(self):
        self.example()
        baseline = Decimal(self.prepare()["costs"]["windows"]["total"]["attributed_loc"])
        self.cost("fix-other", 2, "fix", 3)
        for phase, cost in (("progress", 5), ("bump", 7), ("roadmap", 11)):
            self.cost("overhead-" + phase, None, phase, cost)
        self.ledger[-1]["events"][0]["outcome"] = "failed"
        result = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(Decimal(result["estimated_usd"]), 63)
        self.assertGreater(Decimal(result["attributed_loc"]), baseline)
        expected = Decimal(15000) * 17 / 31 + Decimal(10000) * 23 / (80 + Decimal(10000) * 10 / 15000)
        self.assertAlmostEqual(Decimal(result["attributed_loc"]), expected)

    def test_no_local_reviews_uses_measured_unit_cost_and_missing_counts_are_unknown(self):
        self.example()
        self.pr(3, 15000)
        self.cost("author-3", 3, "roadmap", 10)
        self.reviews(3, 0, 3, rid="unused")
        result = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(result["attribution_known_prs"], 3)
        self.pr(4, 15000)
        self.cost("author-4", 4, "roadmap", 10)
        self.reviews(4, 0, 3, rid="unused", archived=False)
        partial = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(partial["attribution_missing_prs"], 1)
        self.assertEqual(partial["attributed_loc"], result["attributed_loc"])

    def test_historical_review_cost_links_recover_from_existing_native_intervals(self):
        self.example()
        self.pr(3, 15000)
        self.cost("author-3", 3, "roadmap", 10)
        self.cost("old-review", 3, "review", 6, at=self.now - 500, activity=False)
        a._ingest(
            self.db,
            dict(
                version=1,
                id="old-pr-receipt",
                kind="pr_activity",
                at=self.now - 499,
                repository=REPO,
                pr=3,
                worker="worker",
                group="machine",
                activity="review",
            ),
        )
        self.reviews(3, 1, 3, rid="old-review")
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_known_prs"], 3)
        self.assertEqual(row["attribution_missing_prs"], 0)
        self.assertEqual(Decimal(row["estimated_usd"]), 53)
        work = self.db.execute("SELECT repo,number FROM invocation_work WHERE id='old-review'").fetchone()
        self.assertEqual(tuple(work), (REPO, 3))

    def test_costs_before_window_are_used_but_after_landing_do_not_inflate_credit(self):
        self.example()
        self.ledger[0]["timestamp"] -= 2 * 86400
        self.ledger[0]["events"][0]["started"] -= 2 * 86400
        self.ledger[1]["timestamp"] -= 2 * 86400
        self.cost("after-land", 1, "fix", 100, at=self.now - 10)
        row = self.prepare()["costs"]["windows"]["24h"]
        self.assertEqual(Decimal(row["estimated_usd"]), 127)
        expected = Decimal(15000) * 17 / 31 + Decimal(10000) * 20 / (80 + Decimal(10000) * 10 / 15000)
        self.assertAlmostEqual(Decimal(row["attributed_loc"]), expected)

    def test_closed_unmerged_and_open_prs_never_supply_credit(self):
        self.example()
        self.db.execute("UPDATE pr_observations SET landed=NULL,state='CLOSED' WHERE number=2")
        self.db.execute("UPDATE prs SET landed=NULL,state='OPEN' WHERE number=1")
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(Decimal(row["attributed_loc"]), 0)
        self.assertEqual(Decimal(row["estimated_usd"]), 37)

    def test_archive_reads_are_bounded_cached_deduplicated_and_filter_shadow(self):
        self.example()
        directory = self.root / "archive" / "records" / "rounds" / "1"
        row = json.loads((directory / "round-0.json").read_text())
        (directory / "duplicate.json").write_text(json.dumps(row))
        (directory / "shadow.json").write_text(json.dumps({**row, "arm": "shadow:experiment", "run_ids": ["shadow"]}))
        with patch.object(attr, "MAX_RECORDS", 1):
            attr.prepare_reviews(self.db, self.now)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM review_archive_records").fetchone()[0], 1)
        with patch.object(attr, "read_record", wraps=attr.read_record) as read:
            attr.prepare_reviews(self.db, self.now + 301)
            self.assertEqual(read.call_count, len(list((self.root / "archive").rglob("*.json"))) - 1)
        self.assertEqual(self.archive_count(1), 3)
        with patch.object(attr, "read_record", side_effect=AssertionError("warm record reparsed")):
            attr.prepare_reviews(self.db, self.now + 602)

    def test_ambiguous_old_rounds_are_unattributed_and_exact_context_wins(self):
        self.example()
        self.db.execute("INSERT INTO round_prs VALUES ('author',?,1)", (REPO,))
        self.db.execute("INSERT INTO round_prs VALUES ('author',?,2)", (REPO,))
        targets = attr.CostTargets(self.db, self.now)
        session = dict(
            id="legacy", phase="roadmap", worker="worker", started=self.now - 200, activity=dict(round_id="author")
        )
        self.assertEqual(targets.resolve(session)[-2:], (None, None))
        session["activity"].update(repository=REPO, pr=1)
        self.assertEqual(targets.resolve(session)[-2:], (REPO, 1))

    def test_multiple_conversations_in_one_review_pass_count_once(self):
        self.example()
        self.cost("another-conversation", 1, "review", 3, rid="review-own")
        row = self.prepare()["costs"]["windows"]["total"]
        expected = Decimal(15000) * 20 / 40 + Decimal(10000) * 20 / (80 + Decimal(10000) * 10 / 15000)
        self.assertAlmostEqual(Decimal(row["attributed_loc"]), expected)
        self.assertEqual(Decimal(row["estimated_usd"]), 40)

    def test_failed_review_spending_does_not_increase_our_review_count(self):
        self.example()
        baseline = Decimal(self.prepare()["costs"]["windows"]["total"]["attributed_loc"])
        self.cost("failed-review", 1, "review", 20)
        self.db.execute("UPDATE rounds SET outcome='failed' WHERE id='failed-review'")
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(Decimal(row["estimated_usd"]), 57)
        self.assertEqual(Decimal(row["attributed_loc"]), baseline)

    def test_admission_metadata_survives_ledger_replay_and_projection(self):
        budget = Budget(self.root / "budget", clock=lambda: self.now)
        budget.configure(grant=100)
        with patch.dict(os.environ, {"TAUCETI_ACTIVITY_ROUND_ID": "native-round", "TAUCETI_GITHUB_PR": "42"}):
            context = attr.invocation_context()
            self.assertTrue(budget.admit("invocation", "worker", "model", "fix", activity=context)["allowed"])
        budget.settle("invocation", 7, "failed")
        self.assertEqual(budget.replay()["sessions"]["invocation"]["activity"], context)
        with budget.locked():
            self.assertEqual(budget.current()["sessions"]["invocation"]["activity"], context)

    def test_native_selection_records_target_and_existing_archive_without_new_receipts(self):
        captured = []
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_PR": "42"}),
            patch.object(a, "emit", side_effect=lambda kind, **fields: captured.append((kind, fields))),
        ):
            round = a.Round("worker")
            round.select("review")
            round.finish()
        selected = next(fields for kind, fields in captured if kind == "round_selected")
        self.assertEqual(selected["pr"], 42)
        self.assertEqual(selected["review_archive"], str(attr.archive_root()))
        self.assertEqual([kind for kind, _ in captured], ["round_started", "round_selected", "round_finished"])

    def test_archive_retains_only_observed_production_passes_and_tracks_updates(self):
        self.example()
        self.prepare()
        directory = self.root / "archive" / "records" / "rounds" / "1"
        path = directory / "legacy.json"
        path.write_text(
            json.dumps(
                dict(
                    repo=REPO,
                    pr=1,
                    round_id="legacy-1",
                    ran=["check"],
                    ts=datetime.fromtimestamp(self.now - 190, UTC).isoformat(),
                )
            )
        )
        self.db.execute("UPDATE review_archive_checks SET checked=0 WHERE number=1")
        attr.prepare_reviews(self.db, self.now + 301)
        self.assertEqual(self.archive_count(1), 4)
        path.unlink()
        self.db.execute("UPDATE review_archive_checks SET checked=0 WHERE number=1")
        attr.prepare_reviews(self.db, self.now + 602)
        self.assertEqual(self.archive_count(1), 3)

    def test_combined_rate_pools_dollars_and_credit_and_readers_do_no_collection(self):
        self.example()
        summary = self.prepare()
        value = dict(available=True, summary=summary)
        with (
            patch.object(a, "_connect", side_effect=AssertionError("SQL in status")),
            patch.object(attr, "prepare_reviews", side_effect=AssertionError("collection in status")),
            patch.object(attr, "read_record", side_effect=AssertionError("archive in status")),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            combined = activity_summary.combine([value, value])
            activity_summary.display(combined)
        row = combined["summary"]["costs"]["windows"]["total"]
        self.assertAlmostEqual(
            Decimal(row["attributed_loc"]), Decimal(summary["costs"]["windows"]["total"]["attributed_loc"]) * 2
        )
        self.assertEqual(row["attribution_known_prs"], 4)
        self.assertIn("Estimated $ / attributed LOC", out.getvalue())
        older = json.loads(json.dumps(summary))
        for r in older["costs"]["windows"].values():
            for field in list(r):
                if field.startswith(("attribution_", "attributed_")):
                    del r[field]
        row = activity_summary.combine([value, dict(available=True, summary=older)])["summary"]["costs"]["windows"][
            "total"
        ]
        self.assertIsNone(row["attributed_loc"])

    def test_quiet_preparation_does_not_read_history_or_recompute_facts(self):
        self.example()
        self.prepare()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attribution_dirty").fetchone()[0], 0)
        with (
            patch.object(adapter, "invocation", side_effect=AssertionError("quiet session scan")),
            patch.object(adapter, "fact", side_effect=AssertionError("quiet PR scan")),
            patch.object(adapter, "reports", side_effect=AssertionError("quiet report rebuild")),
            patch.object(attr, "read_record", side_effect=AssertionError("quiet archive read")),
        ):
            attr.prepare(self.db, self.now + 1)

    def test_backfill_is_bounded_and_visible_while_costs_are_fully_charged(self):
        self.example()
        self.prepare()
        for i in range(10):
            self.cost(f"extra-{i}", None, "progress", 1)
        path = self.root / "budget" / "events.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.ledger))
        activity_costs.prepare(self.db, self.now, lambda _: "machine")
        with (
            patch.object(adapter, "MAX_SESSIONS", 2),
            patch.object(adapter, "invocation", wraps=adapter.invocation) as process,
        ):
            attr.prepare(self.db, self.now)
        self.assertEqual(process.call_count, 2)
        self.assertLess(
            self.db.execute("SELECT COUNT(*) FROM invocation_work WHERE id LIKE 'extra-%'").fetchone()[0], 10
        )
        row = a._summary(self.db, self.now)["costs"]["windows"]["total"]
        self.assertTrue(row["attribution_preparing"])
        self.assertEqual(Decimal(row["estimated_usd"]), 47)

    def test_archive_behind_our_known_pass_count_is_incomplete(self):
        self.example()
        for i in range(3):
            self.cost(f"extra-own-review-{i}", 1, "review", 1)
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_missing_prs"], 1)
        self.assertEqual(row["attribution_known_prs"], 1)

    def test_failed_preparation_retains_cache_and_retries_its_dirty_work(self):
        self.example()
        self.prepare()
        before = self.db.execute("SELECT payload FROM attribution_reports ORDER BY subject").fetchall()
        self.cost("new-fix", 2, "fix", 3)
        path = self.root / "budget" / "events.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.ledger))
        activity_costs.prepare(self.db, self.now, lambda _: "machine")
        with patch.object(adapter, "reports", side_effect=ValueError("failed publication")):
            with self.assertRaisesRegex(ValueError, "failed publication"):
                attr.prepare(self.db, self.now)
        self.assertEqual(before, self.db.execute("SELECT payload FROM attribution_reports ORDER BY subject").fetchall())
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM attribution_dirty").fetchone()[0], 0)
        attr.prepare(self.db, self.now)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attribution_dirty").fetchone()[0], 0)

    def test_stale_full_archive_is_unknown_until_normal_sync_covers_landing(self):
        self.example()
        fetch = self.root / "archive" / ".git" / "FETCH_HEAD"
        os.utime(fetch, (self.now - 500, self.now - 500))
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_known_prs"], 0)
        self.assertEqual(row["attribution_missing_prs"], 2)
        self.now += 301
        os.utime(fetch, (self.now, self.now))
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_known_prs"], 2)

    def test_unsynced_local_outbox_reviews_are_unioned_with_full_archive(self):
        self.example()
        path = self.root / "archive" / "records" / "rounds" / "1" / "round-0.json"
        outbox = self.root / "outbox"
        local = outbox / "records" / "rounds" / "1" / "local.json"
        local.parent.mkdir(parents=True)
        local.write_bytes(path.read_bytes())
        path.unlink()
        self.db.execute("INSERT INTO review_archives VALUES (?,0)", (str(outbox),))
        row = self.prepare()["costs"]["windows"]["total"]
        expected = Decimal(15000) * 17 / 31 + Decimal(10000) * 20 / (80 + Decimal(10000) * 10 / 15000)
        self.assertAlmostEqual(Decimal(row["attributed_loc"]), expected)

    def test_worker_shares_use_common_denominators_and_add_up_to_host_credit(self):
        self.example()
        self.cost("peer-fix", 1, "fix", 10, worker="peer")
        summary = self.prepare()
        first = a._summary(self.db, self.now, "worker", "worker")["costs"]["windows"]["total"]
        peer = a._summary(self.db, self.now, "worker", "peer")["costs"]["windows"]["total"]
        self.assertGreater(Decimal(peer["attributed_loc"]), 0)
        self.assertAlmostEqual(
            Decimal(first["attributed_loc"]) + Decimal(peer["attributed_loc"]),
            Decimal(summary["costs"]["windows"]["total"]["attributed_loc"]),
        )

    def test_review_only_worker_uses_host_measured_production_rate(self):
        self.example()
        self.cost("peer-review", 2, "review", 4, worker="peer")
        self.prepare()
        row = a._summary(self.db, self.now, "worker", "peer")["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_known_prs"], 1)
        self.assertGreater(Decimal(row["attributed_loc"]), 0)

    def test_failed_fix_adds_no_credit_and_completed_recovery_counts_as_fix(self):
        self.example()
        baseline = Decimal(self.prepare()["costs"]["windows"]["total"]["attributed_loc"])
        self.cost("bad-fix", 2, "fix", 3)
        self.db.execute("UPDATE rounds SET outcome='failed' WHERE id='bad-fix'")
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(Decimal(row["attributed_loc"]), baseline)
        self.assertEqual(Decimal(row["estimated_usd"]), 40)
        self.cost("recovered-fix", 2, "recovery", 3)
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertGreater(Decimal(row["attributed_loc"]), baseline)
        self.assertEqual(Decimal(row["estimated_usd"]), 43)

    def test_unpriced_completed_local_review_is_not_imputed_as_outside_work(self):
        self.example()
        self.db.execute(
            "INSERT INTO rounds(id,worker,scope,kind,started,finished,outcome) VALUES ('unpriced','peer','machine','review',?,?, 'completed')",
            (self.now - 200, self.now - 190),
        )
        self.event("round_selected", round_id="unpriced", repository=REPO, pr=1, round_type="review", worker="peer")
        row = self.prepare()["costs"]["windows"]["total"]
        self.assertEqual(row["attribution_missing_prs"], 1)
        self.assertEqual(Decimal(row["estimated_usd"]), 37)


if __name__ == "__main__":
    unittest.main()

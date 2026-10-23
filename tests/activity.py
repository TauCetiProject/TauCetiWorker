#!/usr/bin/env python3
"""Activity counts are local, replay-safe, and never fall back to upstream reads."""

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity as a
from tauceti_worker import fleet_events


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_ACTIVITY_DIR": self.tmp.name,
                "TAUCETI_FLEET_EVENTS": self.tmp.name + "/worker-events.jsonl",
                "TAUCETI_BUDGET_DIR": self.tmp.name + "/budget",
                "TAUCETI_ACTIVITY_CI_DB": self.tmp.name + "/ci-events.sqlite",
                "TAUCETI_FLEET_MACHINE_ID": "machine",
                "TAUCETI_WORKER_ID": "worker",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, a, "_round", None)
        from tauceti_worker import work_recovery

        recovery_home = patch.object(work_recovery, "HERE", Path(self.tmp.name) / "worker")
        recovery_home.start()
        self.addCleanup(recovery_home.stop)
        self.now = time.time()

    def finish(self, age, kind="review", outcome="completed", rid=None):
        rid = rid or str(age)
        with patch.object(a.time, "time", return_value=self.now - age):
            a.emit("round_finished", key=rid, round_id=rid, round_type=kind, outcome=outcome)

    def test_upgrade_preserves_usage_history_and_ignores_old_containment_projection(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute("CREATE TABLE containment(boundary TEXT PRIMARY KEY, cleanup TEXT)")
            db.execute("INSERT INTO containment VALUES ('historical', 'namespace-unconfirmed')")
            db.execute("INSERT INTO conversation_costs VALUES ('paid', 'worker', 'machine', 1, 2, '17.25')")
        a.emit("containment_result", boundary="old", cleanup="namespace-unconfirmed")
        with contextlib.closing(a._connect()) as db, db:
            a._consume(db, [])
            self.assertEqual(
                tuple(db.execute("SELECT * FROM containment").fetchone()), ("historical", "namespace-unconfirmed")
            )
            self.assertEqual(db.execute("SELECT cost FROM conversation_costs WHERE id='paid'").fetchone()[0], "17.25")

    def test_host_cache_metrics_are_in_prepared_group_status(self):
        from tauceti_worker import expanded_cache

        a.atomic_json(a.root() / "cleanup.json", dict(at=1, managed_bytes=1024, protected_bytes=512))
        a.emit("round_started", worker="one", group="host-one", round_id="round-one")
        with patch.object(expanded_cache, "summary", return_value=dict(workers=1, hits=10, expanded=2, seconds=3)):
            value = a.prepare()
        self.assertEqual(value["groups"]["host-one"]["storage_cleanup"]["managed_bytes"], 1024)
        self.assertEqual(a.cached(group="host-one")["summary"]["expanded_cache"]["hits"], 10)

    def test_table_aligns_all_rows_and_lists_zero_count_work_types(self):
        self.finish(10, kind="fix")
        a.prepare(now=self.now)
        value = a.cached()
        self.assertEqual(value["summary"]["round_types"], list(a.ROUND_TYPES))
        value["summary"]["rounds"]["total"]["fix"]["finished"] = 12345678
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(value)
        lines = output.getvalue().splitlines()
        header = next(line for line in lines if line.startswith("Finished rounds"))
        expected_ends = [header.index(h) + len(h) for h in ("1h", "3h", "24h", "7d", "All time")]
        data_rows = [line for line in lines if re.search(r"\d+\s+\d+\s+\d+\s+\d+$", line)]
        for line in data_rows:
            self.assertEqual([m.end() for m in re.finditer(r"\d+", line)], expected_ends, line)
        for kind in ("roadmap", "review", "fix-ci", "progress", "security-audit", "no-work"):
            line = next(line for line in data_rows if line.strip().startswith(kind + " "))
            self.assertEqual(re.findall(r"\d+", line), ["0"] * 5)
        self.assertNotIn("unrecorded", output.getvalue())
        self.assertNotIn("Older resource logs", output.getvalue())
        self.assertNotIn("totals cover", output.getvalue())
        self.assertNotIn("Outcome rows are subsets", output.getvalue())
        pr_start = next(i for i, line in enumerate(lines) if line.startswith("PRs authored"))
        self.assertEqual(lines[pr_start - 1], "")

    def test_housekeeping_failure_does_not_abort_activity_refresh(self):
        from tauceti_worker import work_recovery

        with patch.object(work_recovery, "housekeep", side_effect=OSError("fixture housekeeping failure")):
            value = a.prepare(now=self.now)
        self.assertTrue(a.cached()["available"])
        self.assertTrue(any("Recovery housekeeping deferred" in error for error in value["errors"]))

    def test_current_work_counts_distinct_workers_not_unfinished_receipts(self):
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            for key, worker in (("one", "w1"), ("duplicate", "w1"), ("two", "w2")):
                db.execute(
                    "INSERT INTO rounds(id,worker,scope,kind,started) VALUES (?,?,?,'fix',?)",
                    (key, worker, "machine", self.now - 5),
                )
            summary = a._summary(db, self.now, "scope", "machine")
        self.assertEqual(summary["running"], 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(dict(available=True, summary=summary))
        self.assertIn("Currently: 2 workers working;", output.getvalue())

    def test_windows_total_failures_and_idempotent_replays(self):
        for age in (10, 3600, 3601, 10800, 10801, 86400, 86401, 604800, 604801):
            self.finish(age)
        self.finish(10)  # Same receipt, not an additional round.
        self.finish(20, kind="fix", outcome="timeout")
        a.prepare(now=self.now)
        s = a.cached(group="machine")["summary"]
        self.assertEqual([s["rounds"][w]["review"]["finished"] for w in a.WINDOWS], [2, 4, 6, 8, 9])
        self.assertEqual(s["rounds"]["3h"]["fix"]["failed"], 1)
        a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["rounds"], s["rounds"])

    def test_round_lifecycle_selection_no_work_waits_and_interruption(self):
        r = a.Round("worker")
        r.select("roadmap")
        r.finish()
        r.finish()
        r = a.Round("worker")
        r.rc = 76
        r.finish()
        for code in (80, 81):
            r = a.Round("worker")
            r.rc = code
            r.finish()
        from tauceti_worker.config import ClaimCollision

        r = a.Round("worker")
        r.finish(ClaimCollision("peer owns this PR"))
        r = a.Round("worker")
        r.finish()
        a.emit("round_started", round_id="killed", pid=999999999, hostname=__import__("socket").gethostname())
        a.prepare(now=time.time())
        s = a.cached(worker="worker")["summary"]
        self.assertEqual(s["rounds"]["total"]["roadmap"]["finished"], 1)
        self.assertEqual(s["rounds"]["total"]["no-work"]["finished"], 1)
        self.assertEqual(s["interrupted_unfinished"], 1)
        self.assertEqual(s["running"], 0)

    def test_pr_creation_is_recorded_without_fleet_journal(self):
        with patch.dict(os.environ, {"TAUCETI_FLEET_EVENTS": ""}):
            fleet_events.mutation(
                {"argv": ["pr", "create"]}, SimpleNamespace(stdout="https://github.com/org/repo/pull/7")
            )
        with patch.object(a, "observe", side_effect=AssertionError("No configured endpoint")):
            a.prepare(now=self.now + 1)
        s = a.cached()["summary"]
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        self.assertEqual(s["unknown_prs"], 1)

    def test_missing_finishes_show_start_windows_latest_round_and_scope(self):
        # These were all discovered together. Recent counts must use their
        # recorded starts, never pretend discovery is the interruption time.
        with contextlib.closing(a._connect()) as db, db:
            for rid, age, scope in (
                ("recent", 60, "machine"),
                ("hour-boundary", 3600, "machine"),
                ("day-boundary", 86400, "machine"),
                ("old", 86401, "machine"),
                ("unknown-start", None, "machine"),
                ("other-host", 1, "other"),
            ):
                db.execute(
                    "INSERT INTO rounds(id,worker,scope,kind,started,outcome) "
                    "VALUES (?,'worker',?,'fix',?,'interrupted-unfinished')",
                    (rid, scope, self.now - age if age is not None else None),
                )
        a.prepare(now=self.now)
        value = a.cached(group="machine")
        s = value["summary"]
        self.assertEqual(s["interrupted_unfinished"], 5)
        self.assertEqual(s["rounds"]["total"], {})
        self.assertEqual(
            s["interrupted_unfinished_details"],
            dict(started_1h=2, started_24h=3, latest=dict(round_id="recent", worker="worker", started=self.now - 60)),
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.display(value)
        self.assertIn("Rounds missing finish records (by start time): 2 in 1h; 3 in 24h; 5 all time", output.getvalue())
        self.assertIn("Latest affected round started", output.getvalue())
        self.assertIn("worker, round recent", output.getvalue())
        self.assertIn("excluded from finished counts", output.getvalue())
        self.assertIn("Check worker logs", output.getvalue())

        # A delayed finish reconciles the round rather than leaving a permanent
        # interruption warning or counting it in both categories.
        self.finish(0, kind="fix", rid="recent")
        a.prepare(now=self.now)
        s = a.cached(group="machine")["summary"]
        self.assertEqual(s["interrupted_unfinished"], 4)
        self.assertEqual(s["interrupted_unfinished_details"]["started_1h"], 1)
        self.assertEqual(s["interrupted_unfinished_details"]["latest"]["round_id"], "hour-boundary")
        self.assertEqual(s["rounds"]["total"]["fix"]["finished"], 1)

    def test_missing_finish_fleet_totals_and_older_summaries(self):
        from tauceti_worker import activity_summary

        a.prepare(now=self.now)
        one, two = a.cached(), a.cached()
        for value, count, age, rid in ((one, 3, 20, "one"), (two, 5, 10, "two")):
            value["summary"]["interrupted_unfinished"] = count
            value["summary"]["interrupted_unfinished_details"] = dict(
                started_1h=1, started_24h=2, latest=dict(started=self.now - age, worker=rid, round_id=rid)
            )
        combined = activity_summary.combine([one, two])
        self.assertEqual(combined["summary"]["interrupted_unfinished"], 8)
        detail = combined["summary"]["interrupted_unfinished_details"]
        self.assertEqual((detail["started_1h"], detail["started_24h"]), (2, 4))
        self.assertEqual(detail["latest"]["round_id"], "two")

        del two["summary"]["interrupted_unfinished_details"]
        for values in ([two], [one, two], [one, dict(available=False)]):
            value = activity_summary.combine(values)
            self.assertNotIn("interrupted_unfinished_details", value["summary"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(value)
            self.assertIn("all time; recent timing unavailable", output.getvalue())
            self.assertNotIn("Latest affected round", output.getvalue())

    def test_transport_wait_is_not_counted_as_a_failed_round(self):
        from tauceti_worker.config import Die

        r = a.Round("worker")
        with patch("tauceti_worker.github_transport.blocking_event_since", return_value={"reset": self.now + 60}):
            r.finish(Die("GitHub quota wait"))
        a.prepare(now=time.time())
        self.assertEqual(a.cached()["summary"]["rounds"]["total"], {})

    def test_missing_stale_and_failed_server_do_not_fallback_or_retry_early(self):
        a.emit("pr_created", repository="org/repo", pr=7)
        a.emit("pr_created", repository="org/repo", pr=8)
        env = {
            "TAUCETI_GITHUB_STATE_URL": "https://state.example",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": "/not-read-by-mock",
        }
        response = dict(prs=[dict(number=7, state="OPEN", observed_at=(self.now - 10000) * 1000)], missing=[8])
        with patch.dict(os.environ, env), patch.object(a, "observe", return_value=response) as observation:
            a.prepare(now=self.now + 1)
            a.prepare(now=self.now + 61)
            self.assertEqual(observation.call_count, 1)
        s = a.cached()["summary"]
        self.assertEqual(s["open_prs"], 1)
        self.assertEqual(s["unknown_prs"], 1)
        self.assertLess(s["pr_observed_at"], self.now - 9000)
        with patch.dict(os.environ, env), patch.object(a, "observe", side_effect=OSError("offline")) as observation:
            a.prepare(now=self.now + 302)
            a.prepare(now=self.now + 362)
            self.assertEqual(observation.call_count, 1)
        self.assertEqual(a.cached()["summary"]["open_prs"], 1)

    def test_read_and_display_do_not_open_database_or_network(self):
        self.finish(10)
        a.prepare(now=self.now)
        with (
            patch.object(a, "_connect", side_effect=AssertionError("SQL on read")),
            patch.object(a, "observe", side_effect=AssertionError("network on read")),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                a.display(a.cached())
        self.assertIn("All time", output.getvalue())

    def test_repository_access_failure_is_explicit_without_github_fallback(self):
        a.emit("pr_created", repository="org/roadmap", pr=7)
        env = {
            "TAUCETI_GITHUB_STATE_URL": "https://state.example",
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": "/not-read-by-mock",
        }
        error = a.urllib.error.HTTPError("https://state.example/v1/activity", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch.dict(os.environ, env), patch.object(a, "observe", side_effect=error) as observation:
            a.prepare(now=self.now + 1)
            a.prepare(now=self.now + 61)
        self.assertEqual(observation.call_count, 1)
        self.assertIn(
            "Shared PR observations unavailable for org/roadmap (HTTP 403; repository unavailable or access denied); retaining last-known data",
            a.cached()["errors"],
        )

    def test_pr_freshness_excludes_settled_history_and_respects_scope(self):
        old = self.now - 3 * 86400
        with contextlib.closing(a._connect()) as db, db:
            for number, state, landed, observed, created, worker, scope in (
                (1, "CLOSED", None, old, old, "worker", "machine"),
                (2, "OPEN", old, old, old, "worker", "machine"),
                (3, "MERGED", old, old, old, "worker", "machine"),
                (4, "OPEN", None, self.now - 2700, old, "worker", "machine"),
                (5, None, None, None, self.now - 300, "worker", "machine"),
                (6, "OPEN", None, old, old, "other", "other-machine"),
            ):
                db.execute(
                    "INSERT INTO prs(repo,number,state,landed,observed,created,worker,scope) VALUES ('org/repo',?,?,?,?,?,?,?)",
                    (number, state, landed, observed, created, worker, scope),
                )
            for column, identity in (("scope", "machine"), ("worker", "worker")):
                summary = a._summary(db, self.now, column, identity)
                self.assertEqual(summary["pr_observed_at"], old)  # Raw historical evidence is preserved.
                self.assertEqual(
                    summary["pr_freshness"], dict(observed_at=self.now - 2700, missing_since=self.now - 300)
                )
                with (
                    patch.object(a.time, "time", return_value=self.now),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    a.display(dict(available=True, summary=summary))
                self.assertNotIn("PR evidence:", out.getvalue())
            self.assertEqual(a._summary(db, self.now)["pr_freshness"]["observed_at"], old)

    def test_pr_evidence_warns_only_after_two_hours_without_refreshing(self):
        with contextlib.closing(a._connect()) as db, db:
            summary = a._summary(db, self.now)
        summary["pr_observed_at"] = self.now - 3 * 86400
        summary["pr_checked_at"] = self.now  # A recent read attempt cannot renew stored evidence.
        for freshness, expected in (
            (None, None),  # Legacy summaries cannot distinguish current data from history.
            (dict(observed_at=None, missing_since=None), None),
            (dict(observed_at=self.now - 7200, missing_since=self.now - 7200), None),
            (dict(observed_at=self.now - 7201, missing_since=None), "oldest unresolved PR observation 2h 0m"),
            (dict(observed_at=None, missing_since=self.now - 47 * 3600), "without an observation; created 47h 0m"),
        ):
            with self.subTest(freshness=freshness):
                if freshness is None:
                    summary.pop("pr_freshness", None)
                else:
                    summary["pr_freshness"] = freshness
                with (
                    patch.object(a.time, "time", return_value=self.now),
                    patch.object(a, "_connect", side_effect=AssertionError("SQL on display")),
                    patch.object(a, "observe", side_effect=AssertionError("network on display")),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    a.display(dict(available=True, summary=summary))
                if expected:
                    self.assertIn(expected, out.getvalue())
                    self.assertIn("stale; expected within 2h", out.getvalue())
                else:
                    self.assertNotIn("PR evidence:", out.getvalue())

    def test_pr_freshness_combines_hosts_without_using_legacy_history(self):
        with contextlib.closing(a._connect()) as db, db:
            summary = a._summary(db, self.now)
        legacy = dict(summary)
        legacy.pop("pr_freshness")
        legacy["pr_observed_at"] = self.now - 3 * 86400
        recent = dict(summary, pr_freshness=dict(observed_at=self.now - 2700, missing_since=None))
        stale = dict(summary, pr_freshness=dict(observed_at=self.now - 10800, missing_since=self.now - 86400))
        for hosts, expected in (
            ([legacy, recent], recent["pr_freshness"]),
            ([recent, stale, legacy], stale["pr_freshness"]),
            ([legacy], dict(observed_at=None, missing_since=None)),
        ):
            combined = a.activity_summary.combine([dict(available=True, summary=s) for s in hosts])
            self.assertEqual(combined["summary"]["pr_freshness"], expected)
        # Age is evaluated when reading the cache, even if it crosses the limit
        # without another preparation pass.
        with (
            patch.object(a.time, "time", return_value=self.now + 7201),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            a.display(a.activity_summary.combine([dict(available=True, summary=recent)]))
        self.assertIn("oldest unresolved PR observation", out.getvalue())

    def test_bors_requires_exact_head_and_verified_main_landing(self):
        a.emit("pr_created", repository="org/repo", pr=7)
        a.prepare(now=self.now)
        with contextlib.closing(a._connect()) as db, db:
            db.execute("UPDATE prs SET state='CLOSED',head='right'")
        path = Path(self.tmp.name) / "ci.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE merge_builds(repo TEXT,members_json TEXT,head_sha TEXT,landed INTEGER);CREATE TABLE main_landings(repo TEXT,head_sha TEXT,landed_at TEXT);"
            )
            db.execute(
                "INSERT INTO merge_builds VALUES (?,?,?,1)",
                ("org/repo", json.dumps([dict(pr=7, head_sha="wrong")]), "batch"),
            )
            db.execute("INSERT INTO main_landings VALUES ('org/repo','batch','2026-10-19T00:00:00Z')")
        with patch.dict(os.environ, TAUCETI_ACTIVITY_BORS_DB=str(path)):
            a.prepare(now=self.now)
            self.assertEqual(a.cached()["summary"]["unknown_landings"], 0)
            self.assertEqual(a.cached()["summary"]["pr_windows"]["total"]["landed"], 0)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE merge_builds SET members_json=?", (json.dumps([dict(pr=7, head_sha="right")]),))
            a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["pr_windows"]["total"]["landed"], 1)

    def test_normal_refresh_imports_coordination_after_cursor_migration(self):
        journal = Path(os.environ["TAUCETI_FLEET_EVENTS"])
        event = dict(
            version=1,
            kind="review_coordination",
            id="native-start",
            at=self.now,
            worker="worker",
            machine_id="machine",
            git_held=True,
        )
        journal.write_text(json.dumps(event) + "\n")
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO cursors VALUES (?,?,?)",
                (str(journal), journal.stat().st_ino, journal.stat().st_size),
            )
            db.execute("DELETE FROM meta WHERE key='coordination_journal_schema'")
        a.prepare(now=self.now)
        a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["local_coordination"]["24h"], dict(started=1, git_held=1))

    def test_backfill_ignores_resource_receipts(self):
        logs = Path(self.tmp.name) / "logs" / "worker"
        logs.mkdir(parents=True)
        path = logs / "resources.jsonl"
        rows = [
            dict(worker_id="worker", sampled_at=self.now, round=dict(id="old")),
            dict(worker_id="worker", sampled_at=self.now, round=dict(id="new", activity_id="native")),
        ]
        path.write_text("".join(json.dumps(v) + "\n" for v in rows))
        self.finish(1, rid="native")
        with patch("tauceti_worker.paths.HERE", Path(self.tmp.name)):
            a.prepare(now=self.now, import_history=True)
            a.prepare(now=self.now, import_history=True)
        s = a.cached()["summary"]
        self.assertEqual(sum(r["finished"] for r in s["rounds"]["total"].values()), 1)

    def test_migration_keeps_native_activity_and_cannot_restore_old_data(self):
        with contextlib.closing(a._connect()) as db, db:
            db.execute(
                "INSERT INTO rounds(id,worker,scope,kind,finished,outcome) VALUES ('resource:old','worker','machine','unknown',?,'unknown')",
                (self.now - 80,),
            )
            db.execute("INSERT INTO cursors VALUES ('/old/logs/worker/resources.jsonl',1,100)")
        with patch.object(a.time, "time", return_value=self.now - 60):
            a.emit("round_started", key="native:start", round_id="native")
        self.finish(30, rid="native")
        with patch.object(a.time, "time", return_value=self.now - 100):
            a.emit("pr_created", repository="org/repo", pr=7)
        with patch.object(a.time, "time", return_value=self.now - 10):
            a.emit("pr_created", repository="org/repo", pr=8)
        a.prepare(now=self.now)
        s = a.cached()["summary"]
        self.assertEqual(s["history_since"], self.now - 60)
        self.assertEqual(s["rounds"]["total"], {"review": dict(finished=1, failed=0, unknown=0)})
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        # Even a still-running older collector cannot reinsert imported rounds/PRs.
        with contextlib.closing(a._connect()) as db, db:
            db.execute("INSERT INTO rounds(id,kind) VALUES ('resource:replayed','unknown')")
            db.execute("INSERT INTO prs(repo,number,created) VALUES ('org/repo',9,?)", (self.now - 100,))
            db.execute("INSERT INTO prs(repo,number,created) VALUES ('org/repo',11,?)", (self.now - 1,))
            db.execute("UPDATE prs SET created=? WHERE number=11", (self.now - 100,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
            cutoff = db.execute("SELECT value FROM meta WHERE key='history_cutoff'").fetchone()[0]
        a.emit("round_finished", round_id="resource:pending", round_type="unknown", outcome="unknown")
        with patch.object(a.time, "time", return_value=self.now - 100):
            a.emit("pr_created", repository="org/repo", pr=10)
        a.prepare(now=self.now + 1, import_history=True)
        s = a.cached()["summary"]
        self.assertEqual(s["history_since"], self.now - 60)
        self.assertEqual(s["pr_windows"]["total"]["authored"], 1)
        with contextlib.closing(a._connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='history_cutoff'").fetchone()[0], cutoff)

    def test_observation_client_has_no_redirect_or_direct_api_fallback(self):
        import threading
        import urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer

        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header("Location", "https://api.github.com/repos/org/repo")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = Path(self.tmp.name) / "token"
        token.write_text("client")
        try:
            with self.assertRaises(urllib.error.HTTPError):
                a.observe(f"http://127.0.0.1:{server.server_port}", token, "org/repo", [7])
            self.assertEqual(calls, ["/v1/activity"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

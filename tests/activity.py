#!/usr/bin/env python3
"""Activity counts are local, replay-safe, and never fall back to upstream reads."""

import contextlib
import io
import json
import os
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
                "TAUCETI_FLEET_MACHINE_ID": "machine",
                "TAUCETI_WORKER_ID": "worker",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, a, "_round", None)
        self.now = time.time()

    def finish(self, age, kind="review", outcome="completed", rid=None):
        rid = rid or str(age)
        with patch.object(a.time, "time", return_value=self.now - age):
            a.emit("round_finished", key=rid, round_id=rid, round_type=kind, outcome=outcome)

    def test_windows_total_failures_and_idempotent_replays(self):
        for age in (10, 10800, 10801, 86400, 86401, 604800, 604801):
            self.finish(age)
        self.finish(10)  # Same receipt, not an additional round.
        self.finish(20, kind="fix", outcome="timeout")
        a.prepare(now=self.now)
        s = a.cached(group="machine")["summary"]
        self.assertEqual([s["rounds"][w]["review"]["finished"] for w in a.WINDOWS], [2, 4, 6, 7])
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
            self.assertIn("Recorded", output.getvalue())

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
            self.assertEqual(a.cached()["summary"]["unknown_landings"], 1)
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE merge_builds SET members_json=?", (json.dumps([dict(pr=7, head_sha="right")]),))
            a.prepare(now=self.now)
        self.assertEqual(a.cached()["summary"]["pr_windows"]["total"]["landed"], 1)

    def test_backfill_never_recounts_new_resource_receipts(self):
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
        self.assertEqual(sum(r["finished"] for r in s["rounds"]["total"].values()), 2)

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

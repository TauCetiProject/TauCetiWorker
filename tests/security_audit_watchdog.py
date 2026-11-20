#!/usr/bin/env python3
"""The audit cursor is advancing, or the operator has a private DM: one watchdog decides."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import audit_alerts as alerts
from tauceti_worker import audit_watchdog as watchdog
from tauceti_worker.config import NoProgress

A, B = "a" * 40, "b" * 40


class Watchdog(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        config = self.work / "bot.json"
        config.write_text(
            json.dumps({"site": "https://chat.example", "recipient_id": 1, "email": "bot", "api_key": "secret"})
        )
        for p in (
            patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG": str(config)}),
            patch.object(watchdog.time, "time", side_effect=lambda: self.now),
            patch.object(alerts.time, "time", side_effect=lambda: self.now),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.now = 10000.0
        self.http = patch.object(alerts, "request", return_value={"result": "success", "id": 7}).start()
        self.addCleanup(patch.stopall)
        self.seen = (A, 25)

    def observe(self):
        if isinstance(self.seen, BaseException):
            raise self.seen
        return self.seen

    def check(self, at, min_commits=20):
        self.now = at
        watchdog.check(self.work, self.observe, min_commits=min_commits)

    def posts(self):
        return [c.args[2]["content"] for c in self.http.call_args_list if c.args[1] == "POST"]

    def test_due_cursor_held_for_an_hour_sends_one_dm(self):
        self.check(10000)
        self.check(13599)
        self.assertEqual(self.posts(), [])
        self.check(13600)
        self.check(14000)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("Operational stall", self.posts()[0])
        self.assertIn("25 commit(s) are pending", self.posts()[0])
        self.assertNotIn("secret", self.posts()[0])

    def test_advancing_cursor_restarts_the_clock(self):
        self.check(10000)
        self.seen = (B, 25)
        self.check(13000)
        self.check(16000)
        self.assertEqual(self.posts(), [])
        self.check(16600)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn(B, self.posts()[0])

    def test_below_threshold_is_not_due_unless_work_was_started_there(self):
        self.seen = (A, 5)
        self.check(10000)
        self.check(10**6)
        self.assertEqual(self.posts(), [])
        # A pool range launched at this cursor (status persisted at launch) stays due.
        (self.work / "pool").mkdir()
        (self.work / "pool" / "ranges.json").write_text(json.dumps({"ranges": [{"from_sha": A, "status": "running"}]}))
        self.check(10**6 + 1)
        self.check(10**6 + 3601)
        self.assertEqual(len(self.posts()), 1)

    def test_held_block_at_the_cursor_is_due_below_the_threshold(self):
        self.seen = (A, 3)
        (self.work / "blocked.json").write_text(json.dumps({"from_sha": A, "reason": "malformed"}))
        self.check(10000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)

    def test_passed_range_whose_cursor_update_keeps_failing_is_still_due(self):
        self.seen = (A, 3)
        (self.work / "pool").mkdir()
        (self.work / "pool" / "ranges.json").write_text(json.dumps({"ranges": [{"from_sha": A, "status": "pass"}]}))
        self.check(10000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)

    def test_an_unreadable_range_list_is_due_below_the_threshold(self):
        self.seen = (A, 3)
        (self.work / "pool").mkdir()
        (self.work / "pool" / "ranges.json").write_text("{damaged")
        self.check(10000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)

    def test_newly_due_work_does_not_inherit_idle_time(self):
        self.seen = (A, 0)
        self.check(10000)
        self.seen = (A, 25)
        self.check(10**6)
        self.assertEqual(self.posts(), [])
        self.check(10**6 + 3599)
        self.assertEqual(self.posts(), [])
        self.check(10**6 + 3600)
        self.assertEqual(len(self.posts()), 1)

    def test_unexpected_delivery_errors_never_escape(self):
        import http.client

        self.check(10000)
        self.http.side_effect = http.client.IncompleteRead(b"")
        self.check(13600)  # Must not raise.
        self.assertTrue((self.work / "stall.json").exists())

    def test_missing_state_directory_and_failing_observation_still_alerts(self):
        self.work = self.work / "fresh" / "security-audit"
        self.seen = NoProgress("ls-remote failed")
        self.check(10000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("ls-remote failed", self.posts()[0])

    def test_caught_up_cursor_is_never_a_stall(self):
        self.seen = (A, 0)
        (self.work / "blocked.json").write_text(json.dumps({"from_sha": A}))
        self.check(10000)
        self.check(10**6)
        self.assertEqual(self.posts(), [])

    def test_unobservable_cursor_keeps_the_last_clock_and_says_why(self):
        self.check(10000)
        self.seen = NoProgress("gh: API rate limit exceeded")
        self.check(12000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)
        self.assertIn("cannot observe", self.posts()[0])
        self.assertIn("rate limit", self.posts()[0])
        self.assertIn(A, self.posts()[0])

    def test_daily_reminders_are_new_alerts(self):
        self.check(10000)
        self.check(13600)
        self.check(13600 + 86399)
        self.assertEqual(len(self.posts()), 1)
        self.check(13600 + 86400)
        self.assertEqual(len(self.posts()), 2)

    def test_failed_delivery_is_retried_on_a_later_check(self):
        self.check(10000)
        self.http.side_effect = OSError("certificate verify failed")
        with patch.object(alerts, "log") as logged:
            self.check(13600)
        self.assertIn("certificate verify failed", logged.call_args.args[0])
        self.http.side_effect = None
        self.http.return_value = {"result": "success", "messages": [], "id": 8}
        self.check(13600 + 901)
        self.assertEqual(len(self.posts()), 2)  # The failed attempt, then the delivered retry.
        self.check(13600 + 2000)
        self.assertEqual(len(self.posts()), 2)

    def test_corrupt_state_restarts_rather_than_silences(self):
        (self.work / "stall.json").write_text("not json")
        self.check(10000)
        self.check(13600)
        self.assertEqual(len(self.posts()), 1)

    def test_unconfigured_alerts_skip_observation(self):
        calls = []
        with patch.dict(os.environ, {"TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG": ""}):
            watchdog.check(self.work, lambda: calls.append(1) or (A, 25), min_commits=1)
        self.assertEqual(calls, [])
        self.assertFalse((self.work / "stall.json").exists())

    def test_concurrent_watchdogs_send_one_dm(self):
        import threading
        import time as real_time

        def slow(config, method, params):
            real_time.sleep(0.2)
            return {"result": "success", "id": 9, "messages": []}

        self.http.side_effect = slow
        self.check(10000)
        self.now = 13600
        threads = [
            threading.Thread(target=watchdog.check, args=(self.work, self.observe), kwargs={"min_commits": 20})
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.posts()), 1)

    def test_standalone_threshold_follows_the_owner(self):
        self.assertEqual(watchdog.threshold(self.work), 20)
        self.check(10000, min_commits=7)
        self.assertEqual(watchdog.threshold(self.work), 7)
        (self.work / "stall.json").write_text("not json")
        self.assertEqual(watchdog.threshold(self.work), 20)


if __name__ == "__main__":
    unittest.main()

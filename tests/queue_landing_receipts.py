#!/usr/bin/env python3
"""Only recorded canonical promotions credit exact accepted heads."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from queue_controller import PROJECT, ControllerCase

from tauceti_queue import feeds
from tauceti_queue.controller import VERDICT_CONTEXTS


class Receipts(ControllerCase):
    def land(self):
        self.accepted, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.clock.advance(10)
        self.github.complete_runs()
        for context in VERDICT_CONTEXTS:
            self.controller.handle_event("status", {"sha": self.github.runs[-1].head_sha, "context": context})
        self.controller.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": 1})["state"], "landed")

    def report(self):
        return feeds.candidates_feed(feeds.feed_source(self.controller), PROJECT, "main", promoted=True)

    def test_exact_head_promotion_and_timestamp_without_full_journal_reads(self):
        self.land()
        with patch.object(feeds.ControllerFeedSource, "_events", side_effect=AssertionError("full journal read")):
            report = self.report()
        self.assertEqual(feeds.validate_candidates(report), [])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["snapshot"]["members"][0]["head_sha"], self.accepted)
        event = candidate["events"][-1]
        self.assertEqual((event["kind"], event["source"]), ("promoted", "github.base_push_response"))
        self.assertEqual(event["observed_at"], round(self.clock() * 1_000_000))
        self.assertEqual(event["details"]["commit"], self.github.runs[-1].head_sha)
        self.assertEqual(report["admissions"], [])
        self.github.create_pr(101, {"A.lean": "different revision\n"})
        self.assertEqual(self.report()["candidates"][0]["snapshot"]["members"][0]["head_sha"], self.accepted)

    def test_no_credit_from_ci_or_missing_successful_main_push(self):
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.assertEqual(self.report()["candidates"], [])
        self.github.complete_runs()
        self.assertEqual(self.report()["candidates"], [])

    def test_recovery_and_absent_mismatched_or_unsuccessful_receipts(self):
        self.land()
        promotion = self.controller.store.row("outbox", {"kind": "promote"})
        for result in ("already-landed", "stale-source-attempt", "parent-changed", ""):
            self.controller.store.update("outbox", {"id": promotion["id"]}, result=result)
            self.assertEqual(self.report()["candidates"], [], result)
        self.controller.store.update("outbox", {"id": promotion["id"]}, result="already-visible")
        self.assertEqual(self.report()["candidates"][0]["events"][-1]["source"], "github.base_recovery_read")
        payload = json.loads(promotion["payload"])
        payload["tip"] = "a" * 40
        self.controller.store.update("outbox", {"id": promotion["id"]}, payload=json.dumps(payload))
        self.assertEqual(self.report()["candidates"], [])

    def test_rejected_members_and_failed_attempts_are_excluded(self):
        self.land()
        entry = self.controller.store.row("journal", {"batch_id": 1, "kind": "landed"})
        detail = json.loads(entry["detail"])
        detail["members"] = []
        detail["rejected"] = [101]
        self.controller.store.update("journal", {"id": entry["id"]}, detail=json.dumps(detail))
        self.assertEqual(self.report()["candidates"][0]["snapshot"]["members"], [])
        attempt = self.controller.store.row("ci_attempts", {"batch_id": 1})
        self.controller.store.update("ci_attempts", {"id": attempt["id"]}, head_sha="f" * 40)
        self.assertEqual(self.report()["candidates"], [])

    def test_failed_retries_do_not_hide_older_landings(self):
        self.land()
        attempt = self.controller.store.row("ci_attempts", {"batch_id": 1})
        attempt.pop("id")
        attempt["head_sha"] = "e" * 40
        for _ in range(feeds.CANDIDATE_LIMIT + 1):
            self.controller.store.insert("ci_attempts", **attempt)
        self.assertEqual(len(self.report()["candidates"]), 1)

    def test_unsettled_promotion_is_not_a_receipt(self):
        self.land()
        self.controller.store.update("outbox", {"kind": "promote"}, state="pending")
        self.assertEqual(self.report()["candidates"], [])

    def test_journal_lookup_has_an_index(self):
        plan = self.controller.store.query(
            "EXPLAIN QUERY PLAN SELECT * FROM journal WHERE batch_id=? AND kind=? ORDER BY id", (1, "landed")
        )
        self.assertIn("journal_batch_kind", str(plan))


if __name__ == "__main__":
    unittest.main()

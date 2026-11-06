#!/usr/bin/env python3
"""Large snapshots, interrupted transfers, acknowledgements and restart fencing."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_queue.monitoring_protocol import PublisherIdentity, ResyncRequired, SnapshotPublisher, encode


def observation(count=1200):
    return {
        "schema": "tauceti.operations.queue/v1",
        "observed_at": "2026-11-06T12:00:00Z",
        "scope": {"repositories": ["TauCetiProject/TauCeti"]},
        "coverage": "observed",
        "batches": [{"id": "1", "state": "head", "size": count}],
        "members": [{"id": str(i), "batch_id": "1", "explanation": "λ" * 1000} for i in range(count)],
        "inputs": [],
        "attempts": [],
        "tasks": [],
        "events": [],
    }


class Publication(unittest.TestCase):
    def publisher(self):
        return SnapshotPublisher({"id": "00000000-0000-0000-0000-000000000001", "epoch": 1})

    def test_1200_member_snapshot_is_transferred_in_bounded_utf8_frames(self):
        value = observation()
        self.assertGreater(len(encode(value)), 2_000_000)
        frames, _ = self.publisher().prepare(value)
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(encode(f)) <= 256_000 for f in frames))
        self.assertEqual([f["part"] for f in frames], list(range(len(frames))))
        self.assertTrue(all(f["parts"] == len(frames) for f in frames))
        self.assertEqual(sum(len(f["changes"]) for f in frames), 1201)
        self.assertNotIn("metadata", frames[0])
        self.assertEqual(frames[-1]["metadata"]["active_batch_ids"], ["1"])
        self.assertEqual(frames[-1]["metadata"]["collection_counts"]["members"], 1200)

    def test_failed_upload_does_not_acknowledge_a_partial_snapshot(self):
        publisher = self.publisher()
        sent = []

        def fail(frame):
            sent.append(frame)
            if frame["part"] == 1:
                raise OSError("connection lost")
            return {"committed": False}

        with self.assertRaises(OSError):
            publisher.publish(observation(), fail)
        self.assertEqual(publisher.hashes, {})
        retry, _ = publisher.prepare(observation())
        self.assertNotEqual(sent[0]["snapshot"], retry[0]["snapshot"])
        self.assertGreater(retry[0]["source"]["sequence"], sent[0]["source"]["sequence"])
        self.assertEqual(sum(len(f["changes"]) for f in retry), 1201)

    def test_acknowledged_snapshot_becomes_a_small_heartbeat_and_one_changed_row_is_sent_once(self):
        publisher = self.publisher()
        sent = []

        def accept(frame):
            sent.append(frame)
            return {"snapshot": frame["snapshot"], "committed": frame["part"] == frame["parts"] - 1}

        value = observation()
        publisher.publish(value, accept)
        sent.clear()
        value["observed_at"] = "2026-11-06T12:00:15Z"
        publisher.publish(value, accept)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["changes"], [])
        self.assertLess(len(encode(sent[0])), 2000)
        sent.clear()
        value["members"][50]["explanation"] = "Resolved an interaction."
        publisher.publish(value, accept)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]["changes"]), 1)
        self.assertEqual(sent[0]["changes"][0]["id"], "50")

    def test_absent_capped_history_is_not_mistaken_for_a_deletion(self):
        publisher = self.publisher()

        def accept(f):
            return {"snapshot": f["snapshot"], "committed": f["part"] == f["parts"] - 1}

        publisher.publish(observation(2), accept)
        value = observation(1)
        value["truncated"] = True
        publisher.publish(value, accept)
        self.assertIn(("members", "1"), publisher.hashes)
        frames, _ = publisher.prepare(observation(2))
        self.assertEqual([c for f in frames for c in f["changes"] if c["collection"] == "members"], [])

    def test_wrong_commit_acknowledgement_never_advances_hashes(self):
        publisher = self.publisher()
        with self.assertRaises(ValueError):
            publisher.publish(observation(1), lambda f: {"committed": True, "snapshot": "another snapshot"})
        self.assertEqual(publisher.hashes, {})

    def test_oversized_single_record_is_rejected_before_any_network_call(self):
        publisher = self.publisher()
        value = observation(1)
        value["members"][0]["explanation"] = "x" * 300_000
        with self.assertRaises(ValueError):
            publisher.publish(value, lambda _: self.fail("must validate the complete framing before sending"))

    def test_complete_collections_remove_deleted_attempts_but_partial_history_survives(self):
        publisher = self.publisher()

        def accept(f):
            return {"snapshot": f["snapshot"], "committed": f["part"] == f["parts"] - 1}

        value = observation(1)
        value["attempts"] = [{"id": "1", "batch_id": "1"}]
        publisher.publish(value, accept)
        value["attempts"] = []
        value["collection_complete"] = {"attempts": True}
        frames, _ = publisher.prepare(value)
        self.assertEqual(
            [c for f in frames for c in f["changes"]], [{"collection": "attempts", "id": "1", "value": None}]
        )
        self.assertIsNotNone(frames[0]["base"])

    def test_receiver_reset_forces_full_resend(self):
        publisher = self.publisher()
        value = observation(1)
        publisher.publish(value, lambda f: {"snapshot": f["snapshot"], "committed": True})

        def resync(_):
            raise ResyncRequired("base lost")

        with self.assertRaises(ResyncRequired):
            publisher.publish(value, resync)
        frames, _ = publisher.prepare(value)
        self.assertIsNone(frames[0]["base"])
        self.assertEqual(len(frames[0]["changes"]), 2)

    def test_total_snapshot_budget_is_checked_before_sending(self):
        publisher = self.publisher()
        from unittest.mock import patch

        with patch("tauceti_queue.monitoring_protocol.MAX_SNAPSHOT_BYTES", 100):
            with self.assertRaises(ValueError):
                publisher.publish(observation(1), lambda _: self.fail("must reject before network"))

    def test_identity_persists_and_old_process_epochs_are_fenced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.json"
            with PublisherIdentity(path) as first:
                self.assertGreater(first["epoch"], 1_000_000_000_000)
                with self.assertRaises(BlockingIOError):
                    with PublisherIdentity(path):
                        self.fail("a second publisher must not run concurrently")
            with PublisherIdentity(path) as second:
                self.assertGreater(second["epoch"], first["epoch"])
                self.assertEqual(first["id"], second["id"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text(json.dumps({"id": second["id"], "epoch": True}))
            with self.assertRaises(ValueError):
                with PublisherIdentity(path):
                    self.fail("corrupt epochs must not restart at zero")


if __name__ == "__main__":
    unittest.main()

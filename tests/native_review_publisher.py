#!/usr/bin/env python3
"""Shared native receipts preserve paid output, ordering and source authority."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_forgejo_source as fixture
from tauceti_tools.forge import AmbiguousWrite, Incomplete

from tauceti_queue.forgejo_source import PublisherNotGranted
from tauceti_worker.native_review_publisher import QueueReviewPublisher


class ReviewPublisher(unittest.TestCase):
    def setUp(self):
        self.native = fixture.NativeSource()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.clone
        self.policy = self.root / "policy.json"
        self.record = dict(
            schema="tauceti-native-review-publisher/v1",
            publishers=self.native.publishers.record(),
            ledger=str(self.root / "shared-outbox/publications.sqlite3"),
        )
        self.policy.write_text(json.dumps(self.record))
        self.policy.chmod(0o600)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, TAUCETI_NATIVE_REVIEW_POLICY=str(self.policy)).start()
        self.native.api._token = "token"
        self.transport = SimpleNamespace(
            number=1, head="a" * 40, source=SimpleNamespace(identity=self.native.identity, api=self.native.api)
        )

    def body(self, state="green", round=1):
        return (
            "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 "
            + json.dumps(
                dict(
                    repo=self.native.repository.key, head_sha=self.transport.head, states=dict(proof=state), round=round
                )
            )
            + "-->"
        )

    def publish(self, body=None):
        return QueueReviewPublisher(self.transport).publish(body or self.body())

    def test_completed_and_in_progress_and_red_use_existing_receipt_protocol(self):
        for index, state in enumerate(["pending", "green", "red"], 1):
            result = self.publish(self.body(state, index))
            self.assertEqual(result["id"], 999 + index)
            comments, status, _recovery = self.native.read()
            self.assertEqual(status, "success")
            self.assertEqual(len(comments), 1)
            self.assertIn('"proof": "' + state + '"', comments[0]["body"])
        self.assertEqual(len(self.native.rows), 3)  # Preserve immutable prior records.
        self.assertEqual(self.native.rows[0]["id"], 1000)

    def test_crash_after_registration_retries_exact_frozen_intent(self):
        original = self.native.api.post_comment.side_effect

        def post(*args):
            self.native.api.post_comment.side_effect = original
            original(*args)
            raise AmbiguousWrite("lost response")

        self.native.api.post_comment.side_effect = post
        with self.assertRaises(AmbiguousWrite):
            self.publish()
        nonce = self.native.status_rows[-1]["description"].split(":")[3]
        self.assertEqual(self.publish(), dict(id=1000))
        self.assertEqual(len(self.native.rows), 1)
        self.assertEqual(len(self.native.status_rows), 2)
        self.assertEqual(self.native.status_rows[-1]["description"].split(":")[3], nonce)
        self.assertEqual(self.publish(), dict(id=1000))
        self.assertEqual(len(self.native.status_rows), 2)

    def test_workers_share_order_and_old_retry_never_revives_old_verdict(self):
        red = self.body("red", 1)
        green = self.body("green", 2)
        self.publish(red)
        self.publish(green)
        self.assertEqual(self.publish(red), dict(superseded=True))
        comments, status, _recovery = self.native.read()
        self.assertEqual(status, "success")
        self.assertIn('"proof": "green"', comments[0]["body"])
        self.assertEqual([r["description"].split(":")[2] for r in self.native.status_rows], ["1", "1", "2", "2"])

    def test_foreign_metadata_and_policy_and_actor_cannot_publish(self):
        for body in [
            self.body().replace("a" * 40, "c" * 40),
            self.body().replace(self.native.repository.key, "other/project"),
        ]:
            with self.assertRaises(Incomplete):
                self.publish(body)
        self.assertFalse(self.native.rows)
        self.native.api.current_user.return_value = dict(id=99, login="other")
        with self.assertRaises(PublisherNotGranted):
            self.publish()
        self.assertFalse(self.native.rows)
        self.record["publishers"]["source"]["id"] = 99
        self.policy.write_text(json.dumps(self.record))
        with self.assertRaises(PublisherNotGranted):
            QueueReviewPublisher(self.transport)

    def test_unsafe_policy_and_ledger_are_refused(self):
        self.policy.chmod(0o644)
        with self.assertRaises(PublisherNotGranted):
            QueueReviewPublisher(self.transport)

        self.policy.chmod(0o600)
        QueueReviewPublisher(self.transport)
        ledger = Path(self.record["ledger"])
        ledger.chmod(0o644)
        with self.assertRaises(PublisherNotGranted):
            QueueReviewPublisher(self.transport)
        ledger.unlink()
        ledger.symlink_to(self.policy)
        with self.assertRaises(PublisherNotGranted):
            QueueReviewPublisher(self.transport)

    def test_preflight_checks_actor_and_permissions_before_inference(self):
        publisher = QueueReviewPublisher(self.transport)
        self.assertTrue(publisher.preflight())
        self.native.permission = "read"
        with self.assertRaises(PublisherNotGranted):
            publisher.preflight()
        self.native.permission = "write"
        self.native.api.current_user.return_value = dict(id=99, login="other")
        with self.assertRaises(PublisherNotGranted):
            publisher.preflight()

    def test_damaged_receipt_recovers_paid_body_even_after_scratch_cleanup(self):
        self.publish()
        self.native.rows[0]["body"] = "moderated body"
        self.native.api.pull = lambda *_: dict(state="open", head=dict(sha=self.transport.head))
        publisher = QueueReviewPublisher(self.transport)
        self.assertTrue(publisher.reconcile(now=1000))
        comments, status, _ = self.native.read()
        self.assertEqual(status, "success")
        self.assertIn('"proof": "green"', comments[0]["body"])
        self.assertEqual(len(self.native.rows), 2)
        self.assertFalse(publisher.reconcile(now=1001))
        self.assertFalse(publisher.reconcile(now=1400))
        self.assertEqual(len(self.native.rows), 2)

    def test_shared_outbox_waits_for_another_process(self):
        publisher = QueueReviewPublisher(self.transport)
        lock = publisher.ledger.with_suffix(".lock")
        script = 'import fcntl,sys,select; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); select.select([sys.stdin],[],[],5)'
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")

        def release():
            process.stdin.write("release\n")
            process.stdin.flush()

        timer = threading.Timer(0.2, release)
        started = time.monotonic()
        timer.start()
        try:
            self.assertTrue(publisher.preflight())
        finally:
            timer.join()
            process.communicate(timeout=5)
        self.assertGreater(time.monotonic() - started, 0.1)


if __name__ == "__main__":
    unittest.main()

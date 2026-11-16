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
from tauceti_tools.forge import AmbiguousWrite, ForgeError

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
        self.runs = {}
        self.transport = SimpleNamespace(
            plan={}, number=1, head="a" * 40, source=SimpleNamespace(identity=self.native.identity, api=self.native.api)
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
        body = body or self.body()
        publisher = QueueReviewPublisher(self.transport)
        if body not in self.runs:
            self.runs[body] = publisher.reserve()
        self.transport.plan = {"backend_run": self.runs[body]}
        return publisher.publish(body)

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
        self.assertEqual(
            [r["description"].split(":")[2] for r in self.native.status_rows], ["1024", "1024", "2048", "2048"]
        )

    def test_foreign_metadata_and_policy_and_actor_cannot_publish(self):
        for body in [
            self.body().replace("a" * 40, "c" * 40),
            self.body().replace(self.native.repository.key, "other/project"),
        ]:
            with self.assertRaises(PublisherNotGranted):
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
        publisher.preflight()
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
        lock = publisher.ledger.with_suffix(".pr-1.lock")
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
            publisher.reserve()
            elapsed = time.monotonic() - started
        finally:
            timer.join()
            process.communicate(timeout=5)
        self.assertGreater(elapsed, 0.1)

    def test_authority_rotation_republishes_retained_paid_body_without_old_grants(self):
        self.publish()
        self.native.api.pull = lambda *_: dict(state="open", head=dict(sha=self.transport.head))
        original = self.policy.read_text()
        self.record["publishers"]["generation"] = "2" * 32
        self.policy.write_text(json.dumps(self.record))
        publisher = QueueReviewPublisher(self.transport)
        self.assertTrue(publisher.reconcile(now=1000))
        self.assertEqual(len(self.native.rows), 2)
        self.assertIn('"generation":"' + "2" * 32 + '"', self.native.rows[-1]["body"])
        self.policy.write_text(original)
        with self.assertRaises(PublisherNotGranted):
            QueueReviewPublisher(self.transport).preflight()

    def test_older_paid_body_cannot_overtake_new_review_when_first_freeze_failed(self):
        older, newer = self.body("red", 1), self.body("green", 2)
        publisher = QueueReviewPublisher(self.transport)
        self.runs[older] = publisher.reserve()
        self.native.api.repository.side_effect = ForgeError("source unavailable")
        with self.assertRaises(ForgeError):
            self.publish(older)
        self.native.api.repository.side_effect = None
        self.publish(newer)
        self.assertEqual(self.publish(older), dict(superseded=True))
        comments, state, _ = self.native.read()
        self.assertEqual(state, "success")
        self.assertIn('"proof": "green"', comments[0]["body"])

    def test_receipt_repair_stays_before_an_already_reserved_newer_run(self):
        old, new = self.body("red", 1), self.body("green", 2)
        self.publish(old)
        self.runs[new] = QueueReviewPublisher(self.transport).reserve()
        self.native.rows[0]["body"] = "damaged"
        self.assertTrue(self.publish(old)["repaired"])
        self.publish(new)
        self.assertEqual(self.native.status_rows[-1]["description"].split(":")[2], "2048")

    def test_repeated_moderation_is_bounded_and_keeps_paid_output(self):
        self.publish()
        for _ in range(QueueReviewPublisher.MAX_REPAIRS):
            self.native.rows[-1]["body"] = "damaged"
            self.assertTrue(self.publish()["repaired"])
        self.native.rows[-1]["body"] = "damaged"
        with self.assertRaisesRegex(PublisherNotGranted, "repeatedly damaged"):
            self.publish()
        self.assertEqual(len(self.native.rows), 4)

    def test_undelivered_red_receipt_survives_closure(self):
        original = self.native.api.post_comment.side_effect
        self.native.api.post_comment.side_effect = AmbiguousWrite("outage")
        with self.assertRaises(AmbiguousWrite):
            self.publish(self.body("red"))
        self.native.api.post_comment.side_effect = original
        self.native.api.pull = lambda *_: dict(state="closed", head=dict(sha=self.transport.head))
        self.assertTrue(QueueReviewPublisher(self.transport).reconcile(now=1000))
        comments, state, _ = self.native.read()
        self.assertEqual(state, "success")
        self.assertIn('"proof": "red"', comments[0]["body"])

    def test_unreceipted_scoreboard_cannot_suppress_review(self):
        self.native.rows.append(self.native.comment(body=self.body(), comment_id=9999))
        self.assertEqual(QueueReviewPublisher(self.transport).comments(), [])
        self.publish(self.body("red"))
        rows = QueueReviewPublisher(self.transport).comments()
        self.assertEqual(len(rows), 1)
        self.assertIn('"proof": "red"', rows[0]["body"])

    def test_native_worker_view_uses_the_same_receipt_selection(self):
        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        self.publish(self.body("red"))
        forged = self.native.comment(body=self.body("green", 3), comment_id=9999)
        source = ForgejoWorkerSource(self.native.identity, self.native.api, token_file=self.root / "token")
        source.review_publication_policy = str(self.policy)
        source.live_pull = lambda _: dict(head=dict(sha=self.transport.head))
        discussion = dict(id=9998, body="Ordinary author discussion")
        with patch(
            "tauceti_worker.forgejo_reads.ForgejoReadSource.issue_comments",
            return_value=[*self.native.rows, forged, discussion],
        ):
            rows = source.issue_comments(1)
        self.assertIn(discussion, rows)
        self.assertNotIn(forged, rows)
        self.assertEqual(len(rows), 2)
        self.assertIn('"proof": "red"', rows[-1]["body"])

    def test_sweep_of_one_pr_does_not_hold_other_pr_publication(self):
        self.publish()
        entered, release = threading.Event(), threading.Event()

        def pull(_repo, number):
            if number == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test sweep release missing")
            return dict(state="open", head=dict(sha=self.transport.head))

        self.native.api.pull = pull
        errors = []

        def sweep():
            try:
                QueueReviewPublisher(self.transport).reconcile(now=1000)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=sweep)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            other = SimpleNamespace(source=self.transport.source, number=2, head=self.transport.head, plan={})
            publisher = QueueReviewPublisher(other)
            other.plan["backend_run"] = publisher.reserve()
            self.assertIn("id", publisher.publish(self.body()))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)

    def test_one_bad_pr_does_not_starve_another_receipt_repair(self):
        self.publish()
        other = SimpleNamespace(source=self.transport.source, number=2, head=self.transport.head, plan={})
        publisher = QueueReviewPublisher(other)
        other.plan["backend_run"] = publisher.reserve()
        publisher.publish(self.body())
        self.native.rows[-1]["body"] = "damaged second PR"

        def pull(_repo, number):
            if number == 1:
                raise ForgeError("first PR temporarily unavailable")
            return dict(state="open", head=dict(sha=self.transport.head))

        self.native.api.pull = pull
        self.assertTrue(publisher.reconcile(now=1000))
        self.assertEqual(len(self.native.rows), 3)

    def test_scope_is_reentrant_and_rejects_old_run_before_finding_writes(self):
        red, green = self.body("red", 1), self.body("green", 2)
        self.publish(red)
        self.publish(green)
        self.transport.plan = {"backend_run": self.runs[red]}
        publisher = QueueReviewPublisher(self.transport)
        with publisher.scope():
            with publisher.scope():
                self.assertTrue(publisher.superseded(red))
                self.assertEqual(publisher.publish(red), {"superseded": True})

    def test_previous_head_remains_verified_review_history(self):
        self.publish(self.body("red"))
        self.transport.head = "b" * 40
        rows = QueueReviewPublisher(self.transport).comments()
        self.assertEqual(len(rows), 1)
        self.assertIn("a" * 40, rows[0]["body"])
        self.native.rows[0]["body"] = "edited old history"
        self.assertRaises(ForgeError, QueueReviewPublisher(self.transport).comments)

    def test_legacy_paid_body_migrates_once_and_cannot_override_new_run(self):
        publisher = QueueReviewPublisher(self.transport)
        red = self.body("red")
        self.assertFalse(publisher.superseded(red))
        first = publisher.publish(red)
        self.assertEqual(publisher.publish(red), first)
        self.assertFalse(publisher.superseded(red))
        self.publish(self.body("green", 2))
        self.transport.plan = {}
        self.assertTrue(publisher.superseded(red))
        self.assertEqual(publisher.publish(red), {"superseded": True})

    def test_paid_reservation_can_complete_after_generation_rotation(self):
        self.publish(self.body("red"))
        self.record["publishers"]["generation"] = "2" * 32
        self.policy.write_text(json.dumps(self.record))
        publisher = QueueReviewPublisher(self.transport)
        self.assertFalse(publisher.superseded(self.body("red")))
        result = publisher.publish(self.body("red"))
        self.assertEqual(result, publisher.publish(self.body("red")))
        self.assertEqual(len(self.native.rows), 2)

    def test_transient_prepare_failures_do_not_consume_damage_repairs(self):
        self.publish()
        self.native.rows[0]["body"] = "damaged"
        self.native.api.repository.side_effect = ForgeError("transient read outage")
        for _ in range(4):
            self.assertRaises(ForgeError, self.publish)
        self.native.api.repository.side_effect = None
        self.assertTrue(self.publish()["repaired"])

    def test_policy_cannot_be_copied_to_another_publisher_host(self):
        self.record["publisher_host_id"] = "another-host"
        self.policy.write_text(json.dumps(self.record))
        self.assertRaises(PublisherNotGranted, QueueReviewPublisher, self.transport)

    def test_one_bad_comment_read_is_local_to_its_pr(self):
        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        source = ForgejoWorkerSource(self.native.identity, self.native.api, token_file=self.root / "token")
        source.review_publication_policy = str(self.policy)
        source.live_pull = lambda _: dict(head=dict(sha=self.transport.head))
        with (
            patch("tauceti_worker.forgejo_reads.ForgejoReadSource.issue_comments", return_value=[]),
            patch(
                "tauceti_worker.native_review_publisher.QueueReviewPublisher.comments", side_effect=ForgeError("outage")
            ),
        ):
            self.assertIsNone(source.issue_comments(1))


if __name__ == "__main__":
    unittest.main()

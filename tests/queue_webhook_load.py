#!/usr/bin/env python3
"""Webhook noise must not compete with worker claims or amplify controller lock wakeups."""

import http.client
import json
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from queue_controller import PROJECT, ControllerCase
from queue_server import FakeAdapter

from tauceti_queue import server, webhook
from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.controller import ControllerLock

SECRET = b"isolated-webhook-load"


class PrimaryFiltering(ControllerCase):
    def setUp(self):
        super().setUp()
        self.intake = AdmissionPoller(
            self.controller,
            self.github,
            PROJECT,
            AdmissionPolicy(rubrics=["correctness", "reuse"], prefix="TauCeti/", allow_files=[]),
            clock=self.clock,
        )
        self.httpd = server.create_server(
            self.controller,
            server.ServerConfig(
                adapter=FakeAdapter(),
                repositories={1: PROJECT},
                webhook_secret=SECRET,
                port=0,
                identity_revalidate_seconds=0,
            ),
        )
        self.thread = server.serve_in_thread(self.httpd)
        self.addCleanup(self.close_server)

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def deliver(self, event, payload, delivery=None, secret=SECRET):
        body = json.dumps({"repository": {"full_name": PROJECT}, **payload}).encode()
        client = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            client.request(
                "POST",
                "/webhook/github",
                body,
                {
                    "Content-Type": "application/json",
                    "X-GitHub-Event": event,
                    "X-GitHub-Delivery": delivery or str(time.monotonic_ns()),
                    "X-Hub-Signature-256": webhook.signature_for(secret, body),
                },
            )
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_ci_noise_answers_while_controller_lock_is_held(self):
        noise = [
            (event, {"action": action})
            for event in ("check_run", "check_suite", "workflow_run")
            for action in ("created", "requested", "in_progress", "completed")
        ]
        with self.controller._lock:
            with ThreadPoolExecutor(max_workers=6) as pool:
                answers = list(pool.map(lambda pair: self.deliver(*pair), noise * 4))
        self.assertEqual(answers, [(202, {"result": "ignored"})] * len(answers))
        self.assertEqual(self.httpd.receiver.ignored, len(answers))
        self.assertEqual(self.controller.store.journal_entries("event"), [])
        self.assertEqual(self.intake.state.summary()["pending"], 0)

    def test_signature_and_repository_are_checked_before_ignoring(self):
        self.assertEqual(self.deliver("check_run", {}, secret=b"wrong")[0], 401)
        self.assertEqual(
            self.deliver("check_run", {"repository": {"full_name": "other/repo"}}), (202, {"result": "ignored"})
        )

    def test_irrelevant_push_status_labels_title_and_prose_skip_controller(self):
        noise = [
            ("push", {"ref": "refs/heads/feature"}),
            ("push", {"ref": "refs/tauceti-queue/batches/1/v1"}),
            ("status", {"context": "bors", "sha": "a" * 40}),
            ("pull_request", {"number": 1, "action": "labeled", "label": {"name": "ready-to-merge"}}),
            (
                "pull_request",
                {
                    "number": 1,
                    "action": "unlabeled",
                    "label": {"name": "ready-to-merge"},
                    "pull_request": {"state": "closed"},
                },
            ),
            ("pull_request", {"number": 1, "action": "edited", "changes": {"title": {"from": "old"}}}),
            ("pull_request", {"number": 1, "action": "review_requested"}),
            (
                "issue_comment",
                {
                    "action": "created",
                    "issue": {"number": 1, "pull_request": {}},
                    "comment": {"body": "Thanks for the review"},
                },
            ),
            ("pull_request_review", {"action": "submitted", "review": {"body": "Looks good"}}),
        ]
        with self.controller._lock:
            for event, payload in noise:
                self.assertEqual(self.deliver(event, payload), (202, {"result": "ignored"}), (event, payload))
        self.assertEqual(self.controller.store.journal_entries("event"), [])

    def test_relevant_statuses_main_pushes_and_pr_changes_still_reconcile(self):
        events = [("push", {"ref": "refs/heads/main"})]
        events += [
            ("status", {"context": context, "sha": "a" * 40})
            for context in ("build", "scope", "bump-guard", "self-tested/1")
        ]
        events += [
            ("pull_request", {"number": 1, "action": action})
            for action in ("opened", "synchronize", "closed", "converted_to_draft", "ready_for_review")
        ]
        events += [
            ("pull_request", {"number": 1, "action": "labeled", "label": {"name": name}})
            for name in ("hold", "self-tested", "full-lint", "dont-repair-in-queue")
        ]
        events += [("pull_request", {"number": 1, "action": "edited", "changes": {"body": {"from": "old"}}})]
        for event, payload in events:
            self.assertEqual(self.deliver(event, payload), (202, {"result": "ok"}), (event, payload))
        self.assertEqual(len(self.controller.store.journal_entries("event")), len(events))
        self.assertIsNotNone(self.intake.state.get(1))

    def test_review_evidence_edits_and_deletion_are_not_discarded(self):
        issue = {"number": 1, "pull_request": {}}
        for payload in (
            {"action": "created", "issue": issue, "comment": {"body": "<!--tauceti-scoreboard-->"}},
            {
                "action": "edited",
                "issue": issue,
                "comment": {"body": "removed"},
                "changes": {"body": {"from": "<!--tauceti-scoreboard-->"}},
            },
            {"action": "deleted", "issue": issue, "comment": {"id": 42}},
        ):
            self.assertEqual(self.deliver("issue_comment", payload), (202, {"result": "ok"}))
        self.assertEqual(self.intake.state.get(1)["generation"], 3)

    def test_human_commands_in_comments_and_reviews_are_preserved(self):
        actor = {"login": "alice", "id": 7}
        with patch("tauceti_queue.commands.run_comment") as run:
            self.assertEqual(
                self.deliver(
                    "issue_comment",
                    {
                        "action": "created",
                        "issue": {"number": 1, "pull_request": {}},
                        "comment": {"body": "bors r+", "user": actor, "id": 10},
                    },
                ),
                (202, {"result": "ok"}),
            )
            self.assertEqual(
                self.deliver(
                    "pull_request_review",
                    {
                        "action": "submitted",
                        "pull_request": {"number": 1},
                        "review": {"body": "bors r-", "user": actor, "id": 11},
                    },
                ),
                (202, {"result": "ok"}),
            )
        self.assertEqual([call.args[2].body for call in run.call_args_list], ["bors r+", "bors r-"])

    def test_controller_failure_can_be_explicitly_redelivered(self):
        payload = {"ref": "refs/heads/main"}
        with patch.object(self.controller, "handle_event", side_effect=RuntimeError("injected")):
            self.assertEqual(self.deliver("push", payload, delivery="retry-me")[0], 500)
        self.assertEqual(self.deliver("push", payload, delivery="retry-me"), (202, {"result": "ok"}))
        self.assertEqual(self.deliver("push", payload, delivery="retry-me"), (202, {"result": "duplicate"}))

    def test_legacy_intake_keeps_ci_notifications(self):
        self.controller.admission = None
        for event in ("check_run", "check_suite", "workflow_run"):
            self.assertEqual(self.deliver(event, {"action": "completed"}), (202, {"result": "ok"}))
        self.assertEqual(len(self.controller.store.journal_entries("event")), 3)


class LockWakeups(unittest.TestCase):
    def test_each_waiter_is_woken_once_and_workers_keep_priority(self):
        lock = ControllerLock()
        waits = [0]
        original = lock._cond.wait

        def wait(*args, **kwargs):
            waits[0] += 1
            return original(*args, **kwargs)

        lock._cond.wait = wait
        order = []

        def ordinary():
            with lock:
                time.sleep(0.001)
                order.append("ordinary")

        def priority():
            with lock.priority():
                time.sleep(0.001)
                order.append("priority")

        lock.acquire()
        ordinary_threads = [threading.Thread(target=ordinary) for _ in range(64)]
        priority_threads = [threading.Thread(target=priority) for _ in range(3)]
        threads = ordinary_threads + priority_threads
        try:
            for thread in ordinary_threads:
                thread.start()
            deadline = time.monotonic() + 5
            while waits[0] < 64 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(waits[0], 64)
            for thread in priority_threads:
                thread.start()
            deadline = time.monotonic() + 5
            while lock.waiting() < 3 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(lock.waiting(), 3)
        finally:
            lock.release()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(order[:3], ["priority"] * 3)
        self.assertEqual(len(order), 67)
        self.assertLessEqual(waits[0], 67, "broadcast wakeups made 64 contenders wait 2080 times")

    def test_last_priority_timeout_does_not_strand_an_ordinary_waiter(self):
        lock = ControllerLock()
        done = threading.Event()
        refused = []
        lock.acquire()

        def ordinary():
            with lock:
                done.set()

        thread = threading.Thread(target=ordinary)
        priority = threading.Thread(target=lambda: refused.append(lock.acquire(timeout=0.02, priority=True)))
        thread.start()
        priority.start()
        priority.join(timeout=2)
        self.assertEqual(refused, [False])
        self.assertEqual(lock.waiting(), 0)
        lock.release()
        self.assertTrue(done.wait(timeout=2))
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

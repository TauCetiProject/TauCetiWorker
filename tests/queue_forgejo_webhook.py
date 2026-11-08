#!/usr/bin/env python3
"""Native signatures, repository binding, durable invalidation and redelivery."""

import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_requests import policy

from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.fakes import make_controller
from tauceti_queue.forgejo_webhook import ForgejoWebhookReceiver


class Webhooks(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.controller, adapter, _repo, clock = make_controller(root.name, repository="team/Math")
        self.addCleanup(self.controller.store.close)
        self.poller = AdmissionPoller(
            self.controller,
            adapter,
            "team/Math",
            AdmissionPolicy(rubrics=("proof",), prefix="Math/", allow_files=()),
            clock=clock,
        )
        self.source = policy().source
        self.secret = b"dedicated-source-hook-secret" * 2
        self.receiver = ForgejoWebhookReceiver(self.controller, self.source, self.secret)
        self.payload = {
            "repository": {
                "id": self.source.native_id,
                "full_name": "team/Math",
                "html_url": self.source.repository.url,
            },
            "number": 1,
            "action": "opened",
            "pull_request": {"number": 1, "head": {"sha": "a" * 40}},
        }

    def send(self, payload=None, *, event="pull_request", delivery="owned-delivery", signature=None):
        body = json.dumps(payload or self.payload).encode()
        signature = signature or hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return self.receiver.handle(body, event=event, delivery=delivery, signature=signature)

    def test_signed_delivery_records_pending_work_without_granting_approval(self):
        self.assertEqual(self.send(), "invalidated")
        work = self.poller.state.get(1)
        self.assertEqual(work["head_sha"], "a" * 40)
        self.assertNotEqual(work["generation"], work["evaluated_generation"])
        self.assertEqual(self.controller.store.rows("inputs"), [])
        self.assertEqual(self.send(), "duplicate")
        self.assertEqual(self.poller.state.get(1)["generation"], work["generation"])

    def test_bad_signature_or_another_repository_or_server_cannot_invalidate(self):
        with self.assertRaises(PermissionError):
            self.send(signature="0" * 64)
        for change in [
            {"id": self.source.native_id + 1},
            {"full_name": "other/Math"},
            {"html_url": "https://other.example/team/Math"},
        ]:
            with self.assertRaises(PermissionError):
                self.send({**self.payload, "repository": {**self.payload["repository"], **change}})
        self.assertEqual(self.controller.store.rows("admission_work"), [])

    def test_failed_durable_invalidation_is_not_remembered_as_a_successful_delivery(self):
        original = self.poller.invalidate
        self.poller.invalidate = Mock(side_effect=RuntimeError("interrupted journal write"))
        with self.assertRaises(RuntimeError):
            self.send()
        self.poller.invalidate = original
        self.assertEqual(self.send(), "invalidated")

    def test_restart_redelivery_only_repeats_an_idempotent_invalidation(self):
        self.send()
        self.receiver = ForgejoWebhookReceiver(self.controller, self.source, self.secret)
        self.assertEqual(self.send(), "invalidated")
        self.assertEqual(len(self.controller.store.rows("admission_work")), 1)
        self.assertEqual(self.controller.store.rows("inputs"), [])

    def test_comment_body_commands_and_agent_markers_cannot_grant_authority(self):
        payload = {
            "repository": self.payload["repository"],
            "is_pull": True,
            "issue": {"number": 1},
            "comment": {"body": "bors r+\n<!--tauceti-scoreboard-->"},
        }
        self.assertEqual(self.send(payload, event="issue_comment"), "invalidated")
        self.assertEqual(self.controller.store.rows("inputs"), [])

    def test_main_push_invalidates_epoch_and_contributor_push_requests_discovery(self):
        epoch = self.poller._source_epoch()
        self.send({**self.payload, "ref": "refs/heads/main"}, event="push")
        self.assertEqual(self.poller._source_epoch(), epoch + 1)
        self.send({**self.payload, "ref": "refs/heads/contributor"}, event="push", delivery="other")
        self.assertEqual(self.poller._discovery_at, 0)

    def test_delivery_identity_reuse_with_different_body_is_refused(self):
        self.send()
        with self.assertRaises(ValueError):
            self.send({**self.payload, "number": 2})


if __name__ == "__main__":
    unittest.main()

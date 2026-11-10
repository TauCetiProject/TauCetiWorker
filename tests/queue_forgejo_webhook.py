#!/usr/bin/env python3
"""Native signatures, repository binding, durable invalidation and redelivery."""

import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_requests import policy
from tauceti_tools.forge import PrincipalRef

from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.fakes import make_controller
from tauceti_queue.forgejo_webhook import ForgejoWebhookReceiver


class Webhooks(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.controller, adapter, _repo, clock = make_controller(root.name, repository="team/Math")
        self.adapter = adapter
        self.addCleanup(self.controller.store.close)
        self.poller = AdmissionPoller(
            self.controller,
            adapter,
            "team/Math",
            AdmissionPolicy(rubrics=("proof",), prefix="Math/", allow_files=()),
            clock=clock,
        )
        self.source = policy().source
        self.adapter.admission_policy = SimpleNamespace(
            publishers=SimpleNamespace(
                reviews=frozenset({PrincipalRef(self.source.repository.forge, "8")}),
                recoveries=frozenset({PrincipalRef(self.source.repository.forge, "7")}),
            )
        )
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
        head = self.adapter.create_pr(1, {"Math/A.lean": "example : True := trivial\n"})
        self.payload["pull_request"]["head"]["sha"] = head
        self.assertEqual(self.send(), "invalidated")
        work = self.poller.state.get(1)
        self.assertEqual(work["head_sha"], head)
        self.assertNotEqual(work["generation"], work["evaluated_generation"])
        self.assertEqual(self.controller.store.rows("inputs"), [])
        self.assertEqual(self.send(), "duplicate")
        self.assertEqual(self.poller.state.get(1)["generation"], work["generation"])
        self.poller.run_once()  # The actual primary policy still refuses a PR with no completed review.
        self.assertEqual(self.controller.store.rows("inputs"), [])

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

    def test_main_push_is_durable_and_non_main_retention_pushes_do_not_request_discovery(self):
        epoch = self.poller._source_epoch()
        self.send({**self.payload, "ref": "refs/heads/main"}, event="push")
        self.assertEqual(self.poller._source_epoch(), epoch + 1)
        self.poller._discovery_at = 10**12
        self.controller.store.set_setting("state:admission_discovery", {"retry_at": 10**12})
        self.send({**self.payload, "ref": "refs/heads/contributor"}, event="push", delivery="other")
        self.assertEqual(self.poller._discovery_at, 10**12)
        self.assertEqual(self.controller.store.get_setting("state:admission_discovery")["retry_at"], 10**12)

    def test_routine_prose_and_title_only_changes_do_not_dirty_a_pr(self):
        payload = {
            "repository": self.payload["repository"],
            "is_pull": True,
            "issue": {"number": 1},
            "comment": {"body": "Routine conversation"},
            "action": "created",
        }
        self.assertEqual(self.send(payload, event="issue_comment"), "ignored")
        edited = {**self.payload, "action": "edited", "changes": {"title": {"from": "old"}}}
        self.assertEqual(self.send(edited, delivery="title"), "ignored")
        self.assertIsNone(self.poller.state.get(1))

    def test_native_review_events_and_social_metadata_cannot_cancel_an_evaluation(self):
        head = self.adapter.create_pr(1, {"Math/A.lean": "example : True := trivial\n"})
        self.payload["pull_request"]["head"]["sha"] = head
        self.send()
        work = self.poller.state.get(1)
        for name in ["pull_request_approved", "pull_request_rejected", "pull_request_comment"]:
            self.assertEqual(self.send(event=name, delivery=name.replace("_", "-")), "ignored")
        for action in [
            "assigned",
            "unassigned",
            "review_requested",
            "review_request_removed",
            "milestoned",
            "demilestoned",
        ]:
            self.assertEqual(
                self.send({**self.payload, "action": action}, delivery=action.replace("_", "-")), "ignored"
            )
        self.assertEqual(self.poller.state.get(1), work)
        self.poller.run_once()
        self.assertTrue(self.poller.is_current(1, self.poller.state.get(1)["head_sha"]))

    def test_non_pull_comments_are_ignored(self):
        issue = {
            "repository": self.payload["repository"],
            "is_pull": False,
            "issue": {"number": 2},
            "comment": {"body": "<!--tauceti-scoreboard-->"},
        }
        self.assertEqual(self.send(issue, event="issue_comment", delivery="not-a-pr"), "ignored")
        self.assertIsNone(self.poller.state.get(2))

    def test_output_and_nonpublisher_markers_are_ignored_but_moderated_publisher_comments_are_urgent(self):
        from tauceti_worker.source_identity import SourceActor

        self.controller.source_actor = SourceActor.from_native(
            self.source.repository.forge, {"id": 7, "login": "queue"}
        )
        payload = {
            "repository": self.payload["repository"],
            "is_pull": True,
            "issue": {"number": 1},
            "sender": {"id": 7},
            "comment": {"body": "<!--tauceti-scoreboard-->"},
        }
        self.assertEqual(self.send(payload, event="issue_comment"), "ignored")
        payload["sender"] = {"id": 99}
        payload["comment"]["user"] = {"id": 99}
        self.assertEqual(self.send(payload, event="issue_comment", delivery="non-publisher"), "ignored")
        self.assertIsNone(self.poller.state.get(1))
        payload["comment"]["user"] = {"id": 8}
        payload["action"] = "edited"
        self.assertEqual(self.send(payload, event="issue_comment", delivery="moderation"), "invalidated")
        self.assertEqual(self.poller.state.get(1)["urgent"], 1)

    def test_malformed_native_structures_are_refused_as_bad_payloads(self):
        for change in [{"pull_request": []}, {"pull_request": {"head": []}}, {"sender": []}]:
            # Non-empty wrong-shaped values cannot be treated as absent records.
            change = {k: ([1] if v == [] else {"head": [1]}) for k, v in change.items()}
            with self.assertRaises(ValueError):
                self.send({**self.payload, **change})

    def test_delivery_identity_reuse_with_different_body_is_refused(self):
        self.send()
        with self.assertRaises(ValueError):
            self.send({**self.payload, "number": 2})


if __name__ == "__main__":
    unittest.main()

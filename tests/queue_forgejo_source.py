#!/usr/bin/env python3
"""Native source receipts, publication recovery and qualified task metadata."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, Forge, ForgeError, Incomplete, PrincipalRef, RepositoryRef, Unsupported

from tauceti_queue import chain, ports, repair_task
from tauceti_queue.forgejo_source import (
    BINDING,
    PUB_DESCRIPTION,
    RECOVERY_CONTEXT,
    REVIEW_CONTEXT,
    ForgejoSourceAdapter,
    NativeAdmissionPolicy,
    NativePublishers,
    NativePullRequest,
    PublicationDamaged,
    PublicationSuperseded,
)
from tauceti_queue.store import Store
from tauceti_queue.task_identity import NATIVE_GIT_REFS
from tauceti_worker.source_identity import SourceActor


class NativeSource(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clone = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True, capture_output=True)
        self.repository = RepositoryRef(Forge("forgejo", "https://source.example"), "team/Math")
        self.identity = RepositoryIdentity(self.repository, 41)
        self.actor = SourceActor.from_native(self.repository.forge, {"id": 7, "login": "reviewer"})
        self.queue = SourceActor.from_native(self.repository.forge, {"id": 8, "login": "queue"})
        self.publishers = NativePublishers(
            self.identity,
            frozenset({self.actor.principal}),
            frozenset({self.queue.principal}),
            frozenset({self.queue.principal}),
            generation="1" * 32,
        )
        self.policy = NativeAdmissionPolicy(self.publishers, rubrics=("proof",), prefix="Math/", allow_files=())
        self.rows = []
        self.status_rows = []
        self.permission = "write"
        self.calls = []
        self.sequence = 0

        def request(method, path, data=None, **kwargs):
            self.calls.append((method, path, data))
            if path.endswith("/permission"):
                actor = self.queue if "/queue/" in path else self.actor
                return {
                    "user": {"id": int(actor.principal.native_id), "login": actor.login},
                    "permission": self.permission,
                }
            if method == "POST" and "/statuses/" in path:
                row = {
                    "id": 100 + len(self.status_rows),
                    "creator": self.api.current_user(),
                    "context": data["context"],
                    "status": data["state"],
                    "description": data["description"],
                }
                self.status_rows.append(row)
                return copy.deepcopy(row)
            raise AssertionError((method, path))

        def post(repository, number, body):
            row = {
                "id": 1000 + len(self.rows),
                "body": body,
                "user": self.api.current_user(),
                "created_at": "2026-11-08T00:00:00Z",
                "updated_at": "2026-11-08T00:00:00Z",
            }
            self.rows.append(row)
            return copy.deepcopy(row)

        def edit(repository, comment_id, body):
            row = next(row for row in self.rows if row["id"] == comment_id)
            row["body"] = body
            return copy.deepcopy(row)

        self.api = SimpleNamespace(
            forge=self.repository.forge,
            repository=Mock(return_value={"id": 41, "full_name": "team/Math"}),
            request=Mock(side_effect=request),
            comments=lambda *_: copy.deepcopy(self.rows),
            statuses=lambda *_: copy.deepcopy(self.statuses()),
            current_user=Mock(return_value={"id": 7, "login": "reviewer"}),
            post_comment=Mock(side_effect=post),
            edit_comment=Mock(side_effect=edit),
        )
        self.source = ForgejoSourceAdapter(
            self.identity, self.api, self.clone, token="token", admission_policy=self.policy, clock=lambda: 1000
        )
        with closing(Store(str(self.clone / "publisher-policy.sqlite"))) as store:
            self.source.configure_publisher_policy(store)
        self.source.fetch_pr = Mock(return_value=SimpleNamespace(head_sha="a" * 40, state="open"))
        self.body = (
            "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 "
            + json.dumps({"head_sha": "a" * 40, "merge_base_sha": "b" * 40, "states": {"proof": "green"}})
            + "-->"
        )

    def comment(self, *, actor=None, body=None, comment_id=1):
        actor = actor or self.actor
        return {
            "id": comment_id,
            "body": self.policy.bind_comment(body or self.body, actor),
            "user": {"id": int(actor.principal.native_id), "login": actor.login},
            "created_at": "2026-11-08T00:00:00Z",
            "updated_at": "2026-11-08T00:00:00Z",
        }

    def statuses(self):
        return [item for row in self.status_rows for item in (row if isinstance(row, list) else [row])]

    def prepare(self, *args, **kwargs):
        self.sequence += 1
        return self.source.prepare_publication(*args, sequence=kwargs.pop("sequence", self.sequence), **kwargs)

    def stamp(self, row, *, actor=None, status_id=1, kind="review"):
        actor = actor or self.actor
        prefix = PUB_DESCRIPTION + f"{self.source.admission_policy.publishers.generation}:{status_id}:{status_id:032x}:"
        common = {
            "context": (REVIEW_CONTEXT if kind == "review" else RECOVERY_CONTEXT) + "1",
            "creator": {"id": int(actor.principal.native_id), "login": actor.login},
            "created_at": "2026-11-08T00:00:00Z",
        }
        body_hash = hashlib.sha256(row["body"].encode()).hexdigest()
        return [
            dict(common, id=status_id * 2, status="pending", description=prefix + "pending:" + body_hash),
            dict(common, id=status_id * 2 + 1, status="success", description=prefix + str(row["id"]) + ":" + body_hash),
        ]

    def read(self):
        return self.source._comments(1, "a" * 40, self.statuses())

    def controller(self):
        from queue_ci_requests import CONFIG
        from queue_ci_requests import policy as execution_policy
        from queue_source_ci import VerifiedExecution

        from tauceti_queue.ci_requests import RequestLedger
        from tauceti_queue.controller import Controller
        from tauceti_queue.fakes import FakeClock
        from tauceti_queue.source_ci import SourceCandidateCI

        store = Store(str(self.clone / "controller.sqlite"))
        self.addCleanup(store.close)
        clock = FakeClock()
        ledger = RequestLedger(store, self.clone, execution_policy(), CONFIG, workflow_revision="b" * 40)
        ci = SourceCandidateCI(ledger, VerifiedExecution(), clock=clock)
        controller = Controller(
            store,
            self.source,
            str(self.clone),
            self.repository.name,
            revision="native-outbox",
            candidate_ci=ci,
            canonical_branch="main",
            clock=clock,
        )
        controller.set_setting("publication_enabled", True, "owned fixture")
        return controller, clock

    def test_native_policy_requires_qualified_source_local_publishers(self):
        with self.assertRaises(ValueError):
            NativePublishers(self.identity, frozenset(), self.publishers.statuses, generation="1" * 32)
        with self.assertRaises(ValueError):
            NativePublishers(
                self.identity,
                frozenset({PrincipalRef(Forge("forgejo", "https://other.example"), "7")}),
                self.publishers.statuses,
                generation="1" * 32,
            )
        other = replace(self.publishers, source=replace(self.identity, native_id=42))
        changed = NativeAdmissionPolicy(other, rubrics=("proof",), prefix="Math/", allow_files=())
        with self.assertRaises(ValueError):
            ForgejoSourceAdapter(self.identity, self.api, self.clone, token="token", admission_policy=changed)
        self.assertNotEqual(self.policy.revision, changed.revision)

    def test_native_controller_cannot_fall_back_to_github_execution(self):
        from tauceti_queue.controller import Controller

        with closing(Store(str(self.clone / "queue.sqlite"))) as store:
            with self.assertRaisesRegex(ValueError, "qualified source CI"):
                Controller(store, self.source, str(self.clone), self.repository.name, revision="fixture")

    def test_current_generation_with_an_unsupported_protocol_never_falls_back(self):
        row = self.comment()
        self.rows = [row]
        self.status_rows = [self.stamp(row)]
        self.status_rows.append(
            {
                "id": 99,
                "context": REVIEW_CONTEXT + "1",
                "status": "success",
                "creator": {"id": 7, "login": "reviewer"},
                "description": "tauceti-pub/v99:" + self.publishers.generation + ":future",
            }
        )
        with self.assertRaises(Incomplete):
            self.read()

    def test_legacy_receipts_and_unknown_newer_publishers_never_grant_authority(self):
        row = self.comment()
        self.rows = [row]
        self.status_rows = [
            {
                "id": 1,
                "context": REVIEW_CONTEXT + "1",
                "status": "success",
                "creator": {"id": 7, "login": "reviewer"},
                "description": "tauceti-pub/v1:1:" + hashlib.sha256(row["body"].encode()).hexdigest(),
            }
        ]
        self.assertEqual(self.read()[1], "absent")
        self.status_rows = [self.stamp(row)]
        outsider = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "writer"})
        forged = self.comment(actor=outsider, comment_id=2)
        self.rows.append(forged)
        self.status_rows.append(self.stamp(forged, actor=outsider, status_id=2))
        self.assertEqual(self.read()[0], (row,))

    def test_deleting_an_untrusted_actor_cannot_reveal_an_older_green(self):
        row = self.comment()
        self.rows = [row]
        outsider = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "writer"})
        newer = self.stamp(self.comment(actor=outsider, comment_id=2), actor=outsider, status_id=2)
        self.status_rows = [self.stamp(row), newer]
        self.assertEqual(self.read()[0], (row,))
        for stamp in newer:
            stamp["creator"] = None
        self.assertEqual(self.read()[1], "revoked")

    def test_nonpublisher_cannot_poison_a_head_with_an_unrecoverable_sequence_or_malformed_record(self):
        red = self.comment(body=self.body.replace("green", "blocking_request"))
        self.rows = [red]
        self.status_rows = [self.stamp(red)]
        descriptions = [
            PUB_DESCRIPTION + self.publishers.generation + f":{2**63 - 1}:" + "f" * 32 + ":pending:" + "f" * 64,
            PUB_DESCRIPTION + self.publishers.generation + ":malformed",
            "tauceti-pub/v99:" + self.publishers.generation + ":future",
        ]
        for i, description in enumerate(descriptions):
            self.status_rows.append(
                {
                    "id": 100 + i,
                    "context": REVIEW_CONTEXT + "1",
                    "status": "success",
                    "creator": {"id": 99, "login": "writer"},
                    "description": description,
                }
            )
        self.assertEqual(self.read()[0], (red,))

    def test_imported_unbound_and_spoofed_comment_authors_cannot_authorize(self):
        row = self.comment()
        self.rows = [row]
        self.assertEqual(self.read()[0], ())  # No immutable issuer receipt.
        self.status_rows = [self.stamp(row)]
        row["user"] = {"id": 99, "login": "reviewer"}
        with self.assertRaises(Incomplete):
            self.read()
        row["user"] = {"id": 7, "login": "reviewer"}
        row["body"] = self.body
        self.status_rows = [self.stamp(row)]
        with self.assertRaises(Incomplete):
            self.read()

    def test_moderation_or_deletion_never_exposes_an_older_green_body(self):
        green = self.comment()
        red = self.comment(body=self.body.replace("green", "blocking_request"), comment_id=2)
        self.rows = [green, red]
        self.status_rows = [self.stamp(green), self.stamp(red, status_id=2)]
        self.assertEqual(self.read()[0], (red,))
        red["body"] = green["body"]
        with self.assertRaises(Incomplete):
            self.read()
        self.rows = [green]
        with self.assertRaises(Incomplete):
            self.read()

    def test_revoked_latest_publisher_is_not_replaced_with_an_older_green(self):
        green = self.comment()
        self.rows = [green]
        self.status_rows = [self.stamp(green)]
        self.assertEqual(self.read()[0], (green,))
        self.permission = "read"
        self.assertEqual(self.read(), ((), "revoked", "absent"))
        self.api.request.side_effect = ForgeError("outage", status=503)
        with self.assertRaises(ForgeError):
            self.read()

    def test_malformed_source_binding_is_unavailable_not_ignored(self):
        row = self.comment()
        row["body"] = row["body"].replace('"id":41', '"id":42')
        self.rows = [row]
        self.status_rows = [self.stamp(row)]
        with self.assertRaises(Incomplete):
            self.read()

    def test_pending_publication_blocks_the_older_complete_review(self):
        row = self.comment()
        self.rows = [row]
        self.status_rows = [
            self.stamp(row),
            {
                "id": 20,
                "context": REVIEW_CONTEXT + "1",
                "status": "pending",
                "creator": {"id": 7, "login": "reviewer"},
                "description": PUB_DESCRIPTION + "1" * 32 + ":2:" + "c" * 32 + ":pending:" + "c" * 64,
            },
        ]
        self.assertEqual(self.read(), ((), "pending", "absent"))

    def test_permission_response_cannot_borrow_another_native_user_id(self):
        self.api.request.side_effect = None
        self.api.request.return_value = {"permission": "admin", "user": {"id": 99, "login": "reviewer"}}
        with self.assertRaises(Incomplete):
            self.source._permission(self.actor)
        with self.assertRaises(ValueError):
            self.source.repository_admin("reviewer")

    def test_latest_trusted_ci_failure_cannot_be_hidden_by_a_contributor(self):
        def status(i, actor, state):
            return {
                "id": i,
                "description": self.source.ci_status_description,
                "context": "build",
                "status": state,
                "creator": {"id": int(actor.principal.native_id), "login": actor.login},
            }

        self.status_rows = [
            status(4, self.actor, "success"),
            status(3, self.queue, "failure"),
            status(2, self.queue, "success"),
        ]
        self.assertEqual(self.source._statuses("a" * 40), {"build": "error"})

    def test_publication_retry_observes_pending_without_resending_the_intent(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review-marker")
        original = self.api.post_comment.side_effect

        def lose(*args):
            original(*args)
            raise AmbiguousWrite("lost accepted comment response")

        self.api.post_comment.side_effect = lose
        with self.assertRaises(AmbiguousWrite):
            self.source.publish(intent)
        self.assertEqual(self.read()[1], "pending")
        self.api.post_comment.side_effect = original
        self.assertEqual(self.source.publish(intent), "1000")
        self.assertEqual(len(self.status_rows), 2)
        self.assertEqual(self.api.post_comment.call_count, 1)
        self.assertEqual(self.source.publish(intent), "1000")
        self.assertEqual(len(self.status_rows), 2)

    def test_older_frozen_intent_cannot_overwrite_a_newer_publication(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "first")
        self.source.publish(first)
        second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNew verdict.", "second")
        self.source.publish(second)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(len(self.status_rows), 4)

    def test_intent_that_never_started_cannot_overwrite_a_newer_verdict(self):
        old = self.prepare("team/Math", 1, "a" * 40, self.body, "same")
        new = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNew", "same")
        newest = self.source.publish(new)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(old)
        self.assertEqual(str(self.read()[0][0]["id"]), newest)

    def test_offline_intents_delivered_in_order_keep_the_newer_red_verdict(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        second = self.prepare("team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "review")
        self.api.repository.side_effect = ForgeError("outage before registration", status=503)
        with self.assertRaises(ForgeError):
            self.source.publish(first)
        self.api.repository.side_effect = None
        self.source.publish(first)
        expected = self.source.publish(second)
        self.assertEqual(str(self.read()[0][0]["id"]), expected)
        self.assertIn("blocking_request", self.read()[0][0]["body"])

    def test_offline_init_and_final_can_both_be_prepared_before_either_is_sent(self):
        init = self.prepare(
            "team/Math", 1, "a" * 40, self.body.replace('"states":', '"mode":"init","states":'), "review"
        )
        final = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        self.source.publish(init)
        self.source.publish(final)
        self.assertNotIn('"mode":"init"', self.read()[0][0]["body"])

    def test_equal_sequences_with_different_intents_fail_closed(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review", sequence=1)
        second = self.prepare(
            "team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "review", sequence=1
        )
        self.source.publish(first)
        with self.assertRaises(Incomplete):
            self.source.publish(second)

    def test_damaged_completed_intent_requires_republication_without_repeating_inference(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        original_id = self.source.publish(intent)
        self.rows[0]["body"] = "Moderated"
        with self.assertRaises(PublicationDamaged):
            self.source.publish(intent)
        replacement = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        self.assertNotEqual(self.source.publish(replacement), original_id)
        self.assertEqual(self.read()[1], "success")

    def test_a_frozen_clear_cannot_erase_a_newer_recovery_hold(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        first = self.prepare("team/Math", 1, "a" * 40, "First hold", "hold", kind="recovery")
        self.source.publish(first)
        clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
        newest = self.prepare("team/Math", 1, "a" * 40, "New hold", "hold", kind="recovery")
        self.source.publish(newest)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(clear)
        self.assertEqual(self.read()[2], "success")

    def test_new_intents_with_identical_bodies_can_follow_a_recovery_clear(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        first = self.prepare("team/Math", 1, "a" * 40, "Hold", "same", kind="recovery")
        self.source.publish(first)
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True))
        again = self.prepare("team/Math", 1, "a" * 40, "Hold", "same", kind="recovery")
        self.assertNotEqual(first["nonce"], again["nonce"])
        self.source.publish(again)
        self.assertEqual(self.read()[2], "success")

    def test_concurrent_publisher_cannot_edit_the_newer_intents_comment(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        original = self.source._upsert_comment
        nested = []

        def interleave(number, full, marker, actor):
            second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
            with patch.object(self.source, "_upsert_comment", side_effect=original):
                nested.append(self.source.publish(second))
            return original(number, full, marker, actor)

        with patch.object(self.source, "_upsert_comment", side_effect=interleave):
            with self.assertRaises(PublicationSuperseded):
                self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])
        self.assertIn("Newest", self.read()[0][0]["body"])
        self.api.edit_comment.assert_not_called()

    def test_late_success_after_newer_pending_cannot_revive_an_older_review(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        original = self.api.request.side_effect
        nested = []

        def interleave(method, path, data=None, **kwargs):
            if (
                method == "POST"
                and data
                and data.get("description", "").startswith(self.source._intent_prefix(first))
                and data["state"] == "success"
            ):
                second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
                with patch.object(self.api.request, "side_effect", original):
                    nested.append(self.source.publish(second))
            return original(method, path, data, **kwargs)

        self.api.request.side_effect = interleave
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])

    def test_competing_pending_registration_cannot_acquire_a_stale_predecessor(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
        original = self.api.request.side_effect
        nested = []

        def interleave(method, path, data=None, **kwargs):
            if method == "POST" and data and data.get("description", "").startswith(self.source._intent_prefix(first)):
                with patch.object(self.api.request, "side_effect", original):
                    nested.append(self.source.publish(second))
            return original(method, path, data, **kwargs)

        self.api.request.side_effect = interleave
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])

    def test_publisher_reconciliation_does_not_require_other_user_admin_lookup(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        expected = self.source.publish(intent)
        with patch.object(self.source, "_permission", side_effect=ForgeError("forbidden", status=403)):
            self.assertEqual(self.source.publish(intent), expected)

    def test_review_rotation_requires_fresh_publication_after_queue_restart(self):
        green = self.comment(actor=self.queue)
        red = self.comment(comment_id=2, body=self.body.replace("green", "blocking_request"))
        self.rows = [green, red]
        self.status_rows = [self.stamp(green, actor=self.queue), self.stamp(red, status_id=2)]
        with closing(Store(str(self.clone / "history.sqlite"))) as store:
            self.source.configure_publisher_policy(store)
        with closing(Store(str(self.clone / "history.sqlite"))) as store:
            changed = replace(self.publishers, reviews=frozenset({self.queue.principal}), generation="2" * 32)
            self.source = ForgejoSourceAdapter(
                self.identity,
                self.api,
                self.clone,
                token="token",
                admission_policy=NativeAdmissionPolicy(changed, rubrics=("proof",), prefix="Math/", allow_files=()),
            )
            self.source.configure_publisher_policy(store)
            self.assertEqual(self.read(), ((), "absent", "absent"))

    def test_policy_rotations_cannot_reactivate_preplanted_publications(self):
        row = self.comment()
        self.rows = [row]
        self.status_rows = [self.stamp(row)]
        with closing(Store(str(self.clone / "policy.sqlite"))) as store:
            self.source.configure_publisher_policy(store)
            removed = replace(self.publishers, reviews=frozenset({self.queue.principal}))
            self.source.admission_policy = NativeAdmissionPolicy(
                removed, rubrics=("proof",), prefix="Math/", allow_files=()
            )
            with self.assertRaises(ValueError):
                self.source.configure_publisher_policy(store)
            removed = replace(removed, generation="2" * 32)
            self.source.admission_policy.publishers = removed
            self.source.configure_publisher_policy(store)
            planted = self.comment()
            self.rows.append(planted)
            self.status_rows.append(self.stamp(planted, status_id=2))
            self.assertEqual(self.read()[1], "absent")
            restored = replace(self.publishers, generation="3" * 32)
            self.source.admission_policy.publishers = restored
            self.source.configure_publisher_policy(store)
            self.assertEqual(self.read()[1], "absent")
            intent = self.prepare("team/Math", 1, "a" * 40, self.body, "new-grant", sequence=1)
            self.source.publish(intent)
            self.assertEqual(self.read()[1], "success")
            self.source.admission_policy.publishers = self.publishers
            with self.assertRaises(ValueError):
                self.source.configure_publisher_policy(store)

    def test_generation_rotation_preserves_a_hold_until_a_new_explicit_clear(self):
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, self.body, "review", sequence=1))
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        hold = "Merge-queue recovery for head `" + "a" * 40 + "`\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->"
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, hold, "hold", kind="recovery", sequence=1))
        changed = replace(self.publishers, generation="2" * 32)
        self.source.admission_policy = NativeAdmissionPolicy(
            changed, rubrics=("proof",), prefix="Math/", allow_files=()
        )
        self.api.current_user.return_value = {"id": 7, "login": "reviewer"}
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, self.body, "new-review", sequence=1))

        def decision():
            comments, review, recovery = self.read()
            snapshot = ports.AdmissionSnapshot(
                ports.PullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open"),
                "b" * 40,
                ("Math/A.lean",),
                comments,
                {"build": "success", "scope": "success", "_native_review": review, "_native_recovery": recovery},
            )
            return self.source.admission_policy.evaluate(snapshot)[0]

        self.assertEqual(self.read()[2], "revoked")
        self.assertFalse(decision()["merge"])
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        clear = self.prepare("team/Math", 1, "a" * 40, "", "new-clear", kind="recovery", clear=True, sequence=1)
        self.source.publish(clear)
        self.assertTrue(decision()["merge"])

    def test_quoted_scoreboards_are_operational_prose_even_if_queue_is_also_a_reviewer(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        self.source.admission_policy.publishers = replace(self.publishers, reviews=frozenset({self.queue.principal}))
        for body in ["Agent quoted <!--tauceti-scoreboard-->", "Agent quoted:\n> " + self.body]:
            self.assertIsNone(self.source.prepare_outbox_comment("team/Math", 1, body, "quoted", sequence=1))
            self.source.post_comment("team/Math", 1, body, "quoted")
        self.assertEqual(self.status_rows, [])
        self.assertNotIn("<!--tauceti-scoreboard-->", self.rows[0]["body"])
        with self.assertRaises(ValueError):
            self.source.prepare_outbox_comment(
                "team/Math",
                1,
                self.body,
                "bad-kind",
                sequence=1,
                publication={"kind": "review", "head": "a" * 40, "clear": False},
            )

    def test_closed_or_temporarily_retargeted_heads_do_not_discard_a_new_verdict(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        red = self.prepare("team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "review")
        self.source.publish(first)
        self.source.fetch_pr.side_effect = AssertionError("current PR state cannot discard accepted-head facts")
        self.source.publish(red)
        self.assertIn("blocking_request", self.read()[0][0]["body"])

    def test_recovery_hold_clear_and_next_hold_use_one_durable_allocator(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        ids = [controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="first")]
        ids.append(controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="clear", clear=True))
        ids.append(controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold again", key="last"))
        self.assertEqual(ids, sorted(set(ids)))
        controller._drain_outbox(clock())
        self.assertEqual(self.read()[2], "success")
        self.assertIn("Hold again", self.read()[0][-1]["body"])
        self.assertEqual(controller.store.rows("outbox", {"state": "pending"}), [])

    def test_rotation_rebinds_frozen_hold_intents_and_never_replays_old_clears(self):
        from tauceti_queue.controller import Controller

        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        hold_id = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="first")
        with patch.object(self.api, "post_comment", side_effect=OSError("outage")):
            controller._drain_outbox(clock())
        self.source.admission_policy = NativeAdmissionPolicy(
            replace(self.publishers, generation="2" * 32), rubrics=("proof",), prefix="Math/", allow_files=()
        )
        restarted = Controller(
            controller.store,
            self.source,
            str(self.clone),
            self.repository.name,
            revision="rotated",
            candidate_ci=controller.candidate_ci,
            canonical_branch="main",
            clock=clock,
        )
        self.assertEqual(controller.store.row("outbox", {"id": hold_id})["state"], "pending")
        clock.advance(60)
        restarted._drain_outbox(clock())
        updated = json.loads(controller.store.row("outbox", {"id": hold_id})["payload"])["native_intent"]
        self.assertEqual(updated["generation"], "2" * 32)
        self.assertEqual(self.read()[2], "success")
        clear_id = restarted.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="clear", clear=True)
        restarted._drain_outbox(clock())
        self.source.admission_policy.publishers = replace(self.publishers, generation="3" * 32)
        Controller(
            controller.store,
            self.source,
            str(self.clone),
            self.repository.name,
            revision="rotated-again",
            candidate_ci=controller.candidate_ci,
            canonical_branch="main",
            clock=clock,
        )
        self.assertEqual(controller.store.row("outbox", {"id": clear_id})["state"], "done")
        self.assertEqual(self.read()[2], "revoked")

    def test_moderated_owned_hold_requeues_once_without_overriding_a_newer_desired_clear(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        entry = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="hold")
        controller._drain_outbox(clock())
        intent = json.loads(controller.store.row("outbox", {"id": entry})["payload"])["native_intent"]
        self.rows[0]["body"] = "Moderated"
        self.assertTrue(controller.reconcile_source_recovery(1, "a" * 40, intent["nonce"]))
        self.assertFalse(controller.reconcile_source_recovery(1, "a" * 40, intent["nonce"]))
        controller._drain_outbox(clock())
        repaired = controller.store.rows("outbox", order="id")[-1]
        repaired_intent = json.loads(repaired["payload"])["native_intent"]
        self.assertEqual(self.read()[2], "success")
        controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="clear", clear=True)
        self.assertFalse(controller.reconcile_source_recovery(1, "a" * 40, repaired_intent["nonce"]))

    def test_missing_recovery_grant_settles_as_reconciliation_without_permanent_retry(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        entry = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="hold")
        self.source.admission_policy.publishers = replace(self.publishers, recoveries=frozenset())
        controller._drain_outbox(clock())
        settled = controller.store.row("outbox", {"id": entry})
        self.assertEqual(settled["state"], "done")
        self.assertIn("reconciliation-required", settled["result"])

    def test_pending_prepared_and_unprepared_clears_never_survive_authority_rotation(self):
        from tauceti_queue.controller import Controller

        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        prepared = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="prepared", clear=True)
        with patch.object(self.source, "publish", side_effect=OSError("outage")):
            controller._drain_outbox(clock())
        unprepared = controller.enqueue_source_recovery("team/Math", 2, "a" * 40, "Clear", key="unprepared", clear=True)
        self.source.admission_policy = NativeAdmissionPolicy(
            replace(self.publishers, generation="2" * 32), rubrics=("proof",), prefix="Math/", allow_files=()
        )
        restarted = Controller(
            controller.store,
            self.source,
            str(self.clone),
            self.repository.name,
            revision="restore",
            candidate_ci=controller.candidate_ci,
            canonical_branch="main",
            clock=clock,
        )
        clock.advance(60)
        restarted._drain_outbox(clock())
        for row_id in (prepared, unprepared):
            row = controller.store.row("outbox", {"id": row_id})
            self.assertEqual(row["state"], "done")
            self.assertIn("clear predates authority rotation", row["result"])
        self.assertEqual(self.status_rows, [])

    def test_unresolved_or_synthetic_issuer_cannot_poison_sequence_permanently(self):
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, self.body, "original"))
        for i, creator in enumerate([None, {"id": -2, "login": "forgejo-actions"}, {"id": -1, "login": "ghost"}]):
            self.status_rows.append(
                {
                    "id": 1000 + i,
                    "context": REVIEW_CONTEXT + "1",
                    "status": "pending",
                    "creator": creator,
                    "description": PUB_DESCRIPTION
                    + self.publishers.generation
                    + ":"
                    + str(2**63 - 1)
                    + ":"
                    + "f" * 32
                    + ":pending:"
                    + "f" * 64,
                }
            )
        self.assertEqual(self.read()[1], "revoked")
        # Simulate native increasing server IDs after the injected rows.
        original = self.api.request.side_effect

        def native_ids(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[0] == "POST" and "/statuses/" in args[1]:
                self.status_rows[-1]["id"] = 2000 + len(self.status_rows)
            return value

        self.api.request.side_effect = native_ids
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, self.body, "reconciled"))
        self.assertEqual(self.read()[1], "success")

    def test_frozen_intents_respect_publication_disabled_and_os_permission_errors_still_retry(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        row_id = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="hold")
        with patch.object(self.source, "publish", side_effect=PermissionError("socket OS EACCES")):
            controller._drain_outbox(clock())
        row = controller.store.row("outbox", {"id": row_id})
        self.assertEqual(row["state"], "pending")
        self.assertIn("native_intent", json.loads(row["payload"]))
        controller.set_setting("publication_enabled", False, "operator")
        clock.advance(60)
        with patch.object(self.source, "publish", side_effect=AssertionError("disabled frozen intent was sent")):
            controller._drain_outbox(clock())
        self.assertEqual(controller.store.row("outbox", {"id": row_id})["state"], "pending")

    def test_recovery_keys_cannot_silently_change_semantics(self):
        controller, _clock = self.controller()
        first = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="stable")
        self.assertEqual(controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="stable"), first)
        with self.assertRaisesRegex(ValueError, "reused"):
            controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="stable", clear=True)

    def test_damaged_review_is_unsafe_and_refuses_promotion_instead_of_deferring_forever(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        self.source.publish(first)
        self.rows[0]["body"] = "Moderated after confirmation"
        pull = ports.PullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open")
        self.source.fetch_pr.return_value = pull
        frozen = SimpleNamespace(
            policy=SimpleNamespace(source=self.identity, base_branch="main"),
            request=SimpleNamespace(
                source=self.identity,
                base="b" * 40,
                head="c" * 40,
                accepted_members=(SimpleNamespace(pr=1, head="a" * 40),),
            ),
        )
        with (
            patch.object(self.source, "_merge_base", return_value="b" * 40),
            patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")),
        ):
            snapshot = self.source.admission_snapshot("team/Math", 1)
            self.assertEqual(snapshot.statuses["_native_review"], "damaged")
            self.assertFalse(self.source.admission_policy.evaluate(snapshot)[0]["review_safe"])
            with self.assertRaises(ports.PromotionRefused):
                self.source.verify_candidate_approval(frozen)

    def test_unknown_remote_owned_recovery_reports_restore_reconciliation(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        controller, clock = self.controller()
        row_id = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Hold", key="local")
        remote = self.prepare(
            "team/Math", 1, "a" * 40, "Remote later hold", "unknown-to-backup", kind="recovery", sequence=99
        )
        self.source.publish(remote)
        controller._drain_outbox(clock())
        settled = controller.store.row("outbox", {"id": row_id})
        self.assertIn("reconcile restore", settled["result"])
        self.assertTrue(controller.store.rows("journal", {"kind": "native-publication-history-mismatch"}))

    def test_unverified_barrier_preserves_newer_trusted_hold_over_an_old_clear(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        old_clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True, sequence=5)
        hold = self.prepare("team/Math", 1, "a" * 40, "Newer hold", "hold", kind="recovery", sequence=9)
        self.source.publish(hold)
        self.status_rows.append(
            {
                "id": 1000,
                "context": RECOVERY_CONTEXT + "1",
                "status": "pending",
                "creator": None,
                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
            }
        )
        with self.assertRaises(PublicationSuperseded) as refused:
            self.source.publish(old_clear)
        self.assertEqual(refused.exception.sequence, 9)
        self.assertEqual(self.read()[2], "revoked")
        original = self.api.request.side_effect

        def native_ids(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "POST" and "/statuses/" in args[1]:
                self.status_rows[-1]["id"] = 2000 + len(self.status_rows)
            return result

        self.api.request.side_effect = native_ids
        self.source.publish(hold)  # Exact highest intent may reconcile its own registration after the barrier.
        self.assertEqual(self.read()[2], "success")
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(old_clear)

    def test_unverified_barrier_never_revives_old_green_over_a_newer_red(self):
        old = self.prepare("team/Math", 1, "a" * 40, self.body, "green", sequence=5)
        red = self.prepare("team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "red", sequence=6)
        self.source.publish(red)
        self.status_rows.append(
            {
                "id": 1000,
                "context": REVIEW_CONTEXT + "1",
                "status": "pending",
                "creator": None,
                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
            }
        )
        with self.assertRaises(PublicationSuperseded) as refused:
            self.source.publish(old)
        self.assertEqual(refused.exception.sequence, 6)
        self.assertEqual(self.read()[1], "revoked")

    def test_restored_publisher_history_cannot_release_a_remote_hold_in_a_missing_generation(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        path = self.clone / "restore-policy.sqlite"
        with closing(Store(str(path))) as store:
            self.source.configure_publisher_policy(store)
            saved = copy.deepcopy(store.query("SELECT * FROM settings"))
            self.source.admission_policy = NativeAdmissionPolicy(
                replace(self.publishers, generation="2" * 32), rubrics=("proof",), prefix="Math/", allow_files=()
            )
            self.source.configure_publisher_policy(store)
            self.source.publish(
                self.prepare("team/Math", 1, "a" * 40, "Remote newer hold", "hold", kind="recovery", sequence=9)
            )
            store.execute("DELETE FROM settings")
            for row in saved:
                store.set_setting(row["key"], json.loads(row["value"]))
            fresh = ForgejoSourceAdapter(
                self.identity,
                self.api,
                self.clone,
                token="token",
                admission_policy=NativeAdmissionPolicy(
                    replace(self.publishers, generation="3" * 32), rubrics=("proof",), prefix="Math/", allow_files=()
                ),
            )
            with self.assertRaises(Incomplete):
                fresh._comments(1, "a" * 40)
            fresh.configure_publisher_policy(store)
            self.assertEqual(fresh._comments(1, "a" * 40)[2], "revoked")

    def test_fresh_publisher_process_requires_its_durable_binding_before_read_or_write(self):
        fresh = ForgejoSourceAdapter(self.identity, self.api, self.clone, token="token", admission_policy=self.policy)
        with self.assertRaises(Incomplete):
            fresh.prepare_publication("team/Math", 1, "a" * 40, self.body, "review", sequence=1)
        with self.assertRaises(Incomplete):
            fresh._statuses("a" * 40)
        with closing(Store(str(self.clone / "fresh-policy.sqlite"))) as store:
            fresh.configure_publisher_policy(store)
            self.assertEqual(fresh._comments(1, "a" * 40), ((), "absent", "absent"))

    def test_barriers_during_registration_or_completion_leave_the_exact_clear_retryable(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        original = self.api.request.side_effect
        for stage in ["pending", "success"]:
            with self.subTest(stage=stage):
                clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
                injected = []

                def insert_barrier(*args, stage=stage, injected=injected, **kwargs):
                    value = original(*args, **kwargs)
                    if args[0] == "POST" and "/statuses/" in args[1] and args[2]["state"] == stage and not injected:
                        injected.append(True)
                        self.status_rows.append(
                            {
                                "id": 100 + len(self.status_rows),
                                "context": RECOVERY_CONTEXT + "1",
                                "status": "pending",
                                "creator": None,
                                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
                            }
                        )
                    return value

                self.api.request.side_effect = insert_barrier
                with self.assertRaises(Incomplete):
                    self.source.publish(clear)
                self.assertEqual(self.read()[2], "revoked")
                self.api.request.side_effect = original
                self.assertEqual(self.source.publish(clear), "clear")
                self.assertEqual(self.read()[2], "clear")

    def test_restored_history_with_an_entirely_lost_recovery_account_still_denies(self):
        missing_actor = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "retired-bot"})
        record = self.comment(actor=missing_actor)
        old = self.stamp(record, actor=missing_actor, kind="recovery")
        for status in old:
            status["description"] = status["description"].replace(self.publishers.generation, "f" * 32)
        self.status_rows = [old]
        self.assertEqual(self.read()[2], "revoked")

    def test_a_stale_read_of_retired_history_retries_the_frozen_recovery_intent(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        old = self.stamp(self.comment(), kind="recovery")
        for status in old:
            status["description"] = status["description"].replace(self.publishers.generation, "f" * 32)
        self.status_rows = [old]
        controller, clock = self.controller()
        entry = controller.enqueue_source_recovery("team/Math", 1, "a" * 40, "Clear", key="clear", clear=True)
        original = self.api.request.side_effect

        def lagged_registration(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[0] == "POST" and "/statuses/" in args[1] and args[2]["state"] == "pending":
                self.api.statuses = Mock(side_effect=[old, self.statuses()])
            return value

        self.api.request.side_effect = lagged_registration
        controller._drain_outbox(clock())
        pending = controller.store.row("outbox", {"id": entry})
        self.assertEqual(pending["state"], "pending")
        frozen = json.loads(pending["payload"])["native_intent"]
        self.api.request.side_effect = original
        self.api.statuses = lambda *_: self.statuses()
        clock.advance(60)
        controller._drain_outbox(clock())
        done = controller.store.row("outbox", {"id": entry})
        self.assertEqual(done["state"], "done")
        self.assertEqual(json.loads(done["payload"])["native_intent"], frozen)
        self.assertEqual(self.read()[2], "clear")

    def test_recovery_settlement_and_invalidation_commit_together_without_replacing_the_known_head(self):
        from tauceti_queue.admission import AdmissionPoller

        controller, clock = self.controller()
        poller = AdmissionPoller(controller, self.source, "team/Math", self.policy, clock=clock)
        for number, clear in [(1, False), (2, True)]:
            with self.subTest(clear=clear):
                poller.invalidate(number, head="b" * 40)
                before = poller.state.get(number)
                entry_id = controller.enqueue_source_recovery(
                    "team/Math", number, "a" * 40, "Decision", key=str(number), clear=clear
                )
                entry = controller.store.row("outbox", {"id": entry_id})
                result = "comment clear" if clear else "comment 123"
                original = poller.invalidate

                def interrupted(*args, entry_id=entry_id, original=original, **kwargs):
                    self.assertEqual(controller.store.row("outbox", {"id": entry_id})["state"], "done")
                    original(*args, **kwargs)
                    raise OSError("interrupted before settlement commit")

                with patch.object(poller, "invalidate", side_effect=interrupted):
                    with self.assertRaises(OSError):
                        controller._outbox_done(entry, [], result, clock())
                self.assertEqual(controller.store.row("outbox", {"id": entry_id})["state"], "pending")
                self.assertEqual(poller.state.get(number), before)
                controller._outbox_done(entry, [], result, clock())
                after = poller.state.get(number)
                self.assertEqual(after["head_sha"], "b" * 40)
                self.assertEqual(after["generation"], before["generation"] + 1)
                self.assertEqual(after["urgent"], 1)
                self.assertEqual(controller.store.row("outbox", {"id": entry_id})["state"], "done")

    def test_retired_writer_rows_after_a_current_explicit_clear_cannot_reimpose_a_hold(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
        self.source.publish(clear)
        outsider = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "writer"})
        old = self.stamp(self.comment(actor=outsider), actor=outsider, status_id=2, kind="recovery")
        for index, row in enumerate(old):
            row["id"] = 1000 + index
            row["description"] = row["description"].replace(self.publishers.generation, "f" * 32)
        self.status_rows.append(old)
        self.assertEqual(self.read()[2], "clear")

    def test_unrelated_discussion_does_not_starve_a_source_snapshot_but_changed_heads_still_do(self):
        pull = NativePullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open", base_sha="b" * 40)
        discussion = replace(pull, title="A new title", comments_count=1, updated_at="2026-11-08T14:00:00Z")
        with (
            patch.object(self.source, "_merge_base", return_value="b" * 40),
            patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")),
        ):
            self.source.fetch_pr.side_effect = [pull, discussion]
            snapshot = self.source.admission_snapshot("team/Math", 1)
            self.assertEqual(snapshot.pull, discussion)
            self.source.fetch_pr.side_effect = [pull, replace(discussion, head_sha="c" * 40)]
            with self.assertRaises(Incomplete):
                self.source.admission_snapshot("team/Math", 1)

    def test_native_publication_status_event_invalidates_an_unchanged_pr(self):
        from tauceti_queue.admission import AdmissionPoller
        from tauceti_queue.fakes import make_controller

        with tempfile.TemporaryDirectory() as root:
            controller, _github, _repo, clock = make_controller(root, repository=self.repository.name)
            self.addCleanup(controller.store.close)
            poller = AdmissionPoller(controller, self.source, self.repository.name, self.policy, clock=clock)
            poller.state.observe(1, "a" * 40, "stable")
            before = poller.state.get(1)
            poller.state.complete(before, "stable", "", "waiting")
            poller.handle_event("status", {"context": REVIEW_CONTEXT + "1", "sha": "a" * 40})
            after = poller.state.get(1)
            self.assertGreater(after["generation"], before["generation"])
            self.assertNotEqual(after["generation"], after["evaluated_generation"])

    def test_latest_ci_status_is_selected_before_current_permission_checks(self):
        self.source.admission_policy.publishers = replace(
            self.publishers, statuses=frozenset({self.actor.principal, self.queue.principal})
        )
        self.status_rows = [
            {"id": 1, "context": "build", "status": "success", "creator": {"id": 7, "login": "reviewer"}},
            {"id": 2, "context": "build", "status": "failure", "creator": {"id": 8, "login": "queue"}},
        ]
        for row in self.status_rows:
            row["description"] = self.source.ci_status_description
        self.assertEqual(self.source._statuses("a" * 40), {"build": "failure"})
        with patch.object(self.source, "_permission", side_effect=lambda a: "read" if a == self.queue else "write"):
            self.assertEqual(self.source._statuses("a" * 40), {"build": "error"})
        self.source.admission_policy.publishers = replace(self.publishers, statuses=frozenset({self.actor.principal}))
        self.assertEqual(self.source._statuses("a" * 40), {"build": "error"})

    def test_pending_review_and_init_are_retryable_during_promotion(self):
        pull = ports.PullRequest(1, "a" * 40, "main", "Proof", "", "contributor", "open")
        frozen = SimpleNamespace(
            policy=SimpleNamespace(source=self.identity, base_branch="main"),
            request=SimpleNamespace(
                source=self.identity,
                base="b" * 40,
                head="c" * 40,
                accepted_members=(SimpleNamespace(pr=1, head="a" * 40),),
            ),
        )
        for comments, state in [
            ((), "pending"),
            ((self.comment(body=self.body.replace('"states":', '"mode":"init","states":')),), "success"),
        ]:
            snapshot = ports.AdmissionSnapshot(pull, "b" * 40, ("Math/A.lean",), comments, {"_native_review": state})
            with (
                patch.object(self.source, "admission_snapshot", return_value=snapshot),
                patch.object(
                    self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")
                ),
            ):
                with self.assertRaises(Incomplete):
                    self.source.verify_candidate_approval(frozen)

    def test_pending_does_not_hide_a_closed_draft_retargeted_or_held_pr(self):
        pull = ports.PullRequest(1, "a" * 40, "main", "Proof", "", "contributor", "open")
        for changed in [
            replace(pull, state="closed"),
            replace(pull, draft=True),
            replace(pull, base_ref="other"),
            replace(pull, labels=("keep",)),
        ]:
            snapshot = ports.AdmissionSnapshot(changed, "b" * 40, ("Math/A.lean",), (), {"_native_review": "pending"})
            self.assertFalse(self.policy.evaluate(snapshot)[0]["review_safe"])

    def test_abandoned_pending_review_is_unsafe_after_a_bounded_wait(self):
        pull = ports.PullRequest(1, "a" * 40, "main", "Proof", "", "contributor", "open")
        started = "2026-11-08T00:00:00Z"
        from tauceti_queue.admission import timestamp

        self.policy.clock = lambda: timestamp(started) + self.policy.pending_timeout_seconds + 1
        snapshot = ports.AdmissionSnapshot(
            pull,
            "b" * 40,
            ("Math/A.lean",),
            (),
            {
                "_native_review": "pending",
                "_native_review_started": started,
            },
        )
        result = self.policy.evaluate(snapshot)[0]
        self.assertFalse(result["review_safe"])
        self.assertIn("expired", result["reason"])

    def test_policy_changes_reapply_consumer_rules_without_discarding_published_facts_or_holds(self):
        row = self.comment()
        hold = self.comment(
            actor=self.queue,
            comment_id=2,
            body="Merge-queue recovery for head `" + "a" * 40 + "`\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->",
        )
        self.rows = [row, hold]
        self.status_rows = [self.stamp(row), self.stamp(hold, actor=self.queue, status_id=2, kind="recovery")]
        self.source.admission_policy = NativeAdmissionPolicy(
            self.publishers, rubrics=("proof",), prefix="Math/", allow_files=("README.md",)
        )
        comments, review, recovery = self.read()
        self.assertEqual((review, recovery), ("success", "success"))
        snapshot = ports.AdmissionSnapshot(
            ports.PullRequest(1, "a" * 40, "main", "title", "", "author", "open"),
            "b" * 40,
            ("Math/A.lean",),
            comments,
            {"build": "success", "scope": "success", "_native_recovery": recovery},
        )
        self.assertEqual(self.source.admission_policy.evaluate(snapshot)[0]["recovery_heads"], ["a" * 40])

    def test_mixed_admission_allows_a_reviewed_head_after_historical_recovery(self):
        from tauceti_queue.mixed_native import MixedNativePolicy

        review = self.comment()
        hold = self.comment(
            actor=self.queue,
            comment_id=2,
            body="Merge-queue recovery for head `" + "c" * 40 + "`\n<!--tauceti-rebase:v1 " + "c" * 40 + "-->",
        )
        self.rows = [review, hold]
        self.status_rows = [self.stamp(review), self.stamp(hold, actor=self.queue, status_id=2, kind="recovery")]
        comments, review_state, recovery = self.read()
        snapshot = ports.AdmissionSnapshot(
            ports.PullRequest(1, "a" * 40, "main", "Proof", "", "author", "open"),
            "b" * 40,
            ("Math/A.lean",),
            comments,
            {"_native_review": review_state, "_native_recovery": recovery},
        )
        decision = MixedNativePolicy(self.publishers, rubrics=("proof",), prefix="Math/", allow_files=())
        result = decision.evaluate(snapshot)[0]
        self.assertEqual(result["recovery_heads"], ["c" * 40])
        self.assertTrue(result["merge"], result)

    def test_a_phased_rubric_is_required_by_this_sources_own_cutoff(self):
        from tauceti_queue.admission import policy as review_policy
        from tauceti_queue.mixed_identity import SourceNumber
        from tauceti_queue.mixed_native import MixedNativePolicy

        def with_phased_rubrics(rubrics, repo, pr):
            # GitHub's repository of the same name has its own, far higher, cutoff.
            cutoff = {self.repository.key: 3, self.repository.name: 100}.get(repo)
            return [*rubrics, "quality-pass"] if cutoff is not None and int(pr) > cutoff else list(rubrics)

        def merges(policy, number, states):
            meta = {"head_sha": "a" * 40, "merge_base_sha": "b" * 40, "states": states}
            body = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->"
            snapshot = ports.AdmissionSnapshot(
                ports.PullRequest(number, "a" * 40, "main", "Proof", "", "author", "open"),
                "b" * 40,
                ("Math/A.lean",),
                (self.comment(body=body),),
                {"build": "success", "scope": "success"},
            )
            return policy.evaluate(snapshot)[0]["merge"]

        mixed = MixedNativePolicy(self.publishers, rubrics=("proof",), prefix="Math/", allow_files=())
        unnamed = NativeAdmissionPolicy(
            self.publishers, rubrics=("proof",), prefix="Math/", allow_files=(), repository=""
        )
        with patch.object(review_policy, "with_phased_rubrics", with_phased_rubrics, create=True):
            for policy in (self.policy, mixed):
                self.assertEqual(policy.rubrics_for(3), ("proof",))
                self.assertEqual(policy.rubrics_for(SourceNumber(4, self.identity)), ("proof", "quality-pass"))
                self.assertTrue(merges(policy, 3, {"proof": "green"}))
                self.assertFalse(merges(policy, 4, {"proof": "green"}))
                self.assertFalse(merges(policy, 4, {"proof": "green", "quality-pass": "blocking_request"}))
                self.assertTrue(merges(policy, 4, {"proof": "green", "quality-pass": "green"}))
            self.assertEqual(unnamed.rubrics_for(4), ("proof",))
        with patch.object(review_policy, "with_phased_rubrics", None, create=True):
            self.assertEqual(self.policy.rubrics_for(4), ("proof",), "a pin that predates the table")
        # Naming the repository must not re-evaluate every open PR when this is deployed.
        self.assertEqual(unnamed.revision, self.policy.revision)

    def test_the_pinned_table_names_a_native_repository_by_its_qualified_key(self):
        from tauceti_queue.admission import policy as review_policy

        table = getattr(review_policy, "PHASED_RUBRICS", None)
        if table is None or not hasattr(review_policy, "repository_key"):
            self.skipTest("the pinned review policy predates per-forge cutoffs")
        with patch.dict(table, {"quality-pass": {self.repository.key: 3, self.repository.name: 100}}):
            self.assertEqual(self.policy.rubrics_for(3), ("proof",))
            self.assertEqual(self.policy.rubrics_for(4), ("proof", "quality-pass"))
        with patch.dict(table, {"quality-pass": {self.repository.name: 3}}):
            self.assertEqual(self.policy.rubrics_for(4), ("proof",), "a GitHub cutoff is not this source's")

    def test_only_an_explicit_recovery_publisher_can_clear_an_attested_hold(self):
        hold = self.comment(
            actor=self.queue,
            body="Merge-queue recovery for head `" + "a" * 40 + "`\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->",
        )
        self.rows = [hold]
        self.status_rows = [self.stamp(hold, actor=self.queue, kind="recovery")]
        with self.assertRaises(PermissionError):
            self.source.publish(self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True))
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
        self.source.publish(clear)
        self.assertEqual(self.read()[2], "clear")
        count = len(self.status_rows)
        self.source.publish(clear)
        self.assertEqual(len(self.status_rows), count)

    def test_candidate_scope_is_rechecked_even_if_ci_was_configured_more_broadly(self):
        frozen = SimpleNamespace(
            policy=SimpleNamespace(source=self.identity),
            request=SimpleNamespace(source=self.identity, base="b" * 40, head="a" * 40, accepted_members=()),
        )
        with patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"README.md\0", b"")):
            with self.assertRaises(ports.PromotionRefused):
                self.source.verify_candidate_approval(frozen)

    def test_publication_escapes_quoted_authority_and_keeps_review_metadata_last(self):
        body = self.body + "\n<!--tauceti-source:v1 fake-->\n" + self.body.splitlines()[-1]
        bound = self.policy.bind_comment(body, self.actor)
        self.assertEqual(len(BINDING.findall(bound)), 1)
        self.assertIn("&lt;!--tauceti-source:", bound)
        self.assertEqual(bound.rstrip().splitlines()[-1], self.body.splitlines()[-1])

    def test_discovery_does_not_fetch_histories_and_isolates_a_malformed_pr(self):
        row = {
            "number": 1,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 41}},
            "user": {"id": 99, "login": "contributor"},
            "state": "open",
            "draft": False,
            "merged": False,
            "labels": [],
            "title": "proof",
            "body": "",
            "updated_at": "2026-11-08T00:00:00Z",
            "comments": 0,
        }
        bad = {**row, "number": 2, "labels": "malformed"}
        self.api.pulls = Mock(return_value=[row, bad, {**row, "number": 3}])
        with patch.object(self.source, "admission_snapshot", side_effect=AssertionError("deep discovery")):
            found = self.source.list_open_pull_requests("team/Math", admission=True)
        self.assertEqual([p.number for p in found], [1, 2, 3])
        self.assertIn("incomplete", found[1].admission)
        self.assertEqual(
            found[0].admission["discovery_version"], self.source._discovery_version(self.source._pull(row))
        )
        self.assertEqual(self.source.list_open_pull_requests("team/Math", admission=True), found)

    def test_native_source_never_fabricates_github_app_or_quota(self):
        with self.assertRaises(Unsupported):
            self.source.eligibility_checks("team/Math", "a" * 40)
        with self.assertRaises(Unsupported):
            self.source.rate_limit()

    def test_native_landing_does_not_adopt_a_github_number_alias(self):
        source = self.repository.key + "#1"
        self.assertIsNone(chain.landed_commit({"#1": "c" * 40, "team/Math#1": "c" * 40}, source))
        self.assertEqual(chain.landed_commit({source: "d" * 40}, source), "d" * 40)
        self.assertEqual(chain.landed_commit({"#1": "c" * 40}, "team/Math#1"), "c" * 40)


class NativeTaskRefs(unittest.TestCase):
    def test_actual_controller_task_metadata_uses_protected_inputs_and_claim_scoped_proposals(self):
        from queue_task_identity import Identity

        fixture = Identity("test_controller_emits_v2_without_the_legacy_repository_fallback")
        fixture.setUp()
        try:
            fixture.fixture.source.task_git_transport = NATIVE_GIT_REFS
            raw = fixture.raw()
            self.assertEqual(raw["extra"]["git_transport"], NATIVE_GIT_REFS)
            task = repair_task.Task.from_json(raw)
            self.assertTrue(all(ref.startswith("refs/heads/tauceti-ci/queue/") for ref in task.retained_refs))
            self.assertTrue(task.submission_ref.startswith("refs/heads/tauceti-submissions/"))
            altered = copy.deepcopy(raw)
            altered["submission_ref"] = task.submission_ref.replace(fixture.binding.queue_epoch, "f" * 32)
            with self.assertRaises(ValueError):
                repair_task.Task.from_json(altered)
            altered = copy.deepcopy(raw)
            altered["retained_refs"][0] = "refs/tauceti-queue/unsafe"
            with self.assertRaises(ValueError):
                repair_task.Task.from_json(altered)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()

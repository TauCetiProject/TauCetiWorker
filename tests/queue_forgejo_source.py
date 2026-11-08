#!/usr/bin/env python3
"""Native source receipts, publication recovery and qualified task metadata."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
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
    PublicationSuperseded,
)
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
        )
        self.policy = NativeAdmissionPolicy(self.publishers, rubrics=("proof",), prefix="Math/", allow_files=())
        self.rows = []
        self.status_rows = []
        self.permission = "write"
        self.calls = []

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
            statuses=lambda *_: copy.deepcopy(self.status_rows),
            current_user=Mock(return_value={"id": 7, "login": "reviewer"}),
            post_comment=Mock(side_effect=post),
            edit_comment=Mock(side_effect=edit),
        )
        self.source = ForgejoSourceAdapter(
            self.identity, self.api, self.clone, token="token", admission_policy=self.policy
        )
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

    def stamp(self, row, *, actor=None, status_id=1, kind="review"):
        actor = actor or self.actor
        return {
            "id": status_id,
            "context": (REVIEW_CONTEXT if kind == "review" else RECOVERY_CONTEXT) + "1",
            "status": "success",
            "creator": {"id": int(actor.principal.native_id), "login": actor.login},
            "description": PUB_DESCRIPTION + str(row["id"]) + ":" + hashlib.sha256(row["body"].encode()).hexdigest(),
        }

    def read(self):
        return self.source._comments(1, "a" * 40, self.status_rows)

    def test_native_policy_requires_qualified_source_local_publishers(self):
        with self.assertRaises(ValueError):
            NativePublishers(self.identity, frozenset(), self.publishers.statuses)
        with self.assertRaises(ValueError):
            NativePublishers(
                self.identity,
                frozenset({PrincipalRef(Forge("forgejo", "https://other.example"), "7")}),
                self.publishers.statuses,
            )
        other = replace(self.publishers, source=replace(self.identity, native_id=42))
        changed = NativeAdmissionPolicy(other, rubrics=("proof",), prefix="Math/", allow_files=())
        with self.assertRaises(ValueError):
            ForgejoSourceAdapter(self.identity, self.api, self.clone, token="token", admission_policy=changed)
        self.assertNotEqual(self.policy.revision, changed.revision)

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
                "id": 2,
                "context": REVIEW_CONTEXT + "1",
                "status": "pending",
                "creator": {"id": 7, "login": "reviewer"},
                "description": PUB_DESCRIPTION + "pending:" + "c" * 64,
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
                "context": "build",
                "status": state,
                "creator": {"id": int(actor.principal.native_id), "login": actor.login},
            }

        self.status_rows = [
            status(4, self.actor, "success"),
            status(3, self.queue, "failure"),
            status(2, self.queue, "success"),
        ]
        self.assertEqual(self.source._statuses("a" * 40), {"build": "failure"})

    def test_publication_retry_observes_pending_without_resending_the_intent(self):
        intent = self.source.prepare_publication("team/Math", 1, "a" * 40, self.body, "review-marker")
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
        first = self.source.prepare_publication("team/Math", 1, "a" * 40, self.body, "first")
        self.source.publish(first)
        second = self.source.prepare_publication("team/Math", 1, "a" * 40, self.body + "\nNew verdict.", "second")
        self.source.publish(second)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(len(self.status_rows), 4)

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

    def test_only_an_explicit_recovery_publisher_can_clear_an_attested_hold(self):
        hold = self.comment(
            actor=self.queue,
            body="Merge-queue recovery for head `" + "a" * 40 + "`\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->",
        )
        self.rows = [hold]
        self.status_rows = [self.stamp(hold, actor=self.queue, kind="recovery")]
        with self.assertRaises(PermissionError):
            self.source.clear_recovery("team/Math", 1, "a" * 40)
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        self.source.clear_recovery("team/Math", 1, "a" * 40)
        self.assertEqual(self.read()[2], "clear")
        count = len(self.status_rows)
        self.source.clear_recovery("team/Math", 1, "a" * 40)
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
        self.assertTrue(all("incomplete" in p.admission for p in found))

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

#!/usr/bin/env python3
"""Native source admission at parity with GitHub, recovery holds and qualified task metadata."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forge, ForgeError, Incomplete, PrincipalRef, RepositoryRef, Unsupported

from tauceti_queue import chain, ports, repair_task
from tauceti_queue.forgejo_source import (
    ForgejoSourceAdapter,
    NativeAdmissionPolicy,
    NativePublishers,
    NativePullRequest,
)
from tauceti_queue.store import Store
from tauceti_queue.task_identity import NATIVE_GIT_REFS
from tauceti_worker.source_identity import SourceActor

# The provenance block every board published before scoreboard parity still carries.
LEGACY_PROVENANCE = (
    '<!--tauceti-source:v2 {"generation":"6066b56b39db11de4ee57812e7ec983b",'
    '"issuer":"forgejo:https://source.example/users/7","policy_revision":"' + "9" * 40 + '",'
    '"schema":"tauceti-source-publication/v2","source":{"forge":{"kind":"forgejo",'
    '"url":"https://source.example"},"id":41,"name":"team/Math"}}-->'
)


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
        self.publishers = NativePublishers(self.identity, frozenset({self.queue.principal}))
        self.policy = NativeAdmissionPolicy(self.publishers, rubrics=("proof",), prefix="Math/", allow_files=())
        self.rows = []
        self.status_rows = []
        self.permission = "write"

        def request(method, path, data=None, **kwargs):
            if path.endswith("/permission"):
                login = path.rsplit("/", 2)[-2]
                if login not in {"reviewer", "queue"}:
                    raise ForgeError("not a collaborator", status=404)
                actor = self.queue if login == "queue" else self.actor
                return {
                    "user": {"id": int(actor.principal.native_id), "login": actor.login},
                    "permission": self.permission,
                }
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
            current_user=Mock(return_value={"id": 8, "login": "queue"}),
            post_comment=Mock(side_effect=post),
            edit_comment=Mock(side_effect=edit),
        )
        self.source = ForgejoSourceAdapter(
            self.identity, self.api, self.clone, token="token", admission_policy=self.policy, clock=lambda: 1000
        )
        self.pull = NativePullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open", base_sha="b" * 40)
        self.source.fetch_pr = Mock(return_value=self.pull)
        self.body = self.board({"proof": "green"})

    @staticmethod
    def board(states, **meta):
        meta = {"head_sha": "a" * 40, "merge_base_sha": "b" * 40, "states": states, **meta}
        return "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->"

    def comment(self, *, user=(7, "reviewer"), body=None, comment_id=1, updated_at="2026-11-08T00:00:00Z"):
        return {
            "id": comment_id,
            "body": self.body if body is None else body,
            "user": {"id": user[0], "login": user[1]},
            "created_at": "2026-11-08T00:00:00Z",
            "updated_at": updated_at,
            "html_url": f"https://source.example/team/Math/pulls/1#issuecomment-{comment_id}",
        }

    def snapshot(self):
        with (
            patch.object(self.source, "_merge_base", return_value="b" * 40),
            patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")),
            patch("tauceti_queue.diff_guard.violations", return_value=()),
        ):
            return self.source.admission_snapshot("team/Math", 1)

    def decide(self):
        return self.source.admission_policy.evaluate(self.snapshot())[0]

    def ci(self, state="success", creator=(8, "queue")):
        self.status_rows = [
            {"id": i, "context": context, "status": state, "creator": {"id": creator[0], "login": creator[1]}}
            for i, context in enumerate(("build", "scope", "bump-guard"), 1)
        ]

    def test_native_policy_requires_qualified_source_local_publishers(self):
        with self.assertRaises(ValueError):
            NativePublishers(self.identity, frozenset({PrincipalRef(Forge("forgejo", "https://other.example"), "7")}))
        with self.assertRaises(ValueError):
            NativePublishers(self.identity, frozenset({PrincipalRef(self.repository.forge, "0")}))
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

    def test_any_authors_newest_completed_scoreboard_decides_as_on_github(self):
        self.ci()
        self.assertFalse(self.decide()["review_safe"], "no scoreboard")
        self.rows = [self.comment(user=(99, "contributor"))]
        result = self.decide()
        self.assertTrue(result["review_safe"] and result["merge"], result)
        red = self.board({"proof": "blocking_request"})
        self.rows.append(self.comment(user=(98, "other"), body=red, comment_id=2, updated_at="2026-11-08T01:00:00Z"))
        self.assertFalse(self.decide()["review_safe"])
        init = self.board({}, mode="init")
        self.rows = [self.comment(), self.comment(body=init, comment_id=3, updated_at="2026-11-08T02:00:00Z")]
        self.assertTrue(self.decide()["merge"], "an in-progress board never replaces a completed verdict")
        self.rows = [self.comment(body=self.board({"proof": "green"}, merge_base_sha="c" * 40))]
        self.assertFalse(self.decide()["review_safe"], "a board for another merge base")
        self.rows = [self.comment(body=self.board({"proof": "green"}, head_sha="c" * 40))]
        self.assertFalse(self.decide()["review_safe"], "a board for another head")

    def test_existing_boards_with_source_provenance_still_count(self):
        self.ci()
        marker = "<!-- tauceti-queue review-engine:" + "2" * 32 + " -->"
        meta = self.body.splitlines()[-1]
        legacy = "\n".join([self.body.splitlines()[0], "| proof | green |", marker, LEGACY_PROVENANCE, meta])
        self.rows = [self.comment(body=legacy)]
        snapshot = self.snapshot()
        self.assertEqual([row["id"] for row in snapshot.comments], [1])
        result = self.source.admission_policy.evaluate(snapshot)[0]
        self.assertTrue(result["merge"], result)

    def test_snapshots_keep_only_scoreboards_and_recovery_requests(self):
        hold = "Merge-queue recovery for head `" + "c" * 40 + "`\n\n<!--tauceti-rebase:v1 " + "c" * 40 + "-->"
        self.rows = [
            self.comment(body="Routine conversation", comment_id=1),
            self.comment(comment_id=2),
            self.comment(user=(8, "queue"), body=hold, comment_id=3),
        ]
        self.assertEqual([row["id"] for row in self.snapshot().comments], [2, 3])
        self.rows = [{"id": 1}, "malformed"]
        with self.assertRaises(Incomplete):
            self.snapshot()

    def test_only_the_queue_account_can_hold_a_head_for_recovery(self):
        self.ci()
        hold = "Merge-queue recovery for head `" + "a" * 40 + "`\n\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->"
        stalled = hold.replace("tauceti-rebase:v1", "tauceti-merge-stalled:v1")
        self.rows = [self.comment(), self.comment(body=hold, comment_id=2), self.comment(body=stalled, comment_id=3)]
        self.rows.append(self.comment(user=(0, "ghost"), body=hold, comment_id=4))
        result = self.decide()
        self.assertEqual(result["recovery_heads"], [])
        self.assertTrue(result["merge"], result)
        for body in (hold, stalled):
            self.rows = [self.comment(), self.comment(user=(8, "queue"), body=body, comment_id=2)]
            result = self.decide()
            self.assertEqual(result["recovery_heads"], ["a" * 40])
            self.assertTrue(result["review_safe"])
            self.assertFalse(result["merge"])

    def test_mixed_admission_allows_a_reviewed_head_after_historical_recovery(self):
        from tauceti_queue.mixed_native import MixedNativePolicy

        hold = "Merge-queue recovery for head `" + "c" * 40 + "`\n<!--tauceti-rebase:v1 " + "c" * 40 + "-->"
        self.rows = [self.comment(), self.comment(user=(8, "queue"), body=hold, comment_id=2)]
        self.source.admission_policy = MixedNativePolicy(
            self.publishers, rubrics=("proof",), prefix="Math/", allow_files=()
        )
        result = self.decide()
        self.assertEqual(result["recovery_heads"], ["c" * 40])
        self.assertTrue(result["merge"], result)

    def test_permission_response_cannot_borrow_another_native_user_id(self):
        self.api.request.side_effect = None
        self.api.request.return_value = {"permission": "admin", "user": {"id": 99, "login": "reviewer"}}
        with self.assertRaises(Incomplete):
            self.source._permission(self.actor)
        with self.assertRaises(ValueError):
            self.source.repository_admin("reviewer")

    def test_ci_statuses_count_only_while_their_creator_can_write(self):
        self.status_rows = [
            {"id": 1, "context": "build", "status": "success", "creator": {"id": 7, "login": "reviewer"}},
            {"id": 2, "context": "build", "status": "failure", "creator": {"id": 8, "login": "queue"}},
            {"id": 3, "context": "scope", "status": "success", "creator": {"id": 7, "login": "reviewer"}},
            {"id": 4, "context": "unrelated", "status": "success", "creator": {"id": 99, "login": "outsider"}},
        ]
        self.assertEqual(self.source._statuses("a" * 40), {"build": "failure", "scope": "success"})
        with patch.object(self.source, "_permission", side_effect=lambda a: "read" if a == self.queue else "write"):
            self.assertEqual(self.source._statuses("a" * 40), {"build": "error", "scope": "success"})
        # Forgejo Actions posts statuses as its system user for any workflow a PR brings.
        self.status_rows = [
            {"id": 5, "context": ctx, "status": "success", "creator": {"id": -2, "login": "forgejo-actions"}}
            for ctx in ("build", "self-tested/x / y (pull_request)")
        ]
        self.status_rows.append(
            {"id": 6, "context": "bump-guard", "status": "success", "creator": {"id": 99, "login": "outsider"}}
        )
        self.assertEqual(
            self.source._statuses("a" * 40),
            {"build": "error", "self-tested/x / y (pull_request)": "error", "bump-guard": "error"},
        )
        self.status_rows = [
            {"id": 1, "context": "build", "status": "success", "creator": {"id": 7, "login": "reviewer"}}
        ]
        self.api.request.side_effect = ForgeError("outage", status=503)
        with self.assertRaises(ForgeError):
            self.source._statuses("a" * 40)

    def test_queue_comments_never_carry_review_or_recovery_markers(self):
        for body in ["Agent quoted <!--tauceti-scoreboard-->", "Agent quoted:\n> " + self.body]:
            self.source.post_comment("team/Math", 1, body, "quoted")
        self.assertEqual(len(self.rows), 1)
        self.assertNotIn("<!--tauceti-scoreboard-->", self.rows[0]["body"])
        self.assertTrue(self.rows[0]["body"].endswith("\n\n<!-- tauceti-queue quoted -->\n"), self.rows[0]["body"])
        self.rows.insert(0, self.comment(user=(7, "reviewer"), body="<!-- tauceti-queue quoted -->", comment_id=5))
        self.source.post_comment("team/Math", 1, "Fresh", "quoted")
        self.assertEqual(self.rows[0]["body"], "<!-- tauceti-queue quoted -->", "another author's comment")
        self.assertIn("Fresh", self.rows[1]["body"])

    def test_every_native_head_is_held_to_the_pr_level_diff_guards(self):
        pull = NativePullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open", base_sha="b" * 40)
        self.source.fetch_pr.side_effect = [pull, pull]
        with (
            patch.object(self.source, "_merge_base", return_value="b" * 40),
            patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")),
            patch("tauceti_queue.diff_guard.violations", return_value=("too large to review",)) as measure,
        ):
            snapshot = self.source.admission_snapshot("team/Math", 1)
        measure.assert_called_once_with(self.source.clone_dir, "b" * 40, "a" * 40)
        self.assertEqual(snapshot.diff_guard, ("too large to review",))
        result = self.source.admission_policy.evaluate(snapshot)[0]
        self.assertFalse(result["merge"])
        self.assertIn("too large to review", result["reason"])

    def test_unrelated_discussion_does_not_starve_a_source_snapshot_but_changed_heads_still_do(self):
        pull = NativePullRequest(1, "a" * 40, "main", "Proof", "", "writer", "open", base_sha="b" * 40)
        discussion = replace(pull, title="A new title", comments_count=1, updated_at="2026-11-08T14:00:00Z")
        with (
            patch.object(self.source, "_merge_base", return_value="b" * 40),
            patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"Math/A.lean\0", b"")),
            patch("tauceti_queue.diff_guard.violations", return_value=()),
        ):
            self.source.fetch_pr.side_effect = [pull, discussion]
            snapshot = self.source.admission_snapshot("team/Math", 1)
            self.assertEqual(snapshot.pull, discussion)
            self.source.fetch_pr.side_effect = [pull, replace(discussion, head_sha="c" * 40)]
            with self.assertRaises(Incomplete):
                self.source.admission_snapshot("team/Math", 1)

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

    def test_candidate_scope_is_rechecked_even_if_ci_was_configured_more_broadly(self):
        frozen = SimpleNamespace(
            policy=SimpleNamespace(source=self.identity),
            request=SimpleNamespace(source=self.identity, base="b" * 40, head="a" * 40, accepted_members=()),
        )
        with patch.object(self.source, "_git", return_value=subprocess.CompletedProcess([], 0, b"README.md\0", b"")):
            with self.assertRaises(ports.PromotionRefused):
                self.source.verify_candidate_approval(frozen)

    def test_recent_listing_reads_newest_first_until_an_older_update(self):
        def row(number, updated):
            return {
                "number": number,
                "head": {"sha": "a" * 40},
                "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 41}},
                "user": {"id": 99, "login": "contributor"},
                "state": "open",
                "draft": False,
                "merged": False,
                "labels": [],
                "title": "proof",
                "body": "",
                "updated_at": updated,
                "comments": 0,
            }

        newest = [row(9, "2026-11-19T04:10:00Z"), row(5, "2026-11-19T04:05:00Z")]
        older = [row(3, "2026-11-19T03:00:00Z"), row(2, "2026-11-19T02:00:00Z")]
        pages = {1: newest, 2: older}
        paths = []

        def request(method, path, data=None, **kwargs):
            paths.append(path)
            self.assertIn("sort=recentupdate", path)
            return copy.deepcopy(pages.get(int(path.rsplit("page=", 1)[1]), []))

        self.api.request = Mock(side_effect=request)
        since = datetime.fromisoformat("2026-11-19T04:00:00+00:00").timestamp()
        items, latest, complete = self.source.list_recent_pull_requests("team/Math", since=since, page_size=2)
        self.assertEqual([i.number for i in items], [9, 5])
        self.assertTrue(complete)
        self.assertEqual(latest, datetime.fromisoformat("2026-11-19T04:10:00+00:00").timestamp())
        self.assertEqual(
            items[0].admission["discovery_version"], self.source._discovery_version(self.source._pull(newest[0]))
        )
        self.assertEqual(len(paths), 2)  # the second page held the first older update
        # A server that caps pages below the requested size: a short page does not end the read.
        pages = {1: newest[:1], 2: newest[1:], 3: []}
        paths.clear()
        items, _, complete = self.source.list_recent_pull_requests("team/Math", since=since, page_size=2)
        self.assertEqual(([i.number for i in items], complete, len(paths)), ([9, 5], True, 3))
        # A bounded read that never reaches `since` says so, so the caller lists in full.
        items, _, complete = self.source.list_recent_pull_requests("team/Math", since=0, page_size=2, max_pages=1)
        self.assertFalse(complete)

    def test_comment_feed_reads_edits_by_update_time_and_names_their_prs(self):
        pages = {
            1: [
                {
                    "id": 5,
                    "body": "x",
                    "updated_at": "2026-11-19T04:34:45Z",
                    "pull_request_url": "https://source.example/team/Math/pulls/1133",
                },
                {
                    "id": 6,
                    "body": "y",
                    "updated_at": "2026-11-19T04:20:00Z",
                    "pull_request_url": "",
                    "issue_url": "https://source.example/team/Math/issues/7",
                },
            ],
            2: [
                {
                    "id": 7,
                    "body": "z",
                    "updated_at": "2026-11-19T04:40:00Z",
                    "pull_request_url": "https://source.example/team/Math/pulls/12",
                }
            ],
            3: [],
        }
        paths = []

        def request(method, path, data=None, **kwargs):
            paths.append(path)
            return copy.deepcopy(pages[int(path.rsplit("page=", 1)[1])])

        self.api.request = Mock(side_effect=request)
        since = datetime.fromisoformat("2026-11-19T04:00:00+00:00").timestamp()
        rows, newest, complete = self.source.list_recent_comments("team/Math", since=since)
        self.assertEqual([(n, r["id"]) for n, r in rows], [(1133, 5), (12, 7)])  # the issue comment is not a PR's
        self.assertTrue(complete)
        self.assertEqual(newest, datetime.fromisoformat("2026-11-19T04:40:00+00:00").timestamp())
        self.assertIn("since=2026-11-19T04%3A00%3A00Z", paths[0])
        _, _, complete = self.source.list_recent_comments("team/Math", since=since, max_pages=2)
        self.assertFalse(complete)

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
        self.assertEqual(found[0].admission["recheck_seconds"], 900)
        # A later listing of the same pull is the same observation: time alone must not re-flag it.
        self.source.configure_admission_polling(clock=lambda: 1000 + 86400, refresh_seconds=900)
        self.assertEqual(self.source.list_open_pull_requests("team/Math", admission=True), found)
        landed = {**row, "base": {**row["base"], "sha": "c" * 40}}
        self.assertEqual(
            self.source._discovery_version(self.source._pull(landed)), found[0].admission["discovery_version"]
        )
        changed = {**row, "updated_at": "2026-11-09T00:00:00Z", "comments": 1}
        self.assertNotEqual(
            self.source._discovery_version(self.source._pull(changed)), found[0].admission["discovery_version"]
        )

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

#!/usr/bin/env python3
"""The actual source survey uses native IDs and never native build authority."""

import dataclasses
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import Forge, ForgeError, RepositoryRef

from tauceti_worker.forgejo_reads import BoundedReadCache, ForgejoReadSource
from tauceti_worker.source_identity import SourceActor
from tauceti_worker.source_survey import source_pr_info
from tauceti_worker.survey import PRInfo, Survey, owns_author, owns_pr

survey_module = importlib.import_module("tauceti_worker.survey")


class SourceSurveys(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cfg = SimpleNamespace(wid="test", state=Path(self.temporary.name))
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.api = Mock(forge=self.forge, _token="secret", max_bytes=1_000_000)
        self.api.request.return_value = {"id": 2, "login": "renamed"}
        self.api.pulls.return_value = [self.row(1), self.row(2, actor=3, login="renamed"), self.row(3, actor=-1)]
        self.source = ForgejoReadSource(self.repo, self.api)

    def row(self, number, *, actor=2, login="previous-name"):
        return {
            "number": number,
            "id": number + 100,
            "title": "Proof",
            "body": "",
            "flow": 0,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable": False,
            "html_url": self.repo.pull_url(number),
            "user": {"id": actor, "login": login},
            "head": {
                "sha": "a" * 40,
                "label": "roadmap/work",
                "repo": {
                    "id": 7,
                    "name": "project",
                    "full_name": self.repo.name,
                    "html_url": self.repo.url,
                    "owner": {"id": 10, "login": "one"},
                },
            },
            "labels": [{"id": 1, "name": "roadmap/PDE"}, {"id": 2, "name": "awaiting-author"}],
            "updated_at": "2026-11-07T00:00:00Z",
        }

    def run_survey(self, *, deep=False, stages=(), numbers=None, count_all_roadmaps=False):
        rs = Mock()
        with (
            patch.object(survey_module, "me", side_effect=AssertionError("GitHub identity")),
            patch.object(survey_module, "can_push", side_effect=AssertionError("GitHub ACL")),
            patch("tauceti_worker.github_state.read_survey", side_effect=AssertionError("shared service")),
        ):
            result = survey_module.survey(
                self.cfg,
                self.source,
                rs,
                Mock(),
                deep=deep,
                stages=stages,
                selected_roadmap="PDE",
                numbers=numbers,
                count_all_roadmaps=count_all_roadmaps,
            )
        rs.seed_survey.assert_not_called()
        rs.observe.assert_not_called()
        return result

    def test_actual_consumer_uses_qualified_actor_ids_after_rename_and_login_reuse(self):
        result = self.run_survey()
        self.assertFalse(result.github_failed)
        self.assertEqual(result.n_mine_open, 1)
        self.assertEqual(result.me_login, "renamed")
        self.assertEqual([p.number for p in result._mine_open_prs], [1])
        self.assertTrue(result.owns(result.open_prs[0]))
        self.assertFalse(result.owns(result.open_prs[1]))
        self.assertFalse(result.owns(result.open_prs[2]))
        self.assertEqual(result.n_open_nondraft, 3)
        awaiting = next(bucket for bucket in result.status_labels if bucket[0] == "awaiting-author")
        self.assertEqual(awaiting, ("awaiting-author", 3, 1))
        self.assertIsNone(result.next_auto_stage)
        result.rescope_roadmap()
        self.assertIsNone(result.next_auto_stage)

    def test_legacy_recovery_casefold_does_not_apply_to_qualified_native_ids(self):
        legacy = Survey(worker_id="legacy", me_login="AUTHOR")
        row = PRInfo.from_json({"number": 1, "author": {"login": "author"}})
        self.assertFalse(owns_pr(legacy, row))
        self.assertTrue(owns_pr(legacy, row, casefold_legacy=True))
        result = self.run_survey()
        self.assertFalse(owns_pr(result, result.open_prs[1], casefold_legacy=True))
        self.assertIsNone(survey_module._next_auto_stage(result))

    def test_fresh_author_checks_use_native_ids_and_no_legacy_repair_fields_grant_ownership(self):
        result = self.run_survey()
        qualified = {"id": self.forge.key + "/users/2", "login": "previous-name"}
        self.assertTrue(owns_author(result, qualified))
        self.assertFalse(owns_author(result, {**qualified, "id": self.forge.key + "/users/3", "login": "renamed"}))
        self.assertFalse(owns_author(result, {"login": "renamed"}))
        self.assertFalse(owns_author(result, {**qualified, "id": "forgejo:https://other.example.org/users/2"}))
        legacy = SimpleNamespace(me_login="AUTHOR")
        self.assertTrue(owns_author(legacy, {"login": "author"}))
        self.assertFalse(owns_pr(legacy, SimpleNamespace(), casefold_legacy=True))

    def test_held_source_automatic_bumps_follow_existing_nondraft_count_policy(self):
        from tauceti_worker.constants import BUMP_HEAD_PREFIX

        row = self.row(4, actor=-2)
        row["head"]["label"] = BUMP_HEAD_PREFIX + "example"
        row["labels"].append({"id": 3, "name": "hold"})
        self.api.pulls.return_value.append(row)
        result = self.run_survey()
        self.assertEqual(len(result.open_prs), 4)
        self.assertTrue(result.open_prs[-1].automatic_bump)
        self.assertEqual(result.n_open_nondraft, 3)

    def test_public_source_inventory_serializes_its_origin_and_principal(self):
        result = self.run_survey()
        payload = json.loads(json.dumps(dataclasses.asdict(result)))
        self.assertEqual(payload["source_repository"]["forge"]["url"], self.forge.url)
        self.assertEqual(payload["open_prs"][0]["source_author"]["principal"]["native_id"], "2")
        self.assertNotIn("source_repository", dataclasses.asdict(Survey(worker_id="legacy")))

    def test_source_native_statuses_are_not_trusted_ci_even_when_caller_supplies_them(self):
        rows = self.source.open_prs()
        rows[0]["statusCheckRollup"] = [{"context": "build", "state": "SUCCESS"}]
        value = source_pr_info(rows[0], self.repo)
        self.assertFalse(value.build_success)
        self.assertFalse(value.build_failed)
        self.assertIsNone(value.build_status_at)
        self.assertEqual(value.mergeable, "UNKNOWN")
        result = self.run_survey()
        self.assertEqual(result.n_reviewable, 0)
        for kind in [result.reviewable, result.red_ci, result.needs_fix]:
            self.assertFalse(kind.actionable)
        self.api.statuses.assert_not_called()
        self.api.pull.assert_not_called()
        self.api.comments.assert_not_called()
        self.api.files.assert_not_called()

    def test_native_inventory_fetch_coalesces_by_credential_and_is_invalidated_by_pr_write(self):
        self.run_survey()
        self.run_survey()
        self.assertEqual(self.api.pulls.call_count, 1)
        self.assertEqual(self.api.request.call_count, 2, "actor must be fresh on each survey")
        self.source.invalidate(1)
        self.run_survey()
        self.assertEqual(self.api.pulls.call_count, 2)

    def test_verified_empty_inventory_is_successful_but_grants_no_authoring(self):
        self.api.pulls.return_value = []
        result = self.run_survey()
        self.assertFalse(result.github_failed)
        self.assertFalse(result.open_prs)
        self.assertEqual(result.n_mine_open, 0)
        self.assertIsNotNone(result.source_actor)
        self.assertIsNone(result.next_auto_stage)
        self.assertEqual(result.dependency_wait["dependency"], "source-scheduling")

    def test_unknown_actor_or_inventory_failure_is_not_an_empty_success(self):
        self.api.request.side_effect = ForgeError("secret diagnostic", status=403)
        failed = self.run_survey()
        self.assertTrue(failed.github_failed)
        self.assertEqual(failed.dependency_wait["status"], 403)
        self.assertIsNone(failed.next_auto_stage)
        self.assertNotIn("secret", str(failed.errors))
        self.api.pulls.assert_not_called()
        self.api.request.side_effect = None
        self.api.pulls.side_effect = ForgeError("private body", status=500)
        failed = self.run_survey()
        self.assertTrue(failed.github_failed)
        self.assertEqual(failed.dependency_wait["operation"], "open_prs")
        self.assertNotIn("private", str(failed.dependency_wait))

    def test_deep_scheduling_is_explicitly_unavailable_until_required_adapters_exist(self):
        result = self.run_survey(deep=True, stages=["fix", "progress", "review"])
        self.assertEqual(len(result.open_prs), 3)
        self.assertTrue(result.github_failed)
        self.assertEqual(result.dependency_wait["dependency"], "source-scheduling")
        self.assertIsNone(result.next_auto_stage)
        self.assertFalse(result.needs_fix.actionable)

    def test_scope_and_all_roadmap_backpressure_use_qualified_authors(self):
        result = self.run_survey(deep=True, numbers=[2], count_all_roadmaps=True)
        self.assertEqual([p.number for p in result.open_prs], [2])
        self.assertFalse(result._mine_open_prs)
        self.assertEqual(result.n_mine_open, 1)

    def test_other_origin_same_principal_and_login_cannot_own_a_source_pr(self):
        result = self.run_survey()
        other = Forge("forgejo", "https://other.example.org")
        other_actor = SourceActor.from_native(other, {"id": 2, "login": "renamed"})
        self.assertFalse(dataclasses.replace(result, source_actor=other_actor).owns(result.open_prs[0]))
        foreign = dataclasses.replace(result.open_prs[0], source_repository=RepositoryRef(other, self.repo.name))
        self.assertFalse(result.owns(foreign))
        legacy = Survey(worker_id="legacy", me_login=result.open_prs[0].author)
        self.assertFalse(owns_pr(legacy, result.open_prs[0]))
        self.assertFalse(owns_pr(SimpleNamespace(me_login=legacy.me_login), result.open_prs[0]))

    def test_any_malformed_partial_or_foreign_inventory_is_unavailable(self):
        for change in [
            {"html_url": "https://other.example.org/one/project/pulls/1"},
            {"state": "closed"},
            {"flow": 1},
            {"head": None},
            {
                "head": {
                    "sha": "a" * 40,
                    "label": "work",
                    "repo": {
                        "id": 7,
                        "full_name": self.repo.name,
                        "html_url": "https://other.example.org/one/project",
                        "name": "project",
                    },
                }
            },
        ]:
            with self.subTest(change=change):
                self.source.invalidate()
                self.api.pulls.return_value = [self.row(2), {**self.row(1), **change}]
                result = self.run_survey()
                self.assertTrue(result.github_failed)
                self.assertEqual(result.open_prs, [])

    def test_malformed_head_url_is_unavailable_without_partial_inventory(self):
        for url in [17, None, [], {}]:
            with self.subTest(url=url):
                self.source.invalidate()
                row = self.row(1)
                row["head"]["repo"]["html_url"] = url
                self.api.pulls.return_value = [self.row(2), row]
                result = self.run_survey()
                self.assertTrue(result.github_failed)
                self.assertEqual(result.dependency_wait["kind"], "Incomplete")
                self.assertFalse(result.open_prs)

    def test_credential_rotation_during_inventory_rejects_whole_survey(self):
        def changed(*_args, **_kwargs):
            self.api._token = "rotated"
            return [self.row(1)]

        self.api.pulls.side_effect = changed
        result = self.run_survey()
        self.assertTrue(result.github_failed)
        self.assertEqual(result.open_prs, [])

    def test_different_credential_never_reuses_another_inventory(self):
        cache = BoundedReadCache()
        self.source = ForgejoReadSource(self.repo, self.api, cache=cache)
        self.run_survey()
        denied = Mock(forge=self.forge, _token="denied", max_bytes=1_000_000)
        denied.pulls.side_effect = ForgeError("denied", status=403)
        other = ForgejoReadSource(self.repo, denied, cache=cache)
        self.assertIsNone(other.open_prs())
        self.assertEqual(other.last_failure.status, 403)


if __name__ == "__main__":
    unittest.main()

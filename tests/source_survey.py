#!/usr/bin/env python3
"""The actual source survey uses native IDs and never native build authority."""

import dataclasses
import importlib
import json
import random
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

    def test_adopted_forgejo_authors_are_maintained_but_not_counted_as_authored(self):
        with patch.dict("os.environ", {"TAUCETI_FORGEJO_ADOPTED_AUTHOR_IDS": "3"}):
            result = self.run_survey()
            self.assertTrue(result.owns(result.open_prs[0]))
            self.assertTrue(result.owns(result.open_prs[1]))
            self.assertFalse(result.owns(result.open_prs[2]))
            self.assertTrue(owns_author(result, {"id": self.forge.key + "/users/3", "login": "kim"}))
            self.assertFalse(owns_author(result, {"id": "forgejo:https://other.example.org/users/3", "login": "kim"}))
            # Authoring pressure and "mine" counts still cover only the worker's own PRs.
            self.assertEqual(result.n_mine_open, 1)
            self.assertEqual([p.number for p in result._mine_open_prs], [1])
            awaiting = next(bucket for bucket in result.status_labels if bucket[0] == "awaiting-author")
            self.assertEqual(awaiting, ("awaiting-author", 3, 1))
            # An adopted author's fork PR is not maintained: the account cannot push its branch.
            fork = dataclasses.replace(
                result.open_prs[1], source_head_repository=RepositoryRef(self.forge, "kim/project")
            )
            self.assertFalse(result.owns(fork))
            self.assertTrue(
                result.owns(dataclasses.replace(result.open_prs[0], source_head_repository=fork.source_head_repository))
            )

    def test_adopted_author_ids_are_validated_and_forgejo_only(self):
        from tauceti_worker.source_identity import adopted_author_ids, maintains_author

        actor = SourceActor.from_native(self.forge, {"id": 9, "login": "bot"})
        github_actor = SourceActor.from_native(Forge("github", "https://github.com"), {"id": 9, "login": "bot"})
        with patch.dict("os.environ", {"TAUCETI_FORGEJO_ADOPTED_AUTHOR_IDS": " 3, 5 "}):
            self.assertEqual(adopted_author_ids(), frozenset({3, 5}))
            self.assertTrue(maintains_author(actor, self.forge, 9))
            self.assertTrue(maintains_author(actor, self.forge, 3))
            self.assertFalse(maintains_author(actor, self.forge, 4))
            self.assertFalse(maintains_author(actor, self.forge, "3"))
            self.assertFalse(maintains_author(None, self.forge, 3))
            self.assertFalse(maintains_author(github_actor, github_actor.principal.forge, 3))
            other = Forge("forgejo", "https://other.example.org")
            self.assertFalse(maintains_author(actor, other, 3))
            self.assertFalse(maintains_author(actor, other, 9))
        with patch.dict("os.environ", {"TAUCETI_FORGEJO_ADOPTED_AUTHOR_IDS": "3,kim-em"}):
            with self.assertRaises(ValueError):
                adopted_author_ids()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adopted_author_ids(), frozenset())
            self.assertFalse(maintains_author(actor, self.forge, 3))

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


class BudgetedSurveys(unittest.TestCase):
    """An automatic survey lists once and reads review history for at most K PRs."""

    OPEN = 40  # PR numbers 1..40; odd ones are this worker's own

    def setUp(self):
        from tauceti_tools.ci_candidate import RepositoryIdentity

        from tauceti_worker.config import Config
        from tauceti_worker.forgejo_worker import ForgejoWorkerSource
        from tauceti_worker.worker_sources import source_config

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        shared = patch.dict("os.environ", TAUCETI_GITHUB_SHARED_DIR=str(root / "shared"))
        shared.start()
        self.addCleanup(shared.stop)
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.api = Mock(forge=self.forge, _token="secret", max_bytes=1_000_000, max_pages=100)
        self.api.request.return_value = {"id": 3, "login": "worker"}
        self.heads = {n: "a" * 40 for n in range(1, self.OPEN + 1)}
        self.updated = {n: "2026-11-07T00:00:00Z" for n in self.heads}
        self.reviewed = {}  # PR -> head its scoreboard reviewed; verdicts in self.states
        self.states = {}
        self.api.pulls.side_effect = lambda *args, **kwargs: [self.row(n) for n in sorted(self.heads, reverse=True)]
        self.detail = []  # (endpoint, PR) for every per-PR read

        def comments(repository, number):
            self.detail.append(("comments", number))
            if number not in self.reviewed:
                return []
            meta = {"head_sha": self.reviewed[number], "states": self.states.get(number, {"style": "green"})}
            return [
                {
                    "id": number,
                    "body": "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + json.dumps(meta) + "-->",
                    "created_at": "2026-11-07T00:00:00Z",
                    "updated_at": "2026-11-07T00:00:00Z",
                    "html_url": self.repo.pull_url(number) + f"#issuecomment-{number}",
                    "user": {"id": 2, "login": "reviewer"},
                }
            ]

        def reviews(repository, number):
            self.detail.append(("reviews", number))
            return []

        self.api.comments.side_effect = comments
        self.api.reviews.side_effect = reviews
        self.api.pull.side_effect = lambda repository, number: self.detail.append(("pull", number))
        identity = RepositoryIdentity(self.repo, 7)
        self.source = ForgejoWorkerSource(identity, self.api, token_file=root / "token")
        self.source.shared_inventory = True
        base = Config(
            "worker",
            root,
            root,
            root / "state",
            root / "checkout",
            root / "store",
            root / "scoreboard",
            root / "logs",
            root / "quota",
        )
        self.cfg = source_config(base, self.source)

    def row(self, number):
        mine = number % 2 == 1
        return {
            "number": number,
            "id": number + 100,
            "title": "Proof",
            "body": "",
            "flow": 0,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable": True,
            "html_url": self.repo.pull_url(number),
            "user": {"id": 3 if mine else 4, "login": "worker" if mine else "other"},
            "head": {
                "sha": self.heads[number],
                "label": f"roadmap/work-{number}",
                "repo": {
                    "id": 7,
                    "name": "project",
                    "full_name": self.repo.name,
                    "html_url": self.repo.url,
                    "owner": {"id": 10, "login": "one"},
                },
            },
            "labels": [{"id": 1, "name": "roadmap/PDE"}],
            "updated_at": self.updated[number],
        }

    def survey(self, *, numbers=(), seed=0):
        from tauceti_worker.review_state import ReviewState
        from tauceti_worker.source_survey import worker_survey
        from tauceti_worker.survey import Counters

        self.detail.clear()
        with patch("tauceti_worker.source_survey.random", random.Random(seed)):
            return worker_survey(
                self.cfg,
                self.source,
                ReviewState(self.cfg, self.source),
                Counters(self.cfg),
                roadmap="any",
                skip=[],
                deep=True,
                stages=["rebase", "fix", "review"],
                numbers=numbers,
                count_all_roadmaps=True,
            )

    def read_prs(self):
        return {number for _, number in self.detail}

    def test_survey_makes_one_listing_and_at_most_k_detail_reads(self):
        from tauceti_worker.constants import SURVEY_DETAIL_READS

        self.reviewed = {n: "a" * 40 for n in self.heads}  # nothing to do anywhere
        covered = set()
        for seed in range(3):
            listings = self.api.pulls.call_count
            result = self.survey(seed=seed)
            self.assertLessEqual(self.api.pulls.call_count - listings, 1)
            self.assertFalse(result.github_failed)
            self.assertEqual(len(result.open_prs), self.OPEN)
            self.assertEqual(len(self.read_prs()), SURVEY_DETAIL_READS)
            self.assertEqual(result.detail_reads, SURVEY_DETAIL_READS)
            # A PR already read at its (updatedAt, head) is answered from the shared cache.
            self.assertFalse(self.read_prs() & covered)
            covered |= self.read_prs()
            self.assertFalse(result.reviewable.actionable or result.needs_fix.actionable)
        self.assertEqual(self.api.pulls.call_count, 1, "one shared listing serves surveys within its TTL")

    def test_survey_stops_at_the_first_eligible_read_and_keeps_cached_work(self):
        result = self.survey(seed=1)  # no PR has a scoreboard: the first PR read needs review
        self.assertEqual(len(self.read_prs()), 1)
        first = {c.pr for c in result.reviewable.actionable}
        self.assertEqual(first, self.read_prs())
        # The next survey classifies that PR from the cache, so it is still offered without a read.
        result = self.survey(seed=2)
        self.assertEqual(len(self.read_prs()), 1)
        self.assertTrue(first <= {c.pr for c in result.reviewable.actionable})
        self.assertFalse(self.read_prs() & first)

    def test_changed_prs_are_read_again_and_owned_blocking_reviews_become_fixes(self):
        self.reviewed = {n: "a" * 40 for n in self.heads}
        warmed = set()
        for seed in range(20):
            self.survey(seed=seed)
            warmed |= self.read_prs()
        self.assertEqual(warmed, set(self.heads))
        self.assertEqual(self.survey(seed=99).detail_reads, 0, "a warm cache needs no detail reads")
        # A blocking review on an owned PR moves its clock; only that PR is read.
        self.states[7] = {"style": "blocking_request"}
        self.updated[7] = "2026-11-08T00:00:00Z"
        from tauceti_worker.github_transport import invalidate_shared
        from tauceti_worker.source_identity import discovery_scope

        invalidate_shared(discovery_scope(self.source))
        result = self.survey(seed=5)
        self.assertEqual(self.read_prs(), {7})
        self.assertEqual([c.pr for c in result.needs_fix.actionable], [7])
        # A push to another PR is a new head: review work, again for one read.
        self.heads[8] = "b" * 40
        invalidate_shared(discovery_scope(self.source))
        result = self.survey(seed=6)
        self.assertEqual(self.read_prs(), {8})
        self.assertEqual([c.pr for c in result.reviewable.actionable], [8])
        self.assertEqual([c.pr for c in result.needs_fix.actionable], [7])

    def test_ambiguous_owned_mergeability_checks_share_the_detail_budget(self):
        from tauceti_worker.constants import SURVEY_DETAIL_READS
        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        self.reviewed = {n: "a" * 40 for n in self.heads}
        listing = self.api.pulls.side_effect
        self.api.pulls.side_effect = lambda *a, **k: [{**row, "mergeable": False} for row in listing(*a, **k)]
        checked = []

        def resolve(source, state, number, head):
            checked.append(number)
            return "MERGEABLE"

        with patch.object(ForgejoWorkerSource, "resolve_mergeability", resolve):
            result = self.survey(seed=3)
            self.assertEqual(len(checked), SURVEY_DETAIL_READS)
            self.assertTrue(all(number % 2 for number in checked), "only owned PRs are checked")
            # Conflict checks spent the budget; only those PRs may also read their history.
            self.assertLessEqual(self.read_prs(), set(checked))
            self.assertFalse(result.rebaseable.actionable)
            checked.clear()
            conflicting = {9}
            resolve_conflicts = lambda source, state, number, head: (  # noqa: E731
                checked.append(number) or ("CONFLICTING" if number in conflicting else "MERGEABLE")
            )
            with patch.object(ForgejoWorkerSource, "resolve_mergeability", resolve_conflicts):
                for seed in range(30):
                    result = self.survey(seed=seed)
                    self.assertLessEqual(len(set(checked)), SURVEY_DETAIL_READS)
                    if result.rebaseable.actionable:
                        break
                    checked.clear()
            self.assertEqual([c.pr for c in result.rebaseable.actionable], [9])
            self.assertEqual(checked[-1], 9, "the walk stops at the first conflict")

    def test_a_failed_thread_read_is_retried_while_its_scoreboard_is_cached(self):
        from tauceti_tools.forge import Incomplete

        self.reviewed = {n: "a" * 40 for n in self.heads}
        reviews = self.api.reviews.side_effect

        def failing(repository, number):
            self.detail.append(("reviews", number))
            raise Incomplete("transient", status=502)

        self.api.reviews.side_effect = failing
        self.survey(seed=4)
        failed = {number for endpoint, number in self.detail if endpoint == "reviews"}
        self.assertTrue(failed)
        self.api.reviews.side_effect = reviews
        self.survey(seed=4)
        # Same order, scoreboards now cached: the walk spends its reads on the missing threads.
        self.assertLessEqual(failed, {number for endpoint, number in self.detail if endpoint == "reviews"})

    def test_a_reused_thread_observation_keeps_its_age(self):
        from tauceti_worker.review_state import ReviewState

        self.reviewed = {n: "a" * 40 for n in self.heads}
        self.survey(seed=0)
        read = self.read_prs()
        rs = ReviewState(self.cfg, self.source)
        for number in read:
            rs._contest_path(number).unlink()
        self.survey(seed=0)
        self.assertFalse(self.read_prs() & read, "answered from the shared cache")
        # Rewriting the sidecar now would restart the clock on an observation the shared cache aged.
        self.assertFalse(any(rs._contest_path(number).exists() for number in read))

    def test_explicit_targets_are_read_in_full(self):
        result = self.survey(numbers=(2, 4, 6, 8, 10, 12))
        self.assertEqual(self.read_prs(), {2, 4, 6, 8, 10, 12})
        self.assertEqual(result.detail_reads, 0)
        self.assertEqual({c.pr for c in result.reviewable.actionable}, {2, 4, 6, 8, 10, 12})


if __name__ == "__main__":
    unittest.main()

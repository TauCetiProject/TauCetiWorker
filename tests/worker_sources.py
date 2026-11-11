#!/usr/bin/env python3
"""Cross-forge routing, independent outages, identities and budgets."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_worker.config import Config, Die
from tauceti_worker.multi_source import collect, deduplicate
from tauceti_worker.review_state import Meta
from tauceti_worker.source_execution import still_actionable
from tauceti_worker.source_identity import SourceActor
from tauceti_worker.source_survey import worker_survey
from tauceti_worker.survey import Candidate, Counters, PRInfo, Survey
from tauceti_worker.worker_sources import configure, source_config


class Sources(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/project")
        self.identity = RepositoryIdentity(self.repo, 7)
        self.actor = SourceActor.from_native(self.repo.forge, {"id": 3, "login": "worker"})
        self.source = Mock(
            repository=self.repo, identity=self.identity, repo=self.repo.name, review_repository=self.repo.key
        )
        self.source.authenticated_actor.return_value = self.actor
        self.source.binding_current.return_value = True
        self.source.fresh_claim_age.return_value = None
        self.cfg = Config(
            "worker",
            self.root,
            self.root,
            self.root / "state",
            self.root / "checkout",
            self.root / "store",
            self.root / "scoreboard",
            self.root / "logs",
            self.root / "quota",
        )

    def row(self, number=1, *, author=3, login="worker", head="a" * 40):
        return {
            "number": number,
            "title": "Proposal",
            "body": "",
            "headRefOid": head,
            "headRefName": "proposal",
            "headRepository": {"nameWithOwner": self.repo.name, "name": "project"},
            "headRepositoryOwner": {"login": "one"},
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "author": {"id": self.repo.forge.key + "/users/" + str(author), "login": login},
            "labels": [],
            "updatedAt": "2026-11-11T00:00:00Z",
        }

    def native_survey(self, rows, *, blocking=False, metadata=None):
        self.source.open_prs.return_value = rows
        rs = Mock()
        if metadata is None and blocking:
            metadata = Meta({"head_sha": "a" * 40, "states": {"style": "blocking_request"}}, "fresh")
        rs.gh_meta.return_value = metadata or Meta({}, "missing")
        rs.newest_contest_reply.return_value = None
        rs.ledger_clean_head.return_value = ""
        rs.ledger_blocking.return_value = blocking
        cfg = source_config(self.cfg, self.source)
        return worker_survey(
            cfg,
            self.source,
            rs,
            Counters(cfg),
            roadmap="any",
            skip=[],
            deep=True,
            stages=["review", "fix", "rebase"],
            numbers=(),
            count_all_roadmaps=True,
        )

    def test_default_sources_and_new_pr_destination(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace())
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "github")

    def test_explicit_numeric_target_preserves_github_default(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace(pr=["12"]))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")
            with self.assertRaises(Die):
                configure(SimpleNamespace(pr=["12"], forgejo=True))

    def test_native_destination_is_independent(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(SimpleNamespace(forgejo=True, new_pr_forge="forgejo"))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "forgejo")

    def test_native_budgets_preserve_github_files_and_separate_server_ids(self):
        self.cfg.state.mkdir()
        (self.cfg.state / "fix-1-aaaaaaaaaaaa").write_text("2")
        cfg = source_config(self.cfg, self.source)
        self.assertEqual(Counters(cfg).read("fix-1-aaaaaaaaaaaa"), 0)
        Counters(cfg).write("fix-1-aaaaaaaaaaaa", 1)
        self.assertEqual(Counters(self.cfg).read("fix-1-aaaaaaaaaaaa"), 2)
        other = Mock(repository=self.repo, identity=RepositoryIdentity(self.repo, 8))
        self.assertNotEqual(source_config(self.cfg, other).state, cfg.state)

    def test_native_review_selection_keeps_qualified_identity_and_unknown_ci(self):
        result = self.native_survey([self.row()])
        self.assertIsNone(result.dependency_wait)
        self.assertEqual(len(result.reviewable.actionable), 1)
        candidate = result.reviewable.actionable[0]
        self.assertEqual(candidate.source_repository, self.repo)
        self.assertEqual(candidate.source_repository_id, 7)
        self.assertFalse(result.open_prs[0].build_success)

    def test_same_login_does_not_authorize_repair_for_another_native_actor(self):
        result = self.native_survey([self.row(1), self.row(2, author=4)], blocking=True)
        self.assertEqual([candidate.pr for candidate in result.needs_fix.actionable], [1])

    def test_unavailable_history_never_becomes_unreviewed_work(self):
        result = self.native_survey([self.row()], metadata=Meta({}, "fetch_failed"))
        self.assertFalse(result.reviewable.actionable)
        self.assertEqual(result.discovery_gap_prs, [1])

    def test_unavailable_native_inventory_stays_a_failure(self):
        result = self.native_survey(None)
        self.assertTrue(result.github_failed)
        self.assertFalse(result.reviewable.actionable)

    def test_fresh_dispatch_rejects_a_candidate_from_the_other_source(self):
        candidate = Candidate(
            1, "a" * 40, source_repository=RepositoryRef(GITHUB, self.repo.name), source_repository_id=7
        )
        self.assertFalse(still_actionable("review", SimpleNamespace(gh=self.source), None, candidate))
        self.source.live_pull.assert_not_called()

    def test_github_identity_outage_does_not_hide_native_inventory(self):
        worker = SimpleNamespace(cfg=self.cfg, gh=Mock(repo="one/project"), rs=Mock(), counters=Mock(), rc=None)
        native = Survey(worker_id="worker")
        opts = SimpleNamespace(only=["review"], prs=())
        with (
            patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}),
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch("tauceti_worker.multi_source.survey", side_effect=[Die("offline"), native]),
            patch("tauceti_worker.multi_source.ReviewState"),
        ):
            result = collect(worker, opts)
        self.assertTrue(result[0][2].github_failed)
        self.assertIs(result[1][2], native)

    def observations(self, github, native):
        first, second = Survey(worker_id="test"), Survey(worker_id="test")
        first.open_prs, second.open_prs = github, native
        first.reviewable.actionable = [Candidate(pr.number, pr.head_oid) for pr in github]
        second.reviewable.actionable = [Candidate(pr.number, pr.head_oid) for pr in native]
        return [
            ("github", SimpleNamespace(gh=SimpleNamespace(repo="one/project")), first),
            ("forgejo", SimpleNamespace(gh=self.source), second),
        ]

    def test_equal_numbers_on_different_sources_are_independent(self):
        observations = self.observations(
            [PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})],
            [PRInfo.from_json({"number": 1, "headRefOid": "b" * 40})],
        )
        deduplicate(observations)
        self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [1, 1])

    def test_identical_heads_and_target_markers_are_not_paid_twice(self):
        for same_head in (True, False):
            body = '<!--tauceti-target:v1 {"focus":"Algebra","id":"one-target"}-->'
            observations = self.observations(
                [PRInfo.from_json({"number": 1, "headRefOid": "a" * 40, "body": body})],
                [PRInfo.from_json({"number": 2, "headRefOid": ("a" if same_head else "b") * 40, "body": body})],
            )
            deduplicate(observations)
            self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [1, 0])

    def test_explicit_copy_keeps_work_on_its_original_forge(self):
        observations = self.observations(
            [
                PRInfo.from_json(
                    {
                        "number": 1,
                        "headRefOid": "a" * 40,
                        "body": "Origin-PR: https://git.example.org/one/project/pulls/2",
                    }
                )
            ],
            [PRInfo.from_json({"number": 2, "headRefOid": "a" * 40})],
        )
        deduplicate(observations)
        self.assertEqual([len(row[2].reviewable.actionable) for row in observations], [0, 1])

    def test_native_review_entrypoint_delegates_to_the_common_runtime(self):
        from tauceti_worker.source_review import main
        from tauceti_worker.review_runtime import ReviewRuntime

        runtime = ReviewRuntime(self.root / "engine", "a" * 40)
        argv = [
            "review",
            "--pr",
            "1",
            "--head",
            "a" * 40,
            "--work",
            str(self.root / "work"),
            "--store",
            str(self.root / "store"),
            "--reviewer",
            "codex",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("tauceti_worker.source_review.load_source", return_value=self.source),
            patch("tauceti_worker.source_review.resolve", return_value=runtime),
            patch("tauceti_worker.source_review.subprocess.run", return_value=SimpleNamespace(returncode=83)) as run,
        ):
            self.assertEqual(main(), 83)
        command = run.call_args.args[0]
        self.assertEqual(command[1], str(runtime.directory / "runner/cli.py"))
        self.assertEqual(command[command.index("--repo") + 1], self.repo.key)
        self.assertEqual(command[command.index("--expect-head") + 1], "a" * 40)
        self.assertIn("--post", command)
        self.assertIn("--no-sync", command)

    def test_numeric_target_with_inherited_both_still_defaults_to_github(self):
        with patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}, clear=True):
            configure(SimpleNamespace(pr=["1"]))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")

    def test_other_origins_do_not_inherit_the_canonical_private_token(self):
        with patch.dict(os.environ, {"TAUCETI_FORGEJO_ORIGIN": "https://other.example.org"}, clear=True):
            configure(SimpleNamespace())
            self.assertNotIn("TAUCETI_FORGEJO_TOKEN_FILE", os.environ)

    def test_source_claim_failure_before_inference_does_not_consume_review_budget(self):
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock(), rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", return_value=81),
        ):
            self.assertIsNone(review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False))
        worker.counters.incr.assert_not_called()
        worker.counters.write.assert_not_called()

    def test_source_failure_at_dispatch_still_allows_the_other_source(self):
        import contextlib

        from tauceti_worker.config import GitHubUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[0][2].rebaseable.actionable.append(Candidate(1, "a" * 40))
        observations[1][2].reviewable.actionable.append(Candidate(2, "b" * 40))
        for _, worker, _ in observations:
            worker.cfg = self.cfg
        options = RoundOpts(only=["rebase", "review"], agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch("tauceti_worker.work_units.run_round", side_effect=[GitHubUnavailable("offline"), 0]) as execute,
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args.args[1].only, ["review"])

    def test_shared_github_intentions_use_the_github_identity(self):
        from tauceti_worker.intentions import own_identities

        self.source.shared_github_login.return_value = "github-worker"
        with (
            patch("tauceti_worker.worker_sources.current", return_value=self.source),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_EXTRA_IDENTITIES": ""}),
        ):
            self.assertEqual(own_identities(), {"github-worker"})

    def test_multi_source_collection_preserves_fleet_focus(self):
        worker = SimpleNamespace(cfg=self.cfg, gh=Mock(repo="one/project"), rs=Mock(), counters=Mock(), rc=None)
        opts = SimpleNamespace(only=["review"], prs=())
        with (
            patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}),
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch(
                "tauceti_worker.multi_source.survey", side_effect=[Survey(worker_id="test"), Survey(worker_id="test")]
            ) as discover,
            patch("tauceti_worker.multi_source.ReviewState"),
            patch("tauceti_worker.work_units.round_scope", return_value=("Algebra", False, True)),
        ):
            result = collect(worker, opts)
        self.assertTrue(all(call.kwargs["selected_roadmap"] == "Algebra" for call in discover.call_args_list))
        self.assertTrue(all(call.kwargs["count_all_roadmaps"] for call in discover.call_args_list))
        self.assertTrue(all(sv._round_scope == (False, True) for _, _, sv in result))

    def test_unchanged_native_review_does_not_charge_error_allowance(self):
        from tauceti_worker.source_execution import review

        counters = Counters(self.cfg)
        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=counters, rs=Mock())
        worker.rs.review_rounds.return_value = 0

        def engine(command, log, *args):
            result = log.parent / "review-results" / (log.stem + ".json")
            result.write_text(json.dumps(dict(head_sha="a" * 40, changed=False)))
            return 0

        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", side_effect=engine),
        ):
            self.assertIsNone(review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False))
        self.assertEqual(counters.read("review-err-1"), 0)
        self.assertTrue(counters.unchanged_review_recent(1, "a" * 40))

    def test_native_recovery_runs_without_provider_admission(self):
        import contextlib

        from tauceti_worker.multi_source import recover_native

        with (
            patch("tauceti_worker.multi_source.native_source", return_value=self.source),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch("tauceti_worker.multi_source.ReviewState"),
            patch("tauceti_worker.source_publication.pending", return_value=True) as pending,
            patch("tauceti_worker.cli.resolve_work_model", side_effect=AssertionError("provider admission")),
        ):
            self.source.anonymous_discovery = False
            self.assertTrue(recover_native(self.cfg, None, SimpleNamespace()))
            pending.assert_called_once()

    def test_native_execution_failure_does_not_invalidate_github_authoring_inventory(self):
        import contextlib

        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[1][2].reviewable.actionable = [Candidate(2, "b" * 40)]
        for _, worker, _ in observations:
            worker.cfg = self.cfg
        options = RoundOpts(
            only=["review", "roadmap"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True
        )
        with (
            patch.dict(os.environ, {"TAUCETI_NEW_PR_FORGE": "github"}),
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.multi_source.activate", return_value=contextlib.nullcontext()),
            patch(
                "tauceti_worker.work_units.run_round", side_effect=[SourceUnavailable("claim unavailable"), 0]
            ) as execute,
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertEqual(execute.call_args.args[1].only, ["roadmap"])
        self.assertIsNone(observations[1][2].dependency_wait)

    def test_multi_source_fleet_backpressure_falls_back_to_global_maintenance(self):
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        scoped, global_work = self.observations([], []), self.observations([], [])
        for _, _, result in scoped:
            result._round_scope = (False, True)
            result.n_mine_open = result.max_open_prs
        global_work[0][2].reviewable.actionable = [Candidate(1, "a" * 40)]
        options = RoundOpts(
            only=["review", "roadmap"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True
        )
        with (
            patch("tauceti_worker.multi_source.collect", side_effect=[scoped, global_work]) as collect,
            patch("tauceti_worker.work_units.run_round", return_value=0),
        ):
            self.assertEqual(run_round(None, options), 0)
        self.assertFalse(collect.call_args.kwargs["use_fleet_focus"])

    def test_native_branch_claim_outage_refunds_unlaunched_repair(self):
        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.work_units import _do_fixlike

        pr = PRInfo.from_json(
            {
                "number": 1,
                "headRefOid": "a" * 40,
                "headRefName": "proposal",
                "headRepositoryOwner": {"login": "worker"},
                "headRepository": {"name": "project"},
            }
        )
        counters = Counters(self.cfg)
        counters.write("fix-1", 1)
        worker = SimpleNamespace(claims=Mock(), counters=counters)
        worker.claims.begin_branch_work.side_effect = SourceUnavailable("claim unavailable")
        with self.assertRaises(SourceUnavailable):
            _do_fixlike(
                worker,
                SimpleNamespace(open_prs=[pr]),
                Candidate(1, "a" * 40),
                None,
                False,
                prompt_file="fix.md",
                label="fix",
                charged=("fix-1",),
            )
        self.assertEqual(counters.read("fix-1"), 0)

    def test_source_switch_combinations_and_loop_child_handoff(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.worker_sources import selection_arguments

        for flags, expected in (
            ([], "github"),
            (["--github"], "github"),
            (["--forgejo"], "both"),
            (["--github", "--forgejo"], "both"),
            (["--no-github", "--forgejo"], "forgejo"),
            (["--forgejo", "--no-forgejo"], "github"),
        ):
            with (
                self.subTest(flags=flags),
                patch.dict(
                    os.environ, {"TAUCETI_WORKER_FORGES": "both", "TAUCETI_NEW_PR_FORGE": "forgejo"}, clear=True
                ),
            ):
                configure(build_parser().parse_args(["work", *flags]))
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)
                self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "github")
                child = build_parser().parse_args(["_round", *selection_arguments()])
                configure(child)
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)

    def test_source_switches_work_before_or_after_subcommand(self):
        from tauceti_worker.cli import build_parser

        for argv in (["--forgejo", "work"], ["work", "--forgejo"], ["--forgejo", "status"], ["--forgejo"]):
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True):
                configure(build_parser().parse_args(argv))
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")

    def test_empty_source_selection_and_ambiguous_target_are_explicit_errors(self):
        from tauceti_worker.cli import build_parser

        for argv in (["--no-github"], ["--no-github", "--no-forgejo"], ["--forgejo", "--pr", "2"]):
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), self.assertRaises(Die):
                configure(build_parser().parse_args(["work", *argv]))

    def test_explicit_native_destination_survives_loop_handoff(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.worker_sources import selection_arguments

        with patch.dict(os.environ, {}, clear=True):
            configure(build_parser().parse_args(["work", "--forgejo", "--new-pr-forge", "forgejo"]))
            child = build_parser().parse_args(["_round", *selection_arguments()])
            configure(child)
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "both")
            self.assertEqual(os.environ["TAUCETI_NEW_PR_FORGE"], "forgejo")

    def test_driver_forwards_source_flags_into_the_actual_round_command(self):
        from tauceti_worker.cli import build_parser
        from tauceti_worker.loop import cmd_loop

        for flags, expected in (([], "github"), (["--forgejo"], "both"), (["--no-github", "--forgejo"], "forgejo")):
            with self.subTest(flags=flags), patch.dict(os.environ, {}, clear=True):
                args = build_parser().parse_args(["work", "--loop", *flags])
                configure(args)
                with (
                    patch("tauceti_worker.loop.run_round_subprocess", side_effect=KeyboardInterrupt) as launch,
                    patch("tauceti_worker.loop.github_budget", return_value={}),
                    patch("tauceti_worker.loop.fleet_admission_reason", return_value=None),
                    patch("tauceti_worker.verification_jobs.resume_pending"),
                    patch("tauceti_worker.verification_jobs.reap_continuations"),
                    patch("tauceti_worker.repair_recovery.due", return_value=False),
                ):
                    cmd_loop(args, self.cfg, only=["review"], agent="deepseek")
                child = build_parser().parse_args(["_round", *launch.call_args.args[0]])
                configure(child)
                self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], expected)

    def test_dual_source_idle_preserves_actionable_github_discovery_error(self):
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        observations[0][2].dependency_wait = {"dependency": "github-state", "reason": "auth", "actionable": True}
        observations[0][2].errors = ["shared state credential rejected"]
        options = RoundOpts(only=["review"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            self.assertRaisesRegex(Die, "operator action"),
        ):
            run_round(None, options)

    def test_dual_source_idle_preserves_github_wait_deadline(self):
        from tauceti_worker.config import GitHubUnavailable, SharedStateUnavailable
        from tauceti_worker.multi_source import run_round
        from tauceti_worker.work_units import RoundOpts

        observations = self.observations([], [])
        options = RoundOpts(only=["review"], agent="auto", work_model="codex", sandbox_host=True, dry_run=True)
        observations[0][2].dependency_wait = {"dependency": "github-state", "reason": "transport", "retry_at": 1234}
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            self.assertRaises(SharedStateUnavailable) as caught,
        ):
            run_round(None, options)
        self.assertEqual(caught.exception.retry_at, 1234)
        observations[0][2].dependency_wait = None
        observations[0][2].reviewable.actionable = [Candidate(1, "a" * 40)]
        wait = GitHubUnavailable("rate limited", retry_at=2345)
        with (
            patch("tauceti_worker.multi_source.collect", return_value=observations),
            patch("tauceti_worker.work_units.run_round", side_effect=wait),
            self.assertRaises(GitHubUnavailable) as caught,
        ):
            run_round(None, options)
        self.assertIs(caught.exception, wait)

    def test_contest_limit_is_not_reported_as_a_review_error(self):
        from tauceti_worker.constants import MAX_REVIEW_CONTESTS

        cfg = source_config(self.cfg, self.source)
        Counters(cfg).write("review-contest-1", MAX_REVIEW_CONTESTS)
        self.source.open_prs.return_value = [self.row()]
        rs = Mock()
        rs.gh_meta.return_value = Meta({"head_sha": "a" * 40, "states": {"style": "blocking_request"}}, "fresh")
        rs.ledger_blocking.return_value = True
        rs.ledger_clean_head.return_value = "a" * 40
        rs.newest_contest_reply.return_value = {"id": 5, "rubric": "style"}
        result = worker_survey(
            cfg,
            self.source,
            rs,
            Counters(cfg),
            roadmap="any",
            skip=[],
            deep=True,
            stages=["review"],
            numbers=(),
            count_all_roadmaps=True,
        )
        self.assertEqual([candidate.pr for candidate in result.reviewable.suppressed], [1])
        self.assertEqual(result.review_stuck, [])

    def test_shared_preparation_failure_does_not_consume_inference_error_allowance(self):
        from tauceti_worker.source_execution import review
        from tauceti_worker.config import NoProgress

        counters = Counters(self.cfg)
        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=counters, rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with (
            patch("tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["engine"])),
            patch("tauceti_worker.work_units.run_to_logfile", return_value=83),
        ):
            with self.assertRaises(NoProgress):
                review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False)
        self.assertEqual(counters.read("review-err-1"), 0)

    def test_shared_workspace_retains_paid_publication_after_interruption(self):
        from tauceti_worker.retention import review_workspace, collect_review_scratch, fresh_report

        with review_workspace(self.cfg) as work:
            receipt = work / "publication.json"
            receipt.write_text(json.dumps(dict(state="prepared", threads=[dict(body="paid finding")])))
        collect_review_scratch(self.cfg, fresh_report())
        self.assertTrue(work.is_dir())
        self.assertEqual(json.loads(receipt.read_text())["threads"][0]["body"], "paid finding")
        receipt.write_text(json.dumps(dict(state="published")))
        with patch("tauceti_worker.work_recovery.live_users", return_value=[]):
            collect_review_scratch(self.cfg, fresh_report())
        self.assertFalse(work.exists())

    def test_orphan_cleanup_requires_stopped_owner_and_no_live_users(self):
        from tauceti_worker.source_publication import pending

        work = self.cfg.state / "review-work/orphan"
        work.mkdir(parents=True)
        (work / "code").mkdir()
        (work / "review-context.json").write_text(
            json.dumps({"source": self.identity.record(), "store": str(self.cfg.store), "pr": 1, "process": {"pid": 7}})
        )
        with patch("tauceti_worker.work_recovery.owner_stopped", return_value=False):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertTrue(work.exists())
        with (
            patch("tauceti_worker.work_recovery.owner_stopped", return_value=True),
            patch("tauceti_worker.work_recovery.live_users", return_value=["active reviewer"]),
        ):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertTrue(work.exists())
        with (
            patch("tauceti_worker.work_recovery.owner_stopped", return_value=True),
            patch("tauceti_worker.work_recovery.live_users", return_value=[]),
        ):
            self.assertFalse(pending(self.source, self.cfg))
            self.assertFalse(work.exists())

    def test_native_review_uses_common_coordinator(self):
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock(), rs=Mock())
        worker.rs.review_rounds.return_value = 0
        with patch("tauceti_worker.work_units.do_review", return_value=0) as common:
            self.assertEqual(
                review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False), 0
            )
        self.assertIs(common.call_args.args[0], worker)

    def test_compact_github_survey_recovers_exact_head_target_markers_from_cached_inventory(self):
        from tauceti_worker.multi_source import enrich_targets

        github = PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})
        body = '<!--tauceti-target:v1 {"focus":"Algebra","id":"one-target"}-->\nOrigin-PR: https://git.example.org/one/project/pulls/2'
        observations = self.observations(
            [github], [PRInfo.from_json({"number": 2, "headRefOid": "b" * 40, "body": body})]
        )
        with patch(
            "tauceti_worker.github_state.read",
            return_value=(True, [{"number": 1, "headRefOid": "a" * 40, "body": body}]),
        ) as read:
            enrich_targets(observations[0][1], observations[0][2])
        self.assertTrue(observations[0][2].target_inventory_available)
        self.assertEqual(observations[0][2].open_prs[0].origin_pr, "https://git.example.org/one/project/pulls/2")
        deduplicate(observations)
        self.assertEqual([len(item[2].reviewable.actionable) for item in observations], [0, 1])
        read.assert_called_once_with("one/project", "open_prs", _optional=True)

    def test_marker_inventory_cannot_apply_a_body_from_another_head(self):
        from tauceti_worker.multi_source import enrich_targets

        observations = self.observations([PRInfo.from_json({"number": 1, "headRefOid": "a" * 40})], [])
        with patch(
            "tauceti_worker.github_state.read",
            return_value=(
                True,
                [
                    {
                        "number": 1,
                        "headRefOid": "b" * 40,
                        "body": "Origin-PR: https://git.example.org/one/project/pulls/2",
                    }
                ],
            ),
        ):
            enrich_targets(observations[0][1], observations[0][2])
        self.assertFalse(observations[0][2].target_inventory_available)
        self.assertFalse(observations[0][2].open_prs[0].origin_pr)


if __name__ == "__main__":
    unittest.main()

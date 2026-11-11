#!/usr/bin/env python3
"""Cross-forge routing, independent outages, identities and budgets."""

import json
import os
import subprocess
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
        self.source = Mock(repository=self.repo, identity=self.identity)
        self.source.authenticated_actor.return_value = self.actor
        self.source.binding_current.return_value = True
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

    def test_installed_review_engine_accepts_native_prepared_inputs_without_transport_credentials(self):
        from tauceti_worker.source_review import engine_command, inference_environment

        work = self.root / "review-work"
        work.mkdir()
        (work / "code").mkdir()
        (work / "diff.txt").write_text("diff --git a/Proof.lean b/Proof.lean\n+theorem proof : True := trivial\n")
        (work / "paths.z").write_bytes(b"Proof.lean\0")
        (work / "pr_desc.txt").write_text("A simple proof.")
        self.source.issue_comments.return_value = []
        self.source.review_comments.return_value = []

        def prepare_rubrics(directory, *args, **kwargs):
            if args[0] == "checkout":
                (directory / "rubrics").mkdir()
                (directory / "rubrics/_common.md").write_text("Return a structured verdict after the requested marker.")
                (directory / "rubrics/style.md").write_text("# Style\nReview the declaration's style.")
            return b""

        with patch("tauceti_worker.source_review.git", side_effect=prepare_rubrics):
            command = engine_command(
                self.source, 1, "a" * 40, ("b" * 40, "b" * 40), work, self.root / "review-store", "codex"
            )
        command.extend(["--rubrics", "style"])
        binary = self.root / "bin"
        binary.mkdir()
        codex = binary / "codex"
        codex.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """
import json, os, re, sys
assert not any(key.startswith(('GH_', 'TAUCETI_FORGEJO_', 'TAUCETI_SOURCE_', 'GIT_CONFIG_')) for key in os.environ)
prompt = sys.stdin.read()
marker = re.search(r'TAUCETI-VERDICT-[0-9a-f]+', prompt)[0]
print(json.dumps({'type':'item.completed','item':{'type':'agent_message',
                 'text':marker+'\\n'+json.dumps({'verdict':'approve','summary':'Style is clear.'})}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
"""
        )
        codex.chmod(0o700)
        parent = dict(
            os.environ,
            HOME=str(self.root),
            PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""),
            GH_TOKEN="github-test-credential",
            TAUCETI_FORGEJO_TOKEN_FILE="/private/native-token",
            TAUCETI_SOURCE_BINDING=json.dumps(self.identity.record()),
        )
        environment = inference_environment(parent)
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-1200:])
        plan = json.loads((work / "plan.json").read_text())
        self.assertEqual(plan["head_sha"], "a" * 40)
        self.assertFalse(plan["review_error"])
        self.assertIn(self.repo.key, (work / "scoreboard.md").read_text())
        archive = list((self.root / "review-store/outbox/records/runs").rglob("*.json"))
        self.assertTrue(archive)
        self.assertEqual(json.loads(archive[0].read_text())["repo"], self.repo.key)

        # Publish using native handoff, then ask the real installed engine to
        # repeat the same head. No provider may run a second time.
        from tauceti_worker.source_publication import publish, retain_plan

        native_comments = []
        self.source.live_pull.return_value = {"state": "open", "head": {"sha": "a" * 40}}
        self.source.issue_comments.side_effect = lambda *args, **kwargs: list(native_comments)

        def write(method, endpoint, body):
            native_comments[:] = [
                {
                    "id": 17,
                    "body": body["body"],
                    "user": {"id": 3, "login": "worker"},
                    "created_at": "2026-11-11T00:00:00Z",
                }
            ]

        self.source.api.request.side_effect = write
        store = self.root / "review-store"
        publication = retain_plan(
            self.source, SimpleNamespace(store=store / "ledger.json"), 1, "a" * 40, work / "plan.json", actor_id=3
        )
        self.assertTrue(publish(self.source, publication))
        ledger = json.loads((store / "ledger.json").read_text())["prs"]["1"]
        self.assertEqual(ledger["published_head_sha"], "a" * 40)
        self.assertNotIn("pending_publication_head_sha", ledger)
        (work / "code").mkdir()
        # An earlier engine answer has a native parent but is never a new contest.
        self.source.review_comments.return_value = [
            {"id": 10, "body": "<!--tauceti-rubric:style-->", "user": {"login": "worker"}},
            {
                "id": 11,
                "in_reply_to_id": 10,
                "body": "<!--tauceti-reply:style--> Reviewer answer",
                "user": {"login": "worker"},
            },
        ]
        with patch("tauceti_worker.source_review.git", side_effect=prepare_rubrics):
            command = engine_command(self.source, 1, "a" * 40, ("b" * 40, "b" * 40), work, store, "codex")
        self.assertEqual(json.loads((work / "replies.json").read_text()), {})
        command.extend(["--rubrics", "style"])
        codex.write_text("#!" + sys.executable + "\nraise RuntimeError('second model call')\n")
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-1200:])
        self.assertIs(json.loads((work / "plan.json").read_text())["review_changed"], False)
        # A saved in-progress engine board cannot complete the local handoff.
        intent = json.loads(publication.read_text())
        intent.update(state="prepared", in_progress=True, scoreboard="<!--tauceti-scoreboard--> Initializing.")
        publication.write_text(json.dumps(intent))
        self.assertTrue(publish(self.source, publication))
        self.assertNotIn("published_head_sha", json.loads((store / "ledger.json").read_text())["prs"]["1"])

    def test_numeric_target_with_inherited_both_still_defaults_to_github(self):
        with patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both"}, clear=True):
            configure(SimpleNamespace(pr=["1"]))
            self.assertEqual(os.environ["TAUCETI_WORKER_FORGES"], "github")

    def test_other_origins_do_not_inherit_the_canonical_private_token(self):
        with patch.dict(os.environ, {"TAUCETI_FORGEJO_ORIGIN": "https://other.example.org"}, clear=True):
            configure(SimpleNamespace())
            self.assertNotIn("TAUCETI_FORGEJO_TOKEN_FILE", os.environ)

    def test_source_claim_failure_before_inference_does_not_consume_review_budget(self):
        from tauceti_worker.config import SourceUnavailable
        from tauceti_worker.source_execution import review

        worker = SimpleNamespace(cfg=self.cfg, gh=self.source, counters=Mock())
        claim = Mock()
        claim.acquire.return_value = 2
        with patch("tauceti_worker.source_claims.lease", return_value=claim):
            with self.assertRaises(SourceUnavailable):
                review(worker, None, Candidate(1, "a" * 40), SimpleNamespace(work_model="codex"), False)
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
        claim = Mock()
        claim.acquire.return_value = 0
        with (
            patch("tauceti_worker.source_claims.lease", return_value=claim),
            patch("tauceti_worker.agents.run_to_logfile", return_value=84),
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

    def test_failed_native_preparation_releases_generated_inputs(self):
        from tauceti_worker.source_review import main

        work = self.root / "failed-work"
        work.mkdir()

        def fail(*args):
            (work / "code").mkdir()
            (work / "code/large-source").write_text("generated")
            raise RuntimeError("preparation unavailable")

        argv = [
            "review",
            "--pr",
            "1",
            "--head",
            "a" * 40,
            "--work",
            str(work),
            "--store",
            str(self.root / "store"),
            "--reviewer",
            "codex",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("tauceti_worker.source_review.load_source", return_value=self.source),
            patch("tauceti_worker.source_review.prepare", side_effect=fail),
        ):
            self.assertEqual(main(), 83)
        self.assertFalse(work.exists())

    def test_paid_plan_is_retained_before_failed_review_inputs_are_released(self):
        from tauceti_worker.source_review import main

        work = self.root / "paid-work"
        work.mkdir()

        def fail(command, **kwargs):
            (work / "code").mkdir()
            (work / "finding.md").write_text("<!--tauceti-rubric:style--> Explain.")
            (work / "scoreboard.md").write_text("<!--tauceti-scoreboard--> Error.")
            (work / "plan.json").write_text(
                json.dumps(
                    {
                        "head_sha": "a" * 40,
                        "scoreboard_body": str(work / "scoreboard.md"),
                        "threads": [
                            {
                                "action": "upsert",
                                "rubric": "style",
                                "path": "Proof.lean",
                                "body": str(work / "finding.md"),
                            }
                        ],
                    }
                )
            )
            return SimpleNamespace(returncode=1)

        argv = [
            "review",
            "--pr",
            "1",
            "--head",
            "a" * 40,
            "--work",
            str(work),
            "--store",
            str(self.root / "store"),
            "--reviewer",
            "codex",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("tauceti_worker.source_review.load_source", return_value=self.source),
            patch("tauceti_worker.source_review.prepare", return_value=("b" * 40, "b" * 40)),
            patch("tauceti_worker.source_review.engine_command", return_value=["fixture"]),
            patch("tauceti_worker.source_review.subprocess.run", side_effect=fail),
        ):
            self.assertEqual(main(), 1)
        self.assertFalse((work / "code").exists())
        self.assertEqual(
            json.loads((work / "publication.json").read_text())["threads"][0]["body"],
            "<!--tauceti-rubric:style--> Explain.",
        )

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

    def test_native_preparation_uses_shared_mathlib_cache_and_default_roadmap(self):
        from tauceti_worker.source_review import engine_command

        self.source.issue_comments.return_value = []
        self.source.review_comments.return_value = []
        for project in (None, SimpleNamespace(raw={"repositories": {}}, tool_pin=lambda name: None)):
            work = self.root / ("without-config" if project is None else "without-role")
            (work / "code").mkdir(parents=True)
            (work / "code/lake-manifest.json").write_text(
                json.dumps({"packages": [{"name": "mathlib", "rev": "d" * 40}]})
            )
            with (
                patch("tauceti_tools.project.active", return_value=project),
                patch("tauceti_worker.source_review.git", return_value=b"") as git,
                patch("runner.cli.prepare_mathlib_source") as mathlib,
                patch.dict(os.environ, {"TAUCETI_REVIEW_MATHLIB_CACHE": str(self.root / "shared-mathlib")}),
            ):
                engine_command(self.source, 1, "a" * 40, ("b" * 40, "b" * 40), work, self.root / "store", "codex")
            self.assertEqual(mathlib.call_args.args[2], self.root / "shared-mathlib")
            self.assertTrue(
                any("https://github.com/TauCetiProject/TauCetiRoadmap.git" in call.args for call in git.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()

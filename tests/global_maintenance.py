"""Global maintenance precedes roadmap choice without expanding GitHub reads."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.config import ClaimCollision, NoProgress
from tauceti_worker.github_state import SurveyRows
from tauceti_worker.review_state import ReviewState
from tauceti_worker.survey import PRInfo
from tauceti_worker.work_units import RoundOpts, run_round

work = importlib.import_module("tauceti_worker.work_units")
HEAD = "a" * 40
branch_claimed_elsewhere = work._branch_claimed_elsewhere


def row(number, area, *, author="science", blocking=True, complete=True, contest=None, mergeable="MERGEABLE"):
    return {
        "pr": {
            "number": number,
            "author": {"login": author},
            "headRefOid": HEAD,
            "headRefName": "roadmap/work",
            "headRepositoryOwner": {"login": author},
            "headRepository": {"name": "TauCeti"},
            "labels": [{"name": "roadmap/" + area}],
            "mergeable": mergeable,
            "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
        },
        "review": {
            "meta": {
                "head_sha": HEAD,
                "runs": [{"verdict": "request_changes"}],
                "states": {"reuse": "blocking_request"},
            }
            if blocking
            else None,
            "markers": [],
            "rebase_heads": [],
            "contest": contest,
            "issue_complete": complete,
            "review_complete": complete,
        },
    }


class GlobalMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = SimpleNamespace(wid="test", sbcache=root / "sb", store_dir=root / "review", state=root)

        def forbidden(*args, **kwargs):
            self.fail("global discovery performed an upstream or per-PR history read")

        gh = SimpleNamespace(
            repo="org/repo",
            open_prs=forbidden,
            issue_comments=forbidden,
            review_comments=forbidden,
            fresh_claim_age=forbidden,
            rebase_requested=forbidden,
        )
        self.counter_values = {}
        self.w = SimpleNamespace(
            cfg=cfg,
            gh=gh,
            claims=SimpleNamespace(),
            rs=ReviewState(cfg, gh),
            counters=SimpleNamespace(read=lambda key: self.counter_values.get(key, 0)),
        )
        self.opts = RoundOpts(
            only=["rebase", "fix-ci", "fix", "review", "roadmap"],
            agent="claude",
            work_model="claude-opus-5-5",
            sandbox_host=True,
            dry_run=False,
        )
        for item in (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://state.test", "TAUCETI_ROADMAP_SKIP": ""}),
            patch.object(work, "roadmap_only", return_value=None),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch.object(work, "mirror_creds"),
            patch.object(work, "_branch_claimed_elsewhere", return_value=False),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_round(self, rows, dispatch=None):
        self.calls = []

        def perform(stage, _w, sv, candidate, _opts):
            self.calls.append((stage, candidate.pr, candidate.reason, sv.roadmap_only))
            return dispatch(stage, candidate) if dispatch else 0

        with (
            patch(
                "tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows(rows, {"science": 3}))
            ) as query,
            patch.object(work, "dispatch", side_effect=perform),
            patch.object(work, "roadmap_areas", return_value=["NewArea"]) as areas,
        ):
            result = run_round(self.w, self.opts)
        return result, query, areas

    def test_held_prs_do_not_consume_automatic_maintenance_or_reviews(self):
        for label in ("on-hold", "human", "hold", "keep", "do-not-close"):
            held = row(10, "Other", mergeable="CONFLICTING")
            held["pr"]["labels"].append({"name": label})
            self.run_round([held, row(20, "Other")])
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 20)])
            if label in {"keep", "do-not-close"}:
                continue  # These prohibit closure/recovery, not ordinary reviews.
            held = row(10, "Other", author="peer", blocking=False)
            held["pr"]["labels"].append({"name": label})
            self.run_round([held, row(20, "Other")])
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 20)])

    def test_old_pause_comments_cannot_strand_maintenance(self):
        for marker, expected in (
            ({"head": HEAD, "author": "science"}, [("fix", 10)]),
            ({"head": "b" * 40, "author": "science"}, [("fix", 10)]),
            ({"head": HEAD, "author": "peer"}, [("fix", 10)]),
        ):
            blocked = row(10, "Other")
            blocked["review"]["blocked"] = [marker]
            self.run_round([blocked, row(20, "Other")])
            self.assertEqual([(s, n) for s, n, *_ in self.calls], expected)

    def test_spent_owned_pr_is_selected_for_final_resolution(self):
        from tauceti_worker.survey import MAX_FIX_ATTEMPTS

        self.counter_values[f"fix-10-{HEAD[:12]}"] = MAX_FIX_ATTEMPTS
        flags = []
        self.run_round([row(10, "Other")], lambda stage, c: flags.append(c.final_resolution) or 0)
        self.assertEqual(self.calls[0][:2], ("fix", 10))
        self.assertEqual(flags, [True])

    def test_old_pause_comment_does_not_bypass_operator_hold(self):
        pr = row(10, "Other")
        pr["pr"]["labels"].append({"name": "hold"})
        pr["review"]["blocked"] = [{"head": HEAD, "author": "science"}]
        self.run_round([pr, row(20, "Other")])
        self.assertEqual(self.calls[0][:2], ("fix", 20))

    def test_last_permitted_fix_attempt_requires_resolution(self):
        from tauceti_worker.survey import MAX_FIX_ATTEMPTS

        self.counter_values[f"fix-10-{HEAD[:12]}"] = MAX_FIX_ATTEMPTS - 1
        flags = []
        self.run_round([row(10, "Other")], lambda stage, c: flags.append(c.final_resolution) or 0)
        self.assertEqual(flags, [True])

    def spent_fixture(self, **changes):
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        live = dict(state="OPEN", headRefOid=HEAD, isDraft=False, labels=[], author={"login": "science"})
        live.update(changes)
        self.w.gh.pr_view = lambda *_: dict(live)
        self.counter_values[f"fix-10-{HEAD[:12]}"] = 3
        self.w.rs = SimpleNamespace(
            gh_meta=lambda *a, **k: SimpleNamespace(
                data={"head_sha": HEAD, "states": {"reuse": "blocking_request"}}, provenance="fresh"
            ),
            ledger_blocking=lambda *a: True,
            newest_contest_reply=lambda *a, **k: None,
            bust=Mock(),
        )
        self.w.claims = SimpleNamespace(
            begin_branch_work=Mock(return_value=True), release=Mock(), stop_heartbeat=Mock()
        )
        return sv, work.Candidate(10, HEAD, final_resolution=True), live

    def test_unlimited_bump_is_not_redirected_to_closure_by_its_attempt_count(self):
        sv, c, _ = self.spent_fixture()
        c.final_resolution = False
        sv.open_prs[0] = SimpleNamespace(number=10, author="bot", automatic_bump=True)
        with (
            patch("tauceti_worker.github_outbox.pending_for", return_value=False),
            patch("tauceti_worker.repair_recovery.pending_for", return_value=False),
            patch.object(work, "_check_stage_read_quota"),
            patch.object(work, "_close_spent_pr") as close,
            patch.object(work, "_effective_authoring_profile", side_effect=RuntimeError("normal model path")),
        ):
            with self.assertRaisesRegex(RuntimeError, "normal model path"):
                work.dispatch("fix", self.w, sv, c, self.opts)
        close.assert_not_called()

    def test_exhausted_head_closes_without_another_model_call(self):
        sv, c, live = self.spent_fixture()

        def close(*args, **kwargs):
            live["state"] = "CLOSED"
            return SimpleNamespace(returncode=0)

        with patch.object(work, "gh_run", side_effect=close) as mutation:
            # gh_run is the existing quota-aware mutation transport.
            self.assertEqual(work._close_spent_pr(self.w, sv, c), 0)
        mutation.assert_called_once()
        self.w.claims.release.assert_called_once()
        self.assertEqual(self.counter_values[f"fix-10-{HEAD[:12]}"], 3)

    def test_exhausted_closure_preserves_live_author_head_and_holds(self):
        for changes in (
            {"labels": [{"name": "hold"}]},
            {"headRefOid": "b" * 40},
            {"author": {"login": "peer"}},
            {"isDraft": True},
        ):
            with self.subTest(changes=changes):
                sv, c, _ = self.spent_fixture(**changes)
                with patch.object(work, "gh_run") as mutation:
                    self.assertIsNone(work._close_spent_pr(self.w, sv, c))
                mutation.assert_not_called()

    def test_exhausted_closure_waits_for_pending_contest_and_fresh_evidence(self):
        sv, c, _ = self.spent_fixture()
        self.w.rs.newest_contest_reply = lambda *a, **k: {"id": 123}
        with patch.object(work, "gh_run") as mutation:
            self.assertIsNone(work._close_spent_pr(self.w, sv, c))
        mutation.assert_not_called()
        self.w.rs.newest_contest_reply = lambda *a, **k: None
        self.w.rs.gh_meta = lambda *a, **k: SimpleNamespace(data={}, provenance="fetch_failed")
        c.partial_history = True
        with patch.object(work, "gh_run") as mutation:
            self.assertIsNone(work._close_spent_pr(self.w, sv, c))
        mutation.assert_not_called()

    def test_other_roadmap_maintenance_and_claim_fallthrough(self):
        # Newest arrives first; a peer holds the oldest. Exhausted and contested
        # heads must not hide the remaining maintenance in a different area.
        rows = [
            row(30, "Algebra"),
            row(20, "PDE"),
            row(10, "Topology"),
            row(15, "Geometry", contest={"id": 1, "rubric": "reuse"}),
        ]
        self.counter_values[f"fix-10-{HEAD[:12]}"] = 3
        _, query, areas = self.run_round(rows, lambda stage, c: None if c.pr in (10, 20) else 0)
        self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 10), ("fix", 20), ("fix", 30)])
        self.assertEqual(set(query.call_args.kwargs["stages"]), set(self.opts.only) - {"roadmap"})
        self.assertEqual(query.call_args.args[1], "auto")
        areas.assert_not_called()

    def test_all_claimed_without_authoring_is_a_collision_not_failure(self):
        self.opts.only = ["fix"]
        with patch.object(work, "_branch_claimed_elsewhere", return_value=True):
            with self.assertRaises(ClaimCollision):
                self.run_round([row(10, "Other")])
        self.assertEqual(self.calls, [])

    def test_authoring_limit_does_not_turn_maintenance_collisions_into_long_backoff(self):
        with (
            patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="1"),
            patch.object(work, "_branch_claimed_elsewhere", return_value=True),
        ):
            with self.assertRaisesRegex(ClaimCollision, "maintenance is claimed"):
                self.run_round([row(10, "Other")])
        self.assertEqual(self.calls, [])

    def test_git_claim_precheck_handles_owner_expiry_and_failures(self):
        leases = [
            ({"owner": "peer", "expires_at": 200}, True),
            ({"owner": "test", "expires_at": 200}, False),
            ({"owner": "peer", "expires_at": 99}, False),
            ({"owner": "peer", "expires_at": "200"}, False),
            (None, False),
        ]
        import json
        import subprocess

        with (
            patch.object(work, "claims_repo", return_value="org/claims"),
            patch.object(work.time, "time", return_value=100),
        ):
            for lease, expected in leases:
                with patch.object(
                    work.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(lease))
                ) as git:
                    self.assertEqual(branch_claimed_elsewhere(self.w, 10), expected)
                    self.assertEqual(git.call_args.args[0][1:], ["read", "branch/10"])
                    self.assertEqual(git.call_args.kwargs["env"]["CLAIM_REPO"], "org/claims")
            for error in (OSError("offline"), subprocess.TimeoutExpired("git", 15)):
                with patch.object(work.subprocess, "run", side_effect=error):
                    self.assertFalse(branch_claimed_elsewhere(self.w, 10))

    def test_known_branch_claim_skips_dispatch_and_fresh_reads(self):
        with patch.object(work, "_branch_claimed_elsewhere", side_effect=lambda _w, pr: pr == 10):
            self.run_round([row(10, "Other"), row(20, "Topology")])
        self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 20)])

    def test_lost_claim_race_refunds_every_retry_counter(self):
        self.w.claims = SimpleNamespace(begin_branch_work=lambda *args: False)
        self.w.counters.write = lambda key, value: self.counter_values.update({key: value})
        self.counter_values.update({"per-head": 1, "per-pr": 2})
        sv = work.Survey(worker_id="test")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        result = work._do_fixlike(
            self.w,
            sv,
            work.Candidate(10, HEAD),
            self.opts,
            False,
            prompt_file="fix.md",
            label="fix-ci",
            charged=("per-head", "per-pr"),
        )
        self.assertIsNone(result)
        self.assertEqual(self.counter_values, {"per-head": 0, "per-pr": 1})

    def test_ci_repair_reselects_when_checkout_has_a_new_head(self):
        import subprocess

        self.w.cfg.checkout = Path(self.tmp.name) / "checkout"
        self.w.cfg.logdir = Path(self.tmp.name) / "logs"
        self.w.claims = SimpleNamespace(
            held=("branch/10", "org/claims"),
            begin_branch_work=lambda *args: True,
            stop_heartbeat=Mock(),
            release=Mock(),
        )
        self.w.counters.write = lambda key, value: self.counter_values.update({key: value})
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        for code, checked in ((0, "b" * 40), (1, "")):
            with self.subTest(code=code, checked=checked):
                self.counter_values.update({"per-head": 1, "per-pr": 2})
                self.w.claims.stop_heartbeat.reset_mock()
                self.w.claims.release.reset_mock()

                def checkout(command, code=code, checked=checked, **kwargs):
                    if command[:3] == ["gh", "pr", "checkout"]:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    if command == ["git", "-C", str(self.w.cfg.checkout), "rev-parse", "HEAD"]:
                        return subprocess.CompletedProcess(command, code, checked, "")
                    self.fail(f"stale repair continued preparing a model run: {command}")

                with (
                    patch.object(work, "prepare_checkout", return_value=True),
                    patch.object(work.subprocess, "run", side_effect=checkout) as commands,
                    patch.object(work, "run_agent_host") as model,
                ):
                    result = work._do_fixlike(
                        self.w,
                        sv,
                        work.Candidate(10, HEAD),
                        self.opts,
                        False,
                        prompt_file="fix-ci.md",
                        label="fix-ci",
                        charged=("per-head", "per-pr"),
                    )
                self.assertIsNone(result)
                model.assert_not_called()
                self.assertEqual(commands.call_count, 2)
                self.w.claims.stop_heartbeat.assert_called_once_with()
                self.w.claims.release.assert_called_once_with()
                self.assertEqual(self.counter_values, {"per-head": 0, "per-pr": 1})

    def test_global_review_before_area_choice(self):
        _, _, areas = self.run_round([row(40, "Other", author="peer", blocking=False)])
        self.assertEqual(self.calls[0][:2], ("review", 40))
        areas.assert_not_called()

    def test_contest_reaction_is_checked_only_at_dispatch(self):
        _, _, areas = self.run_round([row(40, "Other", author="peer", contest={"id": 123, "rubric": "reuse"})])
        self.assertEqual(self.calls[0][:2], ("review", 40))
        areas.assert_not_called()

    def test_conflicting_pr_needs_no_comment_fetch(self):
        _, _, areas = self.run_round([row(5, "Other", complete=False, mergeable="CONFLICTING")])
        self.assertEqual(self.calls[0][:2], ("rebase", 5))
        areas.assert_not_called()

    def test_new_authoring_selects_area_last(self):
        _, query, areas = self.run_round([])
        self.assertEqual(self.calls, [("roadmap", 0, "NewArea", "NewArea")])
        self.assertEqual(set(query.call_args.kwargs["stages"]), set(self.opts.only) - {"roadmap"})
        areas.assert_called_once_with(self.w.gh)

    def test_incomplete_cache_dispatches_review_without_global_history_reads(self):
        self.run_round([row(10, "Other", complete=False)])
        self.assertEqual(self.calls[0][:2], ("review", 10))

    def test_incomplete_cache_dispatches_author_repair(self):
        pr = row(10, "Other", complete=False)
        pr["pr"]["labels"].append({"name": "awaiting-author"})
        self.run_round([pr])
        self.assertEqual(self.calls[0][:2], ("fix", 10))

    def test_partial_history_never_prevents_authoring_when_work_is_claimed(self):
        self.run_round([row(10, "Other", complete=False)], lambda stage, c: 0 if stage == "roadmap" else None)
        self.assertEqual([s for s, *_ in self.calls], ["review", "roadmap"])

    def test_partial_history_preserves_authoring_backpressure_but_not_maintenance(self):
        with patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="1"):
            self.run_round([row(10, "Other", complete=False)])
        self.assertEqual(self.calls[0][:2], ("review", 10))

    def test_partial_history_does_not_bypass_authoring_limit(self):
        with patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="1"):
            with self.assertRaisesRegex(NoProgress, "backpressure"):
                self.run_round([row(10, "Other", complete=False)], lambda stage, c: None)
        self.assertEqual([s for s, *_ in self.calls], ["review"])

    def test_previous_reviewer_preference_does_not_idle_capped_workers(self):
        from tauceti_worker.survey import Candidate

        deferred = Candidate(10, HEAD, "review preferred elsewhere")
        with (
            patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="1"),
            patch.object(work, "prioritize_review_candidates", return_value=([], [deferred])),
        ):
            self.run_round([row(10, "Other", author="peer", blocking=False)])
        self.assertEqual(self.calls[0][:2], ("review", 10))

    def test_partial_history_can_launch_without_fetching_history_before_agent(self):
        from tauceti_worker.survey import Candidate, Survey

        c = Candidate(10, HEAD, partial_history=True)
        self.w.gh.pr_view = lambda *a: dict(headRefOid=HEAD, isDraft=False, state="OPEN", labels=[])
        with patch.object(self.w.rs, "gh_meta", side_effect=AssertionError("pre-agent history read")):
            self.assertTrue(work._still_actionable("review", self.w, Survey("test"), c))
            self.assertTrue(work._still_actionable("fix", self.w, Survey("test"), c))

    def test_cold_service_outage_uses_only_shared_inventory_not_per_pr_histories(self):
        with (
            patch("tauceti_worker.github_state.read_survey", return_value=(False, None)),
            patch.object(self.w.gh, "open_prs", return_value=[row(10, "Other")["pr"]]) as inventory,
            patch.object(work, "dispatch", return_value=0) as dispatch,
        ):
            run_round(self.w, self.opts)
        inventory.assert_called_once()
        self.assertEqual(dispatch.call_args.args[:1], ("review",))
        self.assertEqual(dispatch.call_args.args[3].pr, 10)

    def test_explicit_roadmap_is_preserved(self):
        with patch.object(work, "roadmap_only", return_value="Topology"):
            _, query, areas = self.run_round([row(10, "Other"), row(20, "Topology")])
        self.assertEqual(self.calls[0][:2], ("fix", 20))
        self.assertEqual(query.call_args.args[1], "Topology")
        self.assertIn("fix", query.call_args.kwargs["stages"])
        areas.assert_not_called()

    def test_explicit_pr_overrides_roadmap_without_authoring(self):
        self.opts.prs = (10,)
        with patch.object(work, "roadmap_only", return_value="Topology"):
            _, query, areas = self.run_round([row(10, "Other"), row(20, "Topology")])
        self.assertEqual(self.calls[0][:2], ("fix", 10))
        self.assertEqual(query.call_args.kwargs["numbers"], (10,))
        areas.assert_not_called()

    def test_skipped_roadmap_remains_excluded(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": "Topology"}):
            self.run_round([row(10, "Topology"), row(20, "Other")])
        self.assertEqual(self.calls[0][:2], ("fix", 20))

    def test_missing_cache_predictor_offers_review(self):
        survey = importlib.import_module("tauceti_worker.survey")
        with patch("tauceti_worker.github_state.read_survey", return_value=(True, [row(10, "Other", complete=False)])):
            result = survey.survey(
                self.w.cfg,
                self.w.gh,
                self.w.rs,
                self.w.counters,
                stages=["fix", "review"],
                selected_roadmap="auto",
                cached_only=True,
            )
        self.assertEqual(result.discovery_gaps, 1)
        self.assertEqual(result.discovery_gap_prs, [10])
        self.assertEqual(result.next_auto_stage, "review")

    def test_disabled_maintenance_remains_disabled(self):
        self.opts.only = ["roadmap"]
        self.run_round([row(10, "Other", complete=False)])
        self.assertEqual(self.calls[0][:2], ("roadmap", 0))


if __name__ == "__main__":
    unittest.main()

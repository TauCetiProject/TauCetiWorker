"""Global maintenance precedes roadmap choice without expanding GitHub reads."""

import contextlib
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
            counters=SimpleNamespace(
                read=lambda key: self.counter_values.get(key, 0), write=lambda k, v: self.counter_values.update({k: v})
            ),
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
            # The roadmap repository defines no umbrella (tests/roadmap_umbrella.py covers them).
            patch("tauceti_worker.umbrellas.read_umbrella", return_value=None),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch.object(work, "mirror_creds"),
            patch.object(work, "_branch_claimed_elsewhere", return_value=False),
            patch.object(work, "spread_candidates", side_effect=lambda cs: sorted(cs, key=lambda c: c.pr)),
            patch.object(work, "age_spread", side_effect=lambda cs: sorted(cs, key=lambda c: c.pr)),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_round(self, rows, dispatch=None, *, author_count=3):
        self.calls = []

        def perform(stage, _w, sv, candidate, _opts):
            self.calls.append((stage, candidate.pr, candidate.reason, sv.roadmap_only))
            return dispatch(stage, candidate) if dispatch else 0

        with (
            patch(
                "tauceti_worker.github_state.read_survey",
                return_value=(True, SurveyRows(rows, {"science": author_count})),
            ) as query,
            patch.object(work, "dispatch", side_effect=perform),
            patch.object(work, "roadmap_areas", return_value=["NewArea"]) as areas,
        ):
            result = run_round(self.w, self.opts)
        return result, query, areas

    def test_a_peer_claim_found_once_is_skipped_by_every_worker_before_fresh_reads(self):
        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        self.w.claims.collisions = 0

        def collide(stage, candidate):
            if candidate.pr == 10:
                self.w.claims.collisions += 1  # dispatch read the PR, then lost the lease
                return None
            return 0

        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            self.run_round([row(10, "Other"), row(20, "Other")], collide)
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 10), ("fix", 20)])
            # The next round on any worker skips #10 without dispatching (no fresh PR reads).
            self.run_round([row(10, "Other"), row(20, "Other")], collide)
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 20)])
        # Without the configured shared directory nothing is recorded or skipped.
        self.run_round([row(10, "Other"), row(20, "Other")], collide)
        self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 10), ("fix", 20)])

    def test_an_unavailable_review_source_selects_other_work_and_spares_peers(self):
        from tauceti_worker.config import ReviewSourceUnavailable

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        self.w.claims.collisions = 0

        def unavailable(stage, candidate):
            if candidate.pr == 10:
                raise ReviewSourceUnavailable("review #10: source preparation unavailable before inference")
            return 0

        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            result, *_ = self.run_round([row(10, "Other"), row(20, "Other")], unavailable)
            self.assertEqual(result, 0)
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 10), ("fix", 20)])
            # Any worker on the host now skips #10 at this head without dispatching it.
            self.run_round([row(10, "Other"), row(20, "Other")], unavailable)
            self.assertEqual([(s, n) for s, n, *_ in self.calls], [("fix", 20)])

    def test_repeated_unavailable_sources_in_one_round_back_off(self):
        from tauceti_worker.config import ReviewSourceUnavailable

        def unavailable(stage, candidate):
            raise ReviewSourceUnavailable(f"review #{candidate.pr}: source preparation unavailable")

        rows = [row(n, "Other") for n in (10, 20, 30, 40)]
        with self.assertRaises(ReviewSourceUnavailable):
            self.run_round(rows, unavailable)
        self.assertEqual([n for _, n, *_ in self.calls], [10, 20, 30])

    def test_the_unavailable_source_cutoff_spans_prepared_tier_rounds(self):
        import dataclasses

        from tauceti_worker.config import ReviewSourceUnavailable
        from tauceti_worker.survey import Candidate, Survey

        def tier(numbers):
            prepared = Survey(worker_id="test", roadmap_only="auto")
            prepared._round_scope = (True, False)
            prepared.needs_fix.actionable = [Candidate(n, HEAD) for n in numbers]
            return prepared

        def unavailable(stage, _w, _sv, candidate, _opts):
            raise ReviewSourceUnavailable(f"review #{candidate.pr}: source preparation unavailable")

        self.opts.only = ["fix"]
        with patch.object(work, "dispatch", side_effect=unavailable):
            first = dataclasses.replace(self.opts)
            with self.assertRaises(ClaimCollision):
                run_round(self.w, first, prepared_survey=tier([10, 20]))
            second = dataclasses.replace(self.opts)
            with self.assertRaises(ReviewSourceUnavailable):
                run_round(self.w, second, prepared_survey=tier([30, 40]))
        self.assertEqual([n for _, n in self.opts.unavailable_sources], [10, 20, 30])

    def test_the_unfocused_fallback_keeps_the_rounds_unavailable_source_count(self):
        from tauceti_worker.config import ReviewSourceUnavailable

        def unavailable(stage, _w, _sv, candidate, _opts):
            raise ReviewSourceUnavailable(f"review #{candidate.pr}: source preparation unavailable")

        self.opts.unavailable_sources[:] = [("review", 1), ("review", 2)]
        with (
            patch(
                "tauceti_worker.github_state.read_survey",
                return_value=(True, SurveyRows([row(10, "Other"), row(20, "Other")], {"science": 3})),
            ),
            patch.object(work, "dispatch", side_effect=unavailable),
            patch.object(work, "roadmap_areas", return_value=["NewArea"]),
        ):
            with self.assertRaises(ReviewSourceUnavailable):
                run_round(self.w, self.opts, use_fleet_focus=False)
        self.assertEqual([n for _, n in self.opts.unavailable_sources], [1, 2, 10])

    def test_unavailable_source_hints_are_short_and_separate_from_claims(self):
        from tauceti_worker import peer_claims

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            peer_claims.note("forgejo:one/project", "review", 7, HEAD, now=1000, unavailable=True)
            self.assertTrue(peer_claims.hinted("forgejo:one/project", "review", 7, HEAD, now=1001, unavailable=True))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "review", 7, HEAD, now=1001))
            self.assertFalse(
                peer_claims.hinted("forgejo:one/project", "review", 7, "b" * 40, now=1001, unavailable=True)
            )
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "fix", 7, HEAD, now=1001, unavailable=True))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "review", 7, HEAD, now=1301, unavailable=True))

    def test_diff_guard_records_are_per_head_and_outlast_claim_hints(self):
        from tauceti_worker import peer_claims

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            self.assertFalse(peer_claims.diff_guarded("forgejo:one/project", 7, HEAD, now=1000))
            peer_claims.note_diff_guard("forgejo:one/project", 7, HEAD, now=1000)
            self.assertTrue(peer_claims.diff_guarded("forgejo:one/project", 7, HEAD, now=1000 + 3600))
            self.assertFalse(peer_claims.diff_guarded("forgejo:one/project", 7, "b" * 40, now=1001), "a push")
            self.assertFalse(peer_claims.diff_guarded("github:one/project", 7, HEAD, now=1001))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "review", 7, HEAD, now=1001))
            self.assertFalse(
                peer_claims.diff_guarded("forgejo:one/project", 7, HEAD, now=1000 + peer_claims.GUARD_TTL_S + 1)
            )
        self.assertFalse(peer_claims.diff_guarded("forgejo:one/project", 7, HEAD), "no shared directory")

    def test_a_diff_guarded_review_head_is_skipped_as_no_work(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.survey import Candidate, Survey

        prepared = Survey(worker_id="test", roadmap_only="auto")
        prepared._round_scope = (True, False)
        prepared.reviewable.actionable = [Candidate(10, HEAD)]
        self.opts.only = ["review"]
        with (
            patch.object(peer_claims, "diff_guarded", return_value=True) as guarded,
            patch.object(work, "dispatch") as dispatch,
        ):
            with self.assertRaises(work.NoEligibleWork):
                run_round(self.w, self.opts, prepared_survey=prepared)
        guarded.assert_called_once()
        self.assertEqual(guarded.call_args.args[1:], (10, HEAD))
        dispatch.assert_not_called()
        # A green build at the same head lifts the engine's guards: the review is offered again.
        prepared.open_prs = [
            PRInfo(10, HEAD, "roadmap/work", "science", "TauCeti", False, "MERGEABLE", "science", True, False)
        ]
        prepared.reviewable.actionable = [Candidate(10, HEAD)]
        with (
            patch.object(peer_claims, "diff_guarded", return_value=True),
            patch.object(work, "dispatch", return_value=0) as dispatch,
        ):
            run_round(self.w, self.opts, prepared_survey=prepared)
        dispatch.assert_called_once()

    def test_unchanged_review_records_are_per_head_scope_and_expire(self):
        from tauceti_worker import peer_claims

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            self.assertFalse(peer_claims.unchanged_review_noted("github:one/project", 7, HEAD, now=1000))
            peer_claims.note_unchanged_review("github:one/project", 7, HEAD, now=1000)
            self.assertTrue(peer_claims.unchanged_review_noted("github:one/project", 7, HEAD, now=1000 + 3600))
            self.assertFalse(peer_claims.unchanged_review_noted("github:one/project", 7, "b" * 40, now=1001), "a push")
            self.assertFalse(peer_claims.unchanged_review_noted("forgejo:one/project", 7, HEAD, now=1001))
            self.assertFalse(peer_claims.diff_guarded("github:one/project", 7, HEAD, now=1001))
            self.assertFalse(
                peer_claims.unchanged_review_noted(
                    "github:one/project", 7, HEAD, now=1000 + peer_claims.UNCHANGED_REVIEW_TTL_S + 1
                )
            )
        self.assertFalse(peer_claims.unchanged_review_noted("github:one/project", 7, HEAD), "no shared directory")

    def test_a_review_head_found_unchanged_by_a_peer_is_skipped_as_no_work(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.survey import Candidate, Survey

        prepared = Survey(worker_id="test", roadmap_only="auto")
        prepared._round_scope = (True, False)
        prepared.reviewable.actionable = [Candidate(10, HEAD, partial_history=True)]
        self.opts.only = ["review"]
        with (
            patch.object(peer_claims, "unchanged_review_noted", return_value=True) as noted,
            patch.object(work, "dispatch") as dispatch,
        ):
            with self.assertRaises(work.NoEligibleWork):
                run_round(self.w, self.opts, prepared_survey=prepared)
        self.assertEqual(noted.call_args.args[1:], (10, HEAD))
        dispatch.assert_not_called()
        # So is a candidate from complete history that this worker's own ledger has not seen reviewed.
        prepared.reviewable.actionable = [Candidate(10, HEAD)]
        with (
            patch.object(peer_claims, "unchanged_review_noted", return_value=True),
            patch.object(work, "dispatch") as dispatch,
        ):
            with self.assertRaises(work.NoEligibleWork):
                run_round(self.w, self.opts, prepared_survey=prepared)
        dispatch.assert_not_called()
        # A new contest reply at the same head is offered.
        prepared.reviewable.actionable = [Candidate(10, HEAD, contest="reuse", contest_reply_id=999)]
        with (
            patch.object(peer_claims, "unchanged_review_noted", return_value=True),
            patch.object(work, "dispatch", return_value=0) as dispatch,
        ):
            run_round(self.w, self.opts, prepared_survey=prepared)
        dispatch.assert_called_once()

    def test_fixed_heads_are_recorded_per_pr_and_scope_and_expire(self):
        from tauceti_worker import peer_claims

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            self.assertEqual(peer_claims.fixed_heads("forgejo:one/project", 7), set())
            peer_claims.note_fixed_head("forgejo:one/project", 7, HEAD)
            peer_claims.note_fixed_head("forgejo:one/project", 7, HEAD)
            peer_claims.note_fixed_head("forgejo:one/project", 7, "b" * 40)
            self.assertEqual(peer_claims.fixed_heads("forgejo:one/project", 7), {HEAD, "b" * 40})
            self.assertEqual(peer_claims.fixed_heads("forgejo:one/project", 8), set())
            self.assertEqual(peer_claims.fixed_heads("github:one/project", 7), set())
            later = os.path.getmtime(peer_claims._fixed_dir("forgejo:one/project", 7) / HEAD)
            self.assertEqual(
                peer_claims.fixed_heads("forgejo:one/project", 7, now=later + peer_claims.FIXED_HEAD_TTL_S + 1), set()
            )
        self.assertEqual(peer_claims.fixed_heads("forgejo:one/project", 7), set(), "no shared directory")

    def test_the_fix_cap_counts_distinct_heads_per_pr(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.constants import MAX_FIX_ROUNDS_WITHOUT_APPROVAL
        from tauceti_worker.survey import Candidate

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            scope = work._claim_scope(self.w)
            heads = [f"{n:040x}" for n in range(MAX_FIX_ROUNDS_WITHOUT_APPROVAL + 1)]
            for head in heads[:-1]:
                self.assertFalse(work._fix_cap_reached(self.w, Candidate(10, head)))
                self.assertFalse(work._fix_cap_reached(self.w, Candidate(10, head)), "a retry at one head")
            self.assertEqual(len(peer_claims.fixed_heads(scope, 10)), MAX_FIX_ROUNDS_WITHOUT_APPROVAL)
            self.assertFalse(work._fix_cap_reached(self.w, Candidate(11, heads[-1])), "per PR")
            self.assertFalse(work._fix_cap_spent(self.w, Candidate(10, heads[0])), "a recorded head is not counted")
            self.assertTrue(work._fix_cap_spent(self.w, Candidate(10, heads[-1])))
            self.assertTrue(work._fix_cap_reached(self.w, Candidate(10, heads[-1])))
            self.assertNotIn(heads[-1], peer_claims.fixed_heads(scope, 10), "the capped head is not recorded")

    def test_the_fix_cap_closes_a_maintained_pr(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.constants import MAX_FIX_ROUNDS_WITHOUT_APPROVAL

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        sv, c, live = self.spent_fixture()
        self.counter_values.clear()  # the per-head attempt budget is not spent

        def close(argv, **kwargs):
            live["state"] = "CLOSED"
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            scope = work._claim_scope(self.w)
            for n in range(MAX_FIX_ROUNDS_WITHOUT_APPROVAL):
                peer_claims.note_fixed_head(scope, 10, f"{n:040x}")
            with patch.object(work, "gh_run", side_effect=close) as mutation:
                self.assertEqual(work._close_capped_pr(self.w, sv, c), 0)
            argv = mutation.call_args.args[0]
            self.assertEqual(argv[:4], ["gh", "pr", "close", "10"])
            self.assertEqual(argv[argv.index("--repo") + 1], self.w.gh.repo, "the selected source's repository")
            self.assertIn(f"after {MAX_FIX_ROUNDS_WITHOUT_APPROVAL} fix rounds", argv[argv.index("--comment") + 1])
            self.assertEqual(peer_claims.fixed_heads(scope, 10), set(), "a reopened PR starts a fresh count")
            self.w.claims.release.assert_called_once()

    def test_the_fix_cap_never_closes_another_authors_pr_or_a_kept_one(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.constants import MAX_FIX_ROUNDS_WITHOUT_APPROVAL

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            scope = work._claim_scope(self.w)
            for n in range(MAX_FIX_ROUNDS_WITHOUT_APPROVAL):
                peer_claims.note_fixed_head(scope, 10, f"{n:040x}")
            for changes in ({"labels": [{"name": "keep"}]}, {"author": {"login": "peer"}}):
                with self.subTest(changes=changes):
                    sv, c, _ = self.spent_fixture(**changes)
                    with patch.object(work, "gh_run") as mutation:
                        self.assertIsNone(work._close_capped_pr(self.w, sv, c))
                    mutation.assert_not_called()
                    self.assertEqual(len(peer_claims.fixed_heads(scope, 10)), MAX_FIX_ROUNDS_WITHOUT_APPROVAL)
            sv, c, _ = self.spent_fixture()
            sv.me_login = "someone-else"  # a contributor's PR that the fleet fixes
            self.w.claims.begin_branch_work = Mock(side_effect=AssertionError("no claim for a foreign PR"))
            with patch.object(work, "gh_run") as mutation:
                self.assertIsNone(work._close_capped_pr(self.w, sv, c))
            mutation.assert_not_called()

    def test_a_foreign_pr_at_the_fix_cap_is_skipped_before_any_read(self):
        from tauceti_worker import peer_claims
        from tauceti_worker.constants import MAX_FIX_ROUNDS_WITHOUT_APPROVAL

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            scope = work._claim_scope(self.w)
            for n in range(MAX_FIX_ROUNDS_WITHOUT_APPROVAL):
                peer_claims.note_fixed_head(scope, 10, f"{n:040x}")
            # PR 10 stands for a contributor's PR that the fleet fixes but does not maintain.
            with patch.object(work, "owns_pr", side_effect=lambda sv, p, **k: p.number != 10):
                self.run_round([row(10, "Other"), row(20, "Other")])
            self.assertEqual([call[:2] for call in self.calls], [("fix", 20)])
            self.calls.clear()
            self.run_round([row(10, "Other")])
            self.assertEqual([call[:2] for call in self.calls], [("fix", 10)], "a maintained PR goes on to close")

    def test_the_fix_cap_is_counted_only_after_the_live_recheck_confirms_the_fix(self):
        import inspect

        source = inspect.getsource(work.dispatch)
        recheck = source.index("actionable = _still_actionable(stage, w, sv, c, explicit=explicit)")
        cap = source.index("_fix_cap_reached(w, c)")
        self.assertLess(recheck, cap)
        guard = source[source.rindex('stage == "fix"', 0, cap) : cap]
        for condition in ("actionable is True", "not c.partial_history", "c.pr not in bump_prs(sv)"):
            self.assertIn(condition, guard)

    def test_peer_claim_hints_expire_and_are_scoped(self):
        from tauceti_worker import peer_claims

        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        with patch.dict(os.environ, TAUCETI_GITHUB_SHARED_DIR=shared.name):
            peer_claims.note("forgejo:one/project", "rebase", 7, HEAD, now=1000)
            self.assertTrue(peer_claims.hinted("forgejo:one/project", "fix", 7, "b" * 40, now=1001))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "review", 7, HEAD, now=1001))
            self.assertFalse(peer_claims.hinted("github:one/project", "rebase", 7, HEAD, now=1001))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "rebase", 7, HEAD, now=1000 + 601))
            self.assertFalse(peer_claims.hinted("forgejo:one/project", "rebase", 7, HEAD, now=0))

    def test_prepared_source_survey_keeps_global_peer_claim_checks(self):
        from tauceti_worker.survey import Candidate, Survey

        prepared = Survey(worker_id="test", roadmap_only="auto")
        prepared._round_scope = (True, False)
        prepared.needs_fix.actionable = [Candidate(10, HEAD)]
        self.opts.only = ["fix"]
        with (
            patch.object(work, "_branch_claimed_elsewhere", return_value=True) as hint,
            patch.object(work, "dispatch") as dispatch,
        ):
            with self.assertRaises(ClaimCollision):
                run_round(self.w, self.opts, prepared_survey=prepared)
        hint.assert_called_once_with(self.w, 10)
        dispatch.assert_not_called()

    def test_prepared_fleet_focus_falls_back_to_both_sources(self):
        from tauceti_worker.survey import Survey

        prepared = Survey(worker_id="test", roadmap_only="Algebra")
        prepared._round_scope = (False, True)
        prepared.roadmap_backpressure = True
        self.opts.only = ["roadmap"]
        with (
            patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both", "TAUCETI_NEW_PR_FORGE": "github"}),
            patch("tauceti_worker.multi_source.run_round", return_value=0) as global_round,
        ):
            self.assertEqual(run_round(self.w, self.opts, prepared_survey=prepared), 0)
        self.assertFalse(global_round.call_args.kwargs["use_fleet_focus"])

    def test_priority_labelled_reviews_come_before_all_other_pr_work(self):
        from tauceti_worker.config import NoProgress as Declined

        def labelled(item, *names):
            item["pr"]["labels"].extend({"name": name} for name in names)
            return item

        rows = [
            row(10, "Other"),  # our fix
            row(20, "Other", author="peer", blocking=False),  # a first review
            labelled(row(30, "Other", author="peer", blocking=False), "GRH"),  # a priority review
            row(40, "Other", blocking=False, mergeable="CONFLICTING"),  # our conflict rebase
            labelled(row(50, "Other"), "grh"),  # our fix on a priority PR: not raised
        ]
        self.opts.only = ["rebase", "fix", "review"]

        def order(environment):
            with patch.dict(os.environ, environment):
                if not environment:
                    os.environ.pop("TAUCETI_PRIORITY_LABELS", None)
                with self.assertRaises(Declined):
                    self.run_round(rows, lambda stage, candidate: None)
            return [(s, n) for s, n, *_ in self.calls]

        ordinary = order({})
        # Reviews within one tier are drawn in a random order.
        self.assertEqual(ordinary[:3], [("rebase", 40), ("fix", 10), ("fix", 50)])
        self.assertEqual(sorted(ordinary[3:]), [("review", 20), ("review", 30), ("review", 40)])
        # The labelled PR's review leads; its fix, and every other unit, keeps its place.
        prioritized = order({"TAUCETI_PRIORITY_LABELS": "wip, grh"})
        self.assertEqual(prioritized[:4], [("review", 30), ("rebase", 40), ("fix", 10), ("fix", 50)])
        self.assertEqual(sorted(prioritized[4:]), [("review", 20), ("review", 40)])
        # An empty setting is off.
        self.assertEqual(order({"TAUCETI_PRIORITY_LABELS": " "})[:3], ordinary[:3])
        # Hold labels still stop a priority PR's review.
        labelled(rows[2], "on-hold")
        self.assertNotIn(("review", 30), order({"TAUCETI_PRIORITY_LABELS": "grh"}))

    def test_priority_reviews_skip_reviewer_affinity_and_review_throttles(self):
        import time

        from tauceti_worker.survey import Candidate, Survey

        sv = Survey(worker_id="test")
        sv.reviewable.actionable = [
            Candidate(1, HEAD, ready_at=int(time.time()), priority=True),
            Candidate(2, HEAD, ready_at=int(time.time())),
        ]
        # The throttles hold back a fresh, short queue, but never a priority review.
        work.throttle_review(sv, SimpleNamespace(review_min_queue=5, review_min_age=60))
        self.assertEqual([c.pr for c in sv.reviewable.actionable], [1])

        # A peer reviewer's first-refusal window defers an ordinary re-review, never a priority one.
        prepared = Survey(worker_id="test", roadmap_only="auto")
        prepared._round_scope = (True, False)
        recent = int(time.time()) - 60
        prepared.reviewable.actionable = [
            Candidate(1, HEAD, ready_at=recent, preferred_reviewer="peer", rereview=True),
            Candidate(2, HEAD, ready_at=recent, preferred_reviewer="peer", priority=True),
        ]
        self.opts.only = ["review"]
        offered = []

        def perform(stage, _w, _sv, candidate, _opts):
            offered.append(candidate.pr)

        with (
            patch.object(work, "dispatch", side_effect=perform),
            patch.object(work, "review_publisher", return_value="science"),
            self.assertRaises(NoProgress),
        ):
            run_round(self.w, self.opts, prepared_survey=prepared)
        self.assertEqual(offered, [2])

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

    def test_current_review_findings_can_be_fixed_without_green_ci(self):
        self.opts.only = ["fix", "review", "roadmap"]
        for state in (None, "PENDING", "FAILURE"):
            with self.subTest(build=state):
                pr = row(10, "Other")
                pr["pr"]["statusCheckRollup"] = [] if state is None else [{"context": "build", "state": state}]
                self.run_round([pr])
                self.assertEqual(self.calls[0][:2], ("fix", 10))
                # Moving the head invalidates these findings even when CI is
                # unavailable; removing the CI prerequisite must not revive them.
                pr["review"]["meta"]["head_sha"] = "b" * 40
                self.run_round([pr])
                self.assertEqual(self.calls[0][:2], ("roadmap", 0))
                # An old awaiting-author label with incomplete history must
                # not substitute for actual findings at the new head.
                pr["pr"]["labels"].append({"name": "awaiting-author"})
                pr["review"]["issue_complete"] = False
                pr["review"]["review_complete"] = False
                self.run_round([pr])
                self.assertEqual(self.calls[0][:2], ("roadmap", 0))
        self.opts.only = ["fix-ci", "fix", "review", "roadmap"]
        pr = row(10, "Other")
        pr["pr"]["statusCheckRollup"] = [{"context": "build", "state": "FAILURE"}]
        self.run_round([pr])
        self.assertEqual(self.calls[0][:2], ("fix-ci", 10))

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

    def test_review_errors_back_off_then_cap(self):
        import time

        survey_mod = importlib.import_module("tauceti_worker.survey")
        from tauceti_worker.constants import MAX_REVIEW_ERRORS, REVIEW_ERROR_BACKOFF_S

        p = PRInfo.from_json(row(10, "Other")["pr"])
        counters = self.w.counters
        now = time.time()
        self.assertEqual(survey_mod._review_error_state(counters, p, 0, now), "ready")
        self.counter_values[survey_mod.review_error_key(10)] = int(now)
        self.assertEqual(survey_mod._review_error_state(counters, p, 1, now), "backoff")
        self.assertEqual(survey_mod._review_error_state(counters, p, 1, now + REVIEW_ERROR_BACKOFF_S + 1), "ready")
        self.assertEqual(
            survey_mod._review_error_state(counters, p, 3, now + 3 * REVIEW_ERROR_BACKOFF_S), "backoff", "doubles"
        )
        self.assertEqual(survey_mod._review_error_state(counters, p, MAX_REVIEW_ERRORS, now + 10**9), "capped")
        # An error streak from before the back-off existed carries no timestamp: retried at once.
        self.counter_values.pop(survey_mod.review_error_key(10))
        self.assertEqual(survey_mod._review_error_state(counters, p, 3, now), "ready")

    def test_a_pr_at_the_review_error_cap_is_offered_for_closing_only_if_maintained(self):
        survey_mod = importlib.import_module("tauceti_worker.survey")
        from tauceti_worker.constants import MAX_REVIEW_ERRORS

        for mine in (True, False):
            with self.subTest(mine=mine):
                sv = work.Survey(worker_id="test", me_login="science" if mine else "someone-else")
                p = PRInfo.from_json(row(10, "Other")["pr"])
                c = work.Candidate(10, HEAD, attempts=MAX_REVIEW_ERRORS, budget=MAX_REVIEW_ERRORS)
                survey_mod._file_review_candidate(sv, self.w.counters, p, c)
                self.assertEqual(sv.review_capped, [10])
                self.assertEqual([x.close_at_review_cap for x in sv.reviewable.actionable], [True] if mine else [])
                self.assertEqual([x.pr for x in sv.reviewable.suppressed], [] if mine else [10])
        # A declined close waits before it is offered again.
        self.counter_values[survey_mod.review_cap_close_key(10)] = 2**40
        sv = work.Survey(worker_id="test", me_login="science")
        survey_mod._file_review_candidate(sv, self.w.counters, PRInfo.from_json(row(10, "Other")["pr"]), c)
        self.assertEqual((sv.reviewable.actionable, [x.pr for x in sv.reviewable.suppressed]), ([], [10]))

    def test_the_review_error_cap_closes_a_maintained_pr_without_a_model_call(self):
        survey_mod = importlib.import_module("tauceti_worker.survey")
        from tauceti_worker.constants import MAX_REVIEW_ERRORS

        sv, _, live = self.spent_fixture()
        self.w.cfg.logdir = self.w.cfg.state
        self.counter_values["review-err-10"] = MAX_REVIEW_ERRORS
        self.w.rs.gh_meta = lambda *a, **k: SimpleNamespace(data={}, provenance="fresh")
        self.w.rs.ledger_clean_head = lambda pr: ""
        c = work.Candidate(10, HEAD, close_at_review_cap=True)

        def close(argv, **kwargs):
            live["state"] = "CLOSED"
            return SimpleNamespace(returncode=0)

        with (
            patch("tauceti_worker.github_outbox.pending_for", return_value=False),
            patch.object(work, "_check_stage_read_quota"),
            patch.object(work, "_still_actionable", side_effect=AssertionError("no review re-read")),
            patch.object(work, "_effective_authoring_profile", side_effect=AssertionError("no model path")),
            patch.object(work, "gh_run", side_effect=close) as mutation,
        ):
            self.assertEqual(work.dispatch("review", self.w, sv, c, self.opts), 0)
        argv = mutation.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "pr", "close", "10"])
        self.assertIn(f"failed {MAX_REVIEW_ERRORS} times", argv[argv.index("--comment") + 1])
        self.assertGreater(self.counter_values[survey_mod.review_cap_close_key(10)], 0)
        self.w.claims.release.assert_called_once()

    def test_the_review_error_cap_never_closes_a_kept_foreign_or_since_reviewed_pr(self):
        from tauceti_worker.constants import MAX_REVIEW_ERRORS

        cases = (
            ({"labels": [{"name": "keep"}]}, "", "fresh", MAX_REVIEW_ERRORS),
            ({"author": {"login": "peer"}}, "", "fresh", MAX_REVIEW_ERRORS),
            ({}, HEAD, "fresh", MAX_REVIEW_ERRORS),  # a peer reviewed the head since the survey
            ({}, "", "fetch_failed", MAX_REVIEW_ERRORS),  # review state unreadable
            ({}, "", "fresh", 0),  # this host's review succeeded since the survey
        )
        for changes, clean, provenance, errors in cases:
            with self.subTest(changes=changes, clean=clean, provenance=provenance, errors=errors):
                sv, _, _ = self.spent_fixture(**changes)
                self.w.cfg.logdir = self.w.cfg.state
                self.counter_values["review-err-10"] = errors
                self.w.rs.gh_meta = lambda *a, provenance=provenance, **k: SimpleNamespace(
                    data={}, provenance=provenance
                )
                self.w.rs.ledger_clean_head = lambda pr, clean=clean: clean
                with patch.object(work, "gh_run") as mutation:
                    self.assertIsNone(work._close_review_capped_pr(self.w, sv, work.Candidate(10, HEAD)))
                mutation.assert_not_called()

    def test_review_throttles_never_hold_back_a_review_cap_close(self):
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        sv.reviewable.actionable = [work.Candidate(10, HEAD, close_at_review_cap=True)]
        work.throttle_review(sv, SimpleNamespace(review_min_queue=5, review_min_age=0))
        self.assertEqual([c.pr for c in sv.reviewable.actionable], [10])

    def test_unlimited_bump_is_not_redirected_to_closure_by_its_attempt_count(self):
        sv, c, _ = self.spent_fixture()
        c.final_resolution = False
        sv.open_prs[0] = SimpleNamespace(number=10, author="bot", automatic_bump=True)
        with (
            patch("tauceti_worker.github_outbox.pending_for", return_value=False),
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
        # Model an independent worker's shuffled order. Claimed and contested
        # heads must not hide the remaining maintenance in a different area.
        rows = [
            row(30, "Algebra"),
            row(20, "PDE"),
            row(10, "Topology"),
            row(15, "Geometry", contest={"id": 1, "rubric": "reuse"}),
        ]
        self.counter_values[f"fix-10-{HEAD[:12]}"] = 3
        with patch.object(work, "age_spread", side_effect=lambda cs: sorted(cs, key=lambda c: -c.pr)):
            _, query, areas = self.run_round(rows, lambda stage, c: None if c.pr in (15, 30, 20) else 0)
        # The author contest is closest to landing (a re-review), so it is offered before the fixes.
        self.assertEqual([(s, n) for s, n, *_ in self.calls], [("review", 15), ("fix", 30), ("fix", 20), ("fix", 10)])
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

    def test_review_claim_hint_skips_fresh_reads_without_taking_a_claim(self):
        self.opts.only = ["review"]
        with patch.object(work, "_review_claimed_elsewhere", return_value=True):
            with self.assertRaises(ClaimCollision):
                self.run_round([row(10, "Other", author="peer", blocking=False)])
        self.assertEqual(self.calls, [])

        self.w.gh.repo = "TauCetiProject/TauCeti"
        import json

        with (
            patch.object(
                work.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({"owner": "peer-nonce", "expires_at": 200})
                ),
            ) as git,
            patch.object(work.time, "time", return_value=100),
            patch.object(work, "claims_repo", return_value="org/claims"),
        ):
            self.assertTrue(work._review_claimed_elsewhere(self.w, work.Candidate(10, HEAD)))
        self.assertEqual(git.call_args.args[0][1:], ["read", f"review/taucetiproject/tauceti/10/{HEAD}"])
        self.assertEqual(git.call_args.kwargs["env"]["CLAIM_REPO"], "org/claims")

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
        self.w.gh.pr_view = lambda *_: dict(headRefOid=HEAD, isDraft=False, state="OPEN", labels=[])
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        for label, code, checked in (("fix-ci", 0, "b" * 40), ("fix-ci", 1, ""), ("fix", 0, "b" * 40)):
            with self.subTest(label=label, code=code, checked=checked):
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
                    patch.object(work, "_progress_snapshot", return_value=None),
                    patch.object(work, "_still_actionable", return_value=True),
                ):
                    result = work._do_fixlike(
                        self.w,
                        sv,
                        work.Candidate(10, HEAD),
                        self.opts,
                        False,
                        prompt_file="fix-ci.md",
                        label=label,
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

    def test_reserved_fleet_slot_uses_area_cascade_and_shared_backpressure(self):
        with (
            patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=["NewArea"]) as focus,
            patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="2"),
        ):
            _, query, areas = self.run_round([row(10, "Other"), row(20, "NewArea")])
            self.assertEqual(self.calls[0][:2], ("fix", 20))
            self.assertEqual(query.call_args.args[1], "NewArea")
            areas.assert_called_once_with(self.w.gh)
            focus.assert_called_once_with(self.w.cfg.wid)
            # Focus cannot exceed the shared account limit; useful global work
            # replaces an otherwise idle focused round when authoring is capped.
            focus.reset_mock()
            _, query, _ = self.run_round([row(10, "Other"), row(30, "Other")])
            self.assertEqual(self.calls[0][:2], ("fix", 10))
            self.assertEqual(query.call_args.args[1], "auto")
            focus.assert_called_once_with(self.w.cfg.wid)
            # Below the shared limit, new authoring remains in the focus area.
            self.run_round([], author_count=1)
            self.assertEqual(self.calls, [("roadmap", 0, "NewArea", "NewArea")])

    def test_focused_round_is_reported_for_fleet_status(self):
        def reported(runtime):
            return [c.kwargs["roadmap_focus"] for c in runtime.call_args_list if "roadmap_focus" in c.kwargs]

        with (
            patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=["NewArea"]),
            patch.dict(os.environ, TAUCETI_MAX_OPEN_PRS="2"),
            patch.object(work, "report_runtime") as runtime,
        ):
            self.run_round([row(10, "Other"), row(20, "NewArea")])
            self.assertEqual(reported(runtime), ["NewArea"])
            # A capped focus slot that falls back to global work is no longer focused.
            runtime.reset_mock()
            self.run_round([row(10, "Other"), row(30, "Other")])
            self.assertEqual(reported(runtime), ["NewArea", None])
        with (
            patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=[]),
            patch.object(work, "report_runtime") as runtime,
        ):
            self.run_round([row(10, "Other")])
            self.assertEqual(reported(runtime), [None])

    def test_missing_or_skipped_focus_keeps_global_maintenance(self):
        for focused, skip in ((["RemovedArea"], ""), (["NewArea"], "NewArea")):
            with (
                patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=focused),
                patch.dict(os.environ, TAUCETI_ROADMAP_SKIP=skip),
            ):
                _, query, _ = self.run_round([row(10, "Other")])
                self.assertEqual(self.calls[0][:2], ("fix", 10))
                self.assertEqual(query.call_args.args[1], "auto")

    def test_fractional_miss_is_not_drawn_again_when_authoring(self):
        with patch("tauceti_worker.fleet_capacity.roadmap_focus", side_effect=[[], ["NewArea"]]) as focus:
            self.run_round([])
            self.assertEqual(self.calls, [("roadmap", 0, "NewArea", "NewArea")])
            focus.assert_called_once_with(self.w.cfg.wid)

    def test_fleet_focus_keeps_explicit_area_and_pr_overrides(self):
        with patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=["NewArea"]) as focus:
            with patch.object(work, "roadmap_only", return_value="Topology"):
                self.run_round([row(10, "Other"), row(20, "Topology")])
                self.assertEqual(self.calls[0][:2], ("fix", 20))
            self.opts.prs = (10,)
            self.run_round([row(10, "Other"), row(20, "NewArea")])
            self.assertEqual(self.calls[0][:2], ("fix", 10))
            focus.assert_not_called()

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
        with patch.object(self.w.rs, "gh_meta", return_value=SimpleNamespace(data={}, provenance="fetch_failed")):
            self.assertTrue(work._still_actionable("fix", self.w, Survey("test"), c))

    def test_fix_reconciles_after_claim_and_skips_before_checkout(self):
        from tauceti_worker import github_transport as transport

        claimed = []
        self.w.claims = SimpleNamespace(
            held=("branch/10", "org/claims"),
            begin_branch_work=lambda *args: claimed.append(True) or True,
            stop_heartbeat=Mock(),
            release=Mock(),
        )
        self.counter_values["attempt"] = 1
        self.w.counters.write = lambda key, value: self.counter_values.update({key: value})

        def progress(_):
            self.assertTrue(claimed)
            self.assertTrue(transport.direct_reads())
            return dict(head="b" * 40, state="OPEN", isDraft=False, labels=[], ncomments=0)

        self.w.gh.pr_progress_state = progress
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        with patch.object(work, "prepare_checkout") as checkout, patch.object(work, "run_agent_host") as model:
            self.assertIsNone(
                work._do_fixlike(
                    self.w,
                    sv,
                    work.Candidate(10, HEAD),
                    self.opts,
                    False,
                    prompt_file="fix.md",
                    label="fix",
                    charged=("attempt",),
                )
            )
        checkout.assert_not_called()
        model.assert_not_called()
        self.w.claims.release.assert_called_once()
        self.assertEqual(self.counter_values["attempt"], 0)
        self.assertEqual(sv.open_prs[0].head_oid, HEAD)
        # Unknown optional observations must reach normal preparation, not a wait.
        with (
            patch.object(work, "_progress_snapshot", return_value=None),
            patch.object(self.w.gh, "pr_view", return_value=None, create=True),
            patch.object(work, "scope_policy", side_effect=RuntimeError("normal preparation")),
            self.assertRaisesRegex(RuntimeError, "normal preparation"),
        ):
            work._do_fixlike(self.w, sv, work.Candidate(10, HEAD), self.opts, False, prompt_file="fix.md", label="fix")

    def test_fix_policy_uses_live_labels_and_retains_survey_on_incomplete_read(self):
        sv = work.Survey(worker_id="test", me_login="science")
        p = PRInfo.from_json(row(10, "Other")["pr"])
        p = work.replace(p, labels=["full-lint"])
        sv.open_prs = [p]
        self.w.claims.begin_branch_work = lambda *args: True
        for label in ("fix", "rebase"):
            for live, expected in (
                (dict(head=HEAD, state="OPEN", isDraft=False, labels=[dict(name="full-lint")]), ["full-lint"]),
                (dict(head=HEAD, state="OPEN", isDraft=False, labels=[]), []),
                (None, ["full-lint"]),
            ):
                with (
                    patch.object(work, "_progress_snapshot", return_value=live),
                    patch.object(self.w.gh, "pr_view", return_value=None, create=True),
                    patch.object(work, "scope_policy", side_effect=RuntimeError("policy observed")) as policy,
                    self.assertRaisesRegex(RuntimeError, "policy observed"),
                ):
                    work._do_fixlike(
                        self.w, sv, work.Candidate(10, HEAD), self.opts, False, prompt_file="fix.md", label=label
                    )
                self.assertEqual(policy.call_args.args[0], expected)

    def test_dispatch_host_rebase_reads_progress_once_after_claim(self):
        self.opts.work_model = "claude"
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        self.w.cfg.checkout = Path(self.tmp.name) / "checkout"
        self.w.counters.incr = lambda key: self.counter_values.update({key: 1})
        claimed = []
        self.w.claims.begin_branch_work = lambda *args: claimed.append(True) or True

        def snapshot(*args):
            self.assertTrue(claimed, "dispatch must leave the baseline read to the claimed stage")
            return dict(head=HEAD, state="OPEN", isDraft=False, labels=[])

        with (
            patch.object(work, "_still_actionable", return_value=True),
            patch.object(work, "_host_agent_binary", return_value=None),
            patch.object(work, "_check_stage_read_quota"),
            patch.object(work, "_checkout_head", return_value=HEAD),
            patch.object(work, "_progress_snapshot", side_effect=snapshot) as progress,
            patch("tauceti_worker.claude_api.api_mode", return_value=False),
            patch("tauceti_worker.claude_api.api_context", side_effect=lambda *a, **kw: contextlib.nullcontext()),
            patch.object(work, "scope_policy", side_effect=RuntimeError("preparation reached")),
            self.assertRaisesRegex(RuntimeError, "preparation reached"),
        ):
            work.dispatch("rebase", self.w, sv, work.Candidate(10, HEAD, rebase_budget_key="rebase-test"), self.opts)
        progress.assert_called_once()

    def test_stale_fix_never_reaches_paid_provider_probes(self):
        self.opts.work_model = "claude"
        self.w.gh.pr_view = lambda *_: dict(headRefOid="b" * 40, isDraft=False, state="OPEN", labels=[])
        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        with (
            patch("tauceti_worker.github_outbox.pending_for", return_value=False),
            patch.object(work, "_host_agent_binary", return_value=None),
            patch.object(work, "resolve_codex_model_access") as entitlement,
            patch("tauceti_worker.fleet_capacity.admission_reason") as admission,
        ):
            self.assertIsNone(work.dispatch("fix", self.w, sv, work.Candidate(10, HEAD), self.opts))
        entitlement.assert_not_called()
        admission.assert_not_called()

    def test_partial_fix_sees_current_reply_instead_of_repeating_it(self):
        from tauceti_worker import github_transport as transport

        self.w.gh.pr_view = lambda *a: dict(headRefOid=HEAD, isDraft=False, state="OPEN", labels=[])

        def meta(*args, **kwargs):
            self.assertTrue(transport.direct_reads())
            return SimpleNamespace(data={"head_sha": HEAD, "replies_through": 1}, provenance="fresh")

        sv = work.Survey(worker_id="test", me_login="science")
        sv.open_prs = [PRInfo.from_json(row(10, "Other")["pr"])]
        with (
            patch.object(self.w.rs, "gh_meta", side_effect=meta),
            patch.object(self.w.rs, "ledger_blocking", return_value=True),
            patch.object(self.w.rs, "newest_contest_reply", return_value={"id": 2}),
        ):
            self.assertFalse(work._still_actionable("fix", self.w, sv, work.Candidate(10, HEAD, partial_history=True)))
        self.counter_values[f"fix-10-{HEAD[:12]}"] = 2
        sv.open_prs = [PRInfo.from_json(row(10, "Other", author="peer")["pr"])]
        with (
            patch.object(self.w.rs, "gh_meta", side_effect=meta),
            patch.object(self.w.rs, "ledger_blocking", return_value=True),
            patch.object(self.w.rs, "newest_contest_reply", return_value=None),
        ):
            self.assertTrue(work._still_actionable("fix", self.w, sv, work.Candidate(10, HEAD)))
            self.counter_values[f"fix-10-{HEAD[:12]}"] = 3
            self.assertFalse(work._still_actionable("fix", self.w, sv, work.Candidate(10, HEAD)))

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

    def test_recent_unchanged_engine_result_suppresses_partial_history_review(self):
        survey = importlib.import_module("tauceti_worker.survey")
        for recorded, offered in ((HEAD, False), ("b" * 40, True)):
            self.w.counters.unchanged_review_recent = lambda pr, head, recorded=recorded: head == recorded
            with patch(
                "tauceti_worker.github_state.read_survey", return_value=(True, [row(10, "Other", complete=False)])
            ):
                result = survey.survey(
                    self.w.cfg,
                    self.w.gh,
                    self.w.rs,
                    self.w.counters,
                    stages=["review"],
                    selected_roadmap="auto",
                    cached_only=True,
                )
            self.assertEqual([c.pr for c in result.reviewable.actionable] == [10], offered)

    def test_disabled_maintenance_remains_disabled(self):
        self.opts.only = ["roadmap"]
        self.run_round([row(10, "Other", complete=False)])
        self.assertEqual(self.calls[0][:2], ("roadmap", 0))


if __name__ == "__main__":
    unittest.main()

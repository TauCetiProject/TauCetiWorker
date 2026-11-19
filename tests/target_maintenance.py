"""Target-list priority stays inside each stage and this account's own slots."""

import dataclasses
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.constants import AUTO_STAGES
from tauceti_worker.survey import Candidate, PRInfo, Survey, cascade

work = importlib.import_module("tauceti_worker.work_units")
HEAD = "a" * 40
TARGETS = "<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `goal` — Prove the target (needs: none)\n"
MARKER = '<!--tauceti-target:v1 {"focus":"Algebra","id":"goal"}-->'


def pr(number, *, author="science", bump=False):
    return PRInfo(
        number,
        HEAD,
        "bump-mathlib/x" if bump else f"roadmap/work-{number}",
        "TauCetiProject",
        "TauCeti",
        False,
        "MERGEABLE",
        author,
        True,
        False,
        bump,
    )


def survey():
    sv = Survey(worker_id="test", me_login="science")
    sv.open_prs = [pr(n) for n in (1, 2, 3, 4, 5, 6, 7, 8)] + [pr(50, author="peer"), pr(9, bump=True)]
    for stage, numbers in (
        ("rebase", [1, 9, 2]),
        ("lint-repair", [7]),
        ("progress", [0]),
        ("fix-ci", [3, 4]),
        ("fix", [5, 50, 6]),
        ("review", [50, 1, 2, 5, 6]),
    ):
        sv.kind(stage).actionable = [Candidate(n, HEAD) for n in numbers]
    return sv


def order(pairs):
    return [(stage, candidate.pr) for stage, candidate in pairs]


class Ordering(unittest.TestCase):
    def test_stage_boundaries_and_bump_priority_survive(self):
        sv = survey()
        self.assertEqual(
            order(cascade(sv, AUTO_STAGES, {2, 4, 6, 50})),
            [
                ("rebase", 9),
                ("rebase", 2),
                ("rebase", 1),
                ("lint-repair", 7),
                ("progress", 0),
                ("fix-ci", 4),
                ("fix-ci", 3),
                ("fix", 6),
                ("fix", 50),
                ("fix", 5),
                ("review", 50),
                ("review", 2),
                ("review", 6),
                ("review", 1),
                ("review", 5),
            ],
        )

    def test_automatic_bumps_keep_global_stage_order_even_when_owned(self):
        sv = survey()
        sv.open_prs += [pr(10, bump=True)]
        sv.kind("fix").actionable.append(Candidate(10, HEAD))
        sv.kind("review").actionable.append(Candidate(9, HEAD))
        base = order(cascade(sv, AUTO_STAGES))
        listed = order(cascade(sv, AUTO_STAGES, {2, 4, 6, 10}))
        self.assertEqual(listed[:3], [("rebase", 9), ("fix", 10), ("review", 9)])
        self.assertEqual([p for p in listed if p[1] in (9, 10)], [p for p in base if p[1] in (9, 10)])

    def test_review_never_moves_other_authors_or_non_target_stages(self):
        sv = survey()
        base = order(cascade(sv, AUTO_STAGES))
        listed = order(cascade(sv, AUTO_STAGES, {2, 4, 6, 50, 7}))
        for i, pair in enumerate(base):
            if pair[1] in (9, 50) or pair[0] in ("lint-repair", "progress"):
                self.assertEqual(listed[i], pair)
        self.assertEqual([p[0] for p in base], [p[0] for p in listed])

    def test_no_list_preserves_current_order_without_ownership_reads(self):
        sv = survey()
        before = order(cascade(sv, AUTO_STAGES))
        sv.owns = Mock(side_effect=AssertionError("ownership must not be read"))
        self.assertEqual(order(cascade(sv, AUTO_STAGES, set())), before)
        sv.owns.assert_not_called()

    def test_selected_stages_and_qualified_native_ownership(self):
        sv = survey()
        sv.source_repository = "forgejo"
        sv.owns = lambda p: p.number in {3, 4, 5, 6}
        self.assertEqual(
            order(cascade(sv, ["fix-ci", "fix"], {4, 6, 50})),
            [
                ("fix-ci", 4),
                ("fix-ci", 3),
                ("fix", 6),
                ("fix", 50),
                ("fix", 5),
            ],
        )


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "targets.md"
        self.path.write_text(TARGETS)
        self.worker = SimpleNamespace(gh=Mock(repo="one/project"))
        self.worker.gh.open_prs.side_effect = AssertionError("maintenance must not make a direct inventory read")
        self.sv = survey()

    def listed(self, *, mirrored=False, rows=None, available=True):
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path)}),
            patch.object(work, "github_state_enabled", return_value=mirrored),
            patch("tauceti_worker.github_state.read", return_value=(available, rows)) as read,
        ):
            result = work.target_list_prs(self.worker, self.sv, AUTO_STAGES)
        self.worker.gh.open_prs.assert_not_called()
        return result, read

    def test_direct_inventory_reuses_parsed_complete_and_partial_markers(self):
        from tauceti_worker.survey import work_target_keys

        for body in (MARKER, MARKER.replace('"goal"', '"goal","partial":true')):
            self.sv.open_prs = [
                dataclasses.replace(p, work_targets=work_target_keys(body)) if p.number == 2 else p
                for p in self.sv.open_prs
            ]
            listed, read = self.listed()
            self.assertEqual(listed, {2})
            read.assert_not_called()

    def test_compact_inventory_fills_marker_bodies_from_the_mirror_for_the_same_head(self):
        rows = [{"number": 2, "headRefOid": HEAD, "body": MARKER}]
        listed, read = self.listed(mirrored=True, rows=rows)
        self.assertEqual(listed, {2})
        read.assert_called_once_with("one/project", "open_prs", _optional=True)
        rows[0]["headRefOid"] = "b" * 40
        self.assertEqual(self.listed(mirrored=True, rows=rows)[0], set())

    def test_mirror_client_preserves_bodies_for_compact_inventory_priority(self):
        from tauceti_worker import github_state as state

        class Response(io.BytesIO):
            headers = {"ETag": '"target-maintenance"'}

        self.worker.gh.repo = state.PUBLIC_REPOSITORY
        payload = {
            "version": 1,
            "repository": state.PUBLIC_REPOSITORY,
            "operation": "open_prs",
            "status": "ok",
            "complete": True,
            "revision": 1,
            "observed_at": 1,
            "verified_at": 1,
            "data": [{"number": 2, "headRefOid": HEAD, "body": MARKER}],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "TAUCETI_ROADMAP_TARGETS": str(self.path),
                    "TAUCETI_GITHUB_STATE_URL": state.DEFAULT_URL,
                    "TAUCETI_GITHUB_SHARED_DIR": str(Path(self.tmp.name) / "github-state"),
                },
            ),
            patch.object(state.urllib.request, "urlopen", return_value=Response(json.dumps(payload).encode())) as fetch,
        ):
            os.environ.pop("TAUCETI_GITHUB_STATE_TOKEN_FILE", None)
            self.assertEqual(work.target_list_prs(self.worker, self.sv, AUTO_STAGES), {2})
        self.assertIn("operation=open_prs", fetch.call_args.args[0].full_url)
        self.worker.gh.open_prs.assert_not_called()

    def test_older_same_head_mirror_body_cannot_remove_a_direct_inventory_marker(self):
        from tauceti_worker.survey import work_target_keys

        self.sv.open_prs = [
            dataclasses.replace(p, work_targets=work_target_keys(MARKER)) if p.number == 2 else p
            for p in self.sv.open_prs
        ]
        rows = [{"number": 2, "headRefOid": HEAD, "body": "body before the marker was added"}]
        self.assertEqual(self.listed(mirrored=True, rows=rows)[0], {2})
        self.sv.target_inventory_available = True
        listed, read = self.listed(mirrored=True)
        self.assertEqual(listed, {2})
        read.assert_not_called()

    def test_done_items_still_prioritize_their_open_repairs(self):
        self.path.write_text(TARGETS.replace("[ ]", "[x]"))
        listed, _ = self.listed(mirrored=True, rows=[{"number": 2, "headRefOid": HEAD, "body": MARKER}])
        self.assertEqual(listed, {2})

    def test_unavailable_missing_or_malformed_body_keeps_the_default_order(self):
        self.assertEqual(self.listed(mirrored=True, available=False)[0], set())
        for body in (None, MARKER[:-4], '<!--tauceti-target:v1 {"focus":"Algebra","id":"other"}-->'):
            rows = [{"number": 2, "headRefOid": HEAD, "body": body}]
            self.assertEqual(self.listed(mirrored=True, rows=rows)[0], set())
        self.path.write_text("not a target list")
        listed, read = self.listed(mirrored=True)
        self.assertEqual(listed, set())
        read.assert_not_called()

    def test_native_inventory_never_borrows_a_same_numbered_github_marker(self):
        self.sv.source_actor = object()
        self.sv.owns = lambda p: p.number in {1, 2}
        self.assertEqual(self.listed(mirrored=True, rows=[{"number": 2, "headRefOid": HEAD, "body": MARKER}])[0], set())
        self.assertEqual(self.listed(mirrored=True)[1].call_count, 0)
        key = json.dumps(["Algebra", "goal"], separators=(",", ":"))
        self.sv.open_prs = [
            dataclasses.replace(p, work_targets=(key,)) if p.number == 2 else p for p in self.sv.open_prs
        ]
        self.assertEqual(self.listed(mirrored=True)[0], {2})

    def test_no_list_or_competing_owned_prs_reads_nothing(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}):
            untouched = Mock(spec=["open_prs", "owns", "kind"])
            self.assertEqual(work.target_list_prs(self.worker, untouched, AUTO_STAGES), set())
            untouched.owns.assert_not_called()
            untouched.kind.assert_not_called()
        self.sv.open_prs = self.sv.open_prs[:1]
        self.assertEqual(self.listed(mirrored=True)[0], set())
        self.assertEqual(self.listed(mirrored=True)[1].call_count, 0)

    def test_explicit_candidate_selection_limits_priority_membership(self):
        self.sv.rebaseable.actionable = [Candidate(1, HEAD)]
        for stage in ("fix", "fix-ci", "review"):
            self.sv.kind(stage).actionable = []
        listed, read = self.listed(mirrored=True, rows=[{"number": 2, "headRefOid": HEAD, "body": MARKER}])
        self.assertEqual(listed, set())
        read.assert_not_called()


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "targets.md"
        self.path.write_text(TARGETS)
        self.worker = SimpleNamespace(
            cfg=SimpleNamespace(wid="test"),
            gh=Mock(repo="one/project"),
            rs=None,
            counters=None,
        )
        self.worker.gh.open_prs.side_effect = AssertionError("unexpected direct inventory read")

    def contexts(self):
        from contextlib import ExitStack

        stack = ExitStack()
        for context in (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path)}),
            patch.object(work, "roadmap_only", return_value="any"),
            patch.object(work, "github_state_enabled", return_value=False),
            patch.object(work, "spread_candidates", side_effect=list),
            patch.object(work, "prioritize_review_candidates", side_effect=lambda queue, *a, **k: (list(queue), [])),
            patch.object(work, "log"),
        ):
            stack.enter_context(context)
        return stack

    def test_round_uses_priorities_after_explicit_pr_filtering(self):
        sv = survey()
        key = json.dumps(["Algebra", "goal"], separators=(",", ":"))
        sv.open_prs = [dataclasses.replace(p, work_targets=(key,)) if p.number == 6 else p for p in sv.open_prs]
        opts = work.RoundOpts(only=["fix"], agent="claude", work_model="claude", sandbox_host=True, dry_run=True)
        seen = []
        with self.contexts(), patch.object(work, "dispatch", side_effect=lambda stage, w, sv, c, o: seen.append(c.pr)):
            with self.assertRaises(work.NoEligibleWork):
                work.run_round(self.worker, opts, prepared_survey=sv)
            self.assertEqual(seen, [6, 50, 5])
            seen.clear()
            opts.prs = (5, 50)
            with self.assertRaises(work.NoEligibleWork):
                work.run_round(self.worker, opts, prepared_survey=sv)
            self.assertEqual(seen, [5, 50])
        self.worker.gh.open_prs.assert_not_called()

    def test_multi_source_dispatches_listed_work_first_inside_the_selected_stage(self):
        from tauceti_worker import multi_source

        first = Survey(worker_id="test", me_login="science")
        first.target_inventory_available = True
        key = json.dumps(["Algebra", "goal"], separators=(",", ":"))
        first.open_prs = [pr(5), dataclasses.replace(pr(6), work_targets=(key,))]
        first.needs_fix.actionable = [Candidate(5, HEAD), Candidate(6, HEAD)]
        second = Survey(worker_id="test", me_login="science")
        second.source_actor = object()
        native = SimpleNamespace(gh=Mock(repo="one/project"))
        observations = [("github", self.worker, first), ("forgejo", native, second)]
        opts = work.RoundOpts(only=["fix"], agent="claude", work_model="claude", sandbox_host=True, dry_run=True)
        with (
            self.contexts(),
            patch.object(multi_source, "collect", return_value=observations),
            patch.object(multi_source.random, "shuffle"),
            patch.object(work, "github_state_enabled", return_value=True),
            patch("tauceti_worker.github_state.read", side_effect=AssertionError("inventory already enriched")),
            patch.object(work, "dispatch", return_value=0) as dispatch,
        ):
            self.assertEqual(multi_source.run_round(self.worker, opts), 0)
        self.assertEqual(dispatch.call_args.args[0], "fix")
        self.assertEqual(dispatch.call_args.args[3].pr, 6)

    def test_multi_source_stage_order_does_not_promote_a_listed_fix_over_a_rebase(self):
        from tauceti_worker import multi_source

        first = Survey(worker_id="test", me_login="science")
        first.open_prs = [pr(1)]
        first.rebaseable.actionable = [Candidate(1, HEAD)]
        second = Survey(worker_id="test", me_login="science")
        second.source_actor = object()
        key = json.dumps(["Algebra", "goal"], separators=(",", ":"))
        second.open_prs = [dataclasses.replace(pr(1), head_oid="b" * 40, work_targets=(key,))]
        second.needs_fix.actionable = [Candidate(1, "b" * 40)]
        native = SimpleNamespace(gh=Mock(repo="one/project"))
        observations = [("github", self.worker, first), ("forgejo", native, second)]
        opts = work.RoundOpts(
            only=["rebase", "fix"], agent="claude", work_model="claude", sandbox_host=True, dry_run=True
        )
        with (
            self.contexts(),
            patch.object(multi_source, "collect", return_value=observations),
            patch.object(multi_source.random, "shuffle"),
            patch.object(work, "dispatch", return_value=0) as dispatch,
        ):
            self.assertEqual(multi_source.run_round(self.worker, opts), 0)
        self.assertEqual(dispatch.call_args.args[0], "rebase")
        self.assertIs(dispatch.call_args.args[1], self.worker)


if __name__ == "__main__":
    unittest.main()

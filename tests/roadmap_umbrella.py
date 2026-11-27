"""Umbrellas for --roadmap-only (docs/umbrellas.md): the file, the draw, maintenance scope, the prompt and the
displays, over a fake roadmap snapshot and fake GitHub reads. No network."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import argparse
import base64
import contextlib
import dataclasses
import importlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tauceti_worker import cli, tui, umbrellas
from tauceti_worker.config import Die, NoProgress, _only_label
from tauceti_worker.constants import ROADMAP, ROADMAP_DIR
from tauceti_worker.github_state import SurveyRows
from tauceti_worker.review_state import ReviewState
from tauceti_worker.survey import PRInfo, Survey, roadmap_open_count, survey
from tauceti_worker.umbrellas import Member, Umbrella, UmbrellaRound
from tauceti_worker.work_units import RoundOpts, run_round

work = importlib.import_module("tauceti_worker.work_units")
DOC = (REPO / "docs" / "umbrellas.md").read_text()
EXAMPLE = re.search(r"```toml\n(.*?)```", DOC, re.S).group(1)
HEAD = "a" * 40
ANT, RH, LATER = "AnalyticNumberTheoryPartI", "RiemannHypothesisForDirichletLFunctions", "NotYetMerged"
GRH = f"""\
description = "Riemann hypothesis for Dirichlet L-functions"
added_later = "unknown keys are ignored"

[[area]]
name = "{ANT}"
sections = ["§1.6", "§3.2"]
weight = 4

[[area]]
name = "{RH}"

[[area]]
name = "{LATER}"
"""
AREAS = [ANT, RH, "Topology", "Other"]
STAGES = ["rebase", "fix-ci", "fix", "review"]


def row(number, area, *, author="science", blocking=True):
    """One compact-survey row (the shape tests/global_maintenance.py uses)."""
    return {
        "pr": {
            "number": number,
            "author": {"login": author},
            "headRefOid": HEAD,
            "headRefName": "roadmap/work",
            "headRepositoryOwner": {"login": author},
            "headRepository": {"name": "TauCeti"},
            "labels": [{"name": "roadmap/" + area}],
            "mergeable": "MERGEABLE",
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
            "contest": None,
            "issue_complete": True,
            "review_complete": True,
        },
    }


class Roadmap:
    """A stand-in for the shared snapshot of the roadmap repository: area directories under
    TauCetiRoadmap/ and umbrella files under umbrellas/. Every read is counted."""

    def __init__(self, test, areas=AREAS, files=None):
        tmp = tempfile.TemporaryDirectory(prefix="umbrella-roadmap-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for area in areas:
            (self.root / ROADMAP_DIR / area).mkdir(parents=True)
        for name, text in (files or {}).items():
            (self.root / umbrellas.UMBRELLA_DIR).mkdir(exist_ok=True)
            (self.root / umbrellas.UMBRELLA_DIR / f"{name}.toml").write_text(text)
        self.available = True
        self.reads = 0

    @contextlib.contextmanager
    def snapshot(self, repo, *, ttl=3600):
        assert repo == ROADMAP
        self.reads += 1
        yield self.root if self.available else None

    def install(self, test):
        replacement = patch("tauceti_worker.shared_repositories.borrowed_snapshot", new=self.snapshot)
        replacement.start()
        test.addCleanup(replacement.stop)
        return self


def grh_round(area=None, skip=(), current=None):
    umbrella = umbrellas.parse_umbrella("GRH", GRH.encode())
    scope = frozenset(m.area for m in umbrella.members if m.area not in skip)
    return UmbrellaRound(umbrella, scope, area, None if current is None else frozenset(current))


class File(unittest.TestCase):
    def test_the_documented_example_parses(self):
        umbrella = umbrellas.parse_umbrella("GRH", EXAMPLE.encode())
        self.assertEqual([m.area for m in umbrella.members], [ANT, RH])
        self.assertEqual(umbrella.members[0], Member(ANT, ("§1.6", "§3.2"), 4))
        self.assertEqual(umbrella.members[1], Member(RH, (), 1))
        self.assertIn("Riemann hypothesis", umbrella.description)
        self.assertIn("tauceti work --loop --full-quality --roadmap-only GRH", DOC)

    def test_unknown_keys_are_ignored_and_the_description_is_one_line(self):
        text = 'description = """two\n lines"""\nlater = 1\n[[area]]\nname = "A"\nnote = "x"\n'
        umbrella = umbrellas.parse_umbrella("U", text.encode())
        self.assertEqual(umbrella, Umbrella("U", "two lines", (Member("A"),)))
        self.assertEqual(umbrellas.parse_umbrella("U", b'[[area]]\nname = "A"\n').description, "")

    def test_a_broken_file_is_refused_naming_the_file(self):
        for text, why in (
            ("description = ", "does not parse"),
            (b"\xff", "does not parse"),
            ('description = "x"\n', "lists no area"),
            ("area = []\n", "lists no area"),
            ('area = "A"\n', r"\[\[area\]\] tables"),
            ("description = 3\n[[area]]\nname = 'A'\n", "description must be a string"),
            ("[[area]]\nsections = []\n", r"\[\[area\]\] 1 needs a name"),
            ("[[area]]\nname = '../A'\n", "needs a name"),
            ("[[area]]\nname = 'A'\n[[area]]\nname = 'A'\n", "A is listed twice"),
            ("[[area]]\nname = 'A'\nweight = 0\n", "weight of A must be a positive integer"),
            ("[[area]]\nname = 'A'\nweight = true\n", "weight of A must be a positive integer"),
            ("[[area]]\nname = 'A'\nweight = 1.5\n", "weight of A must be a positive integer"),
            ("[[area]]\nname = 'A'\nsections = '§1'\n", "sections of A must be a list"),
            ("[[area]]\nname = 'A'\nsections = ['']\n", "sections of A must be a list"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(Die, why) as raised:
                umbrellas.parse_umbrella("GRH", text if isinstance(text, bytes) else text.encode())
            self.assertIn(f"umbrella GRH (umbrellas/GRH.toml in {ROADMAP})", str(raised.exception))


class Reading(unittest.TestCase):
    def test_a_name_is_an_umbrella_only_with_a_file_and_no_area_of_that_name(self):
        roadmap = Roadmap(self, files={"GRH": GRH, "Topology": GRH}).install(self)
        self.assertEqual(umbrellas.read_umbrella(None, "GRH").name, "GRH")
        self.assertIsNone(umbrellas.read_umbrella(None, "Topology"), "an area of that name always wins")
        self.assertIsNone(umbrellas.read_umbrella(None, "Missing"))
        self.assertEqual(roadmap.reads, 3)

    def test_the_roadmap_repository_without_an_umbrellas_directory_has_no_umbrella(self):
        Roadmap(self).install(self)
        self.assertIsNone(umbrellas.read_umbrella(None, "GRH"))

    def test_unset_all_areas_and_path_names_read_nothing(self):
        roadmap = Roadmap(self, files={"GRH": GRH}).install(self)
        for name in (None, "", "any", "auto", "../GRH", "umbrellas/GRH", ".GRH", "GRH "):
            self.assertIsNone(umbrellas.read_umbrella(None, name))
        self.assertEqual(roadmap.reads, 0)

    def test_the_contents_api_is_the_fallback_as_for_the_area_list(self):
        roadmap = Roadmap(self).install(self)
        roadmap.available = False
        listed = {"areas": AREAS, "file": base64.b64encode(GRH.encode()).decode()}

        def api_jq(path, jq):
            if path == f"repos/{ROADMAP}/contents/{umbrellas.UMBRELLA_DIR}/GRH.toml":
                return listed["file"]
            if path == f"repos/{ROADMAP}/contents/{ROADMAP_DIR}":
                return "\n".join(listed["areas"])
            self.fail(f"unexpected read {path}")

        gh = SimpleNamespace(api_jq=Mock(side_effect=api_jq))
        self.assertEqual([m.area for m in umbrellas.read_umbrella(gh, "GRH").members], [ANT, RH, LATER])
        listed["areas"] = AREAS + ["GRH"]
        self.assertIsNone(umbrellas.read_umbrella(gh, "GRH"), "an area of that name always wins")
        listed["file"] = None  # absent, or the read failed: no umbrella, exactly as before
        self.assertIsNone(umbrellas.read_umbrella(gh, "GRH"))

    def test_a_display_shows_a_broken_umbrella_by_name_instead_of_failing(self):
        Roadmap(self, files={"GRH": "description = "}).install(self)
        with self.assertRaisesRegex(Die, "does not parse"):
            umbrellas.umbrella_scope(None, "GRH")
        with patch.object(umbrellas, "log") as log:
            self.assertIsNone(umbrellas.umbrella_scope(None, "GRH", display=True))
        self.assertIn("showing GRH without its umbrella", log.call_args.args[0])


class RoundHarness(unittest.TestCase):
    """Whole rounds through run_round, as tests/global_maintenance.py drives them, with GRH defined."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = SimpleNamespace(wid="test", sbcache=root / "sb", store_dir=root / "review", state=root)

        def forbidden(*args, **kwargs):
            self.fail("an umbrella round performed an upstream or per-PR history read")

        gh = SimpleNamespace(
            repo="org/repo",
            open_prs=forbidden,
            issue_comments=forbidden,
            review_comments=forbidden,
            fresh_claim_age=forbidden,
            rebase_requested=forbidden,
            api_jq=forbidden,
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
        self.roadmap = Roadmap(self, files={"GRH": GRH}).install(self)
        self.logged = []
        for item in (
            patch.dict(
                os.environ,
                {
                    "TAUCETI_GITHUB_STATE_URL": "https://state.test",
                    "TAUCETI_ROADMAP_SKIP": "",
                    "TAUCETI_ROADMAP_TARGETS": "",
                },
            ),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch.object(work, "mirror_creds"),
            patch.object(work, "_branch_claimed_elsewhere", return_value=False),
            patch.object(work, "spread_candidates", side_effect=lambda cs: sorted(cs, key=lambda c: c.pr)),
            patch.object(work, "log", side_effect=self.logged.append),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.roadmap_only = self.start(patch.object(work, "roadmap_only", return_value="GRH"))
        self.focus = self.start(patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=[]))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_round(self, rows, *, author_count=0, pick=None):
        """One round; `pick` fixes the weighted draw to that member. Returns the shared-state query."""
        self.calls = []
        self.draws = []

        def choices(population, weights):
            self.draws.append(dict(zip(population, weights, strict=True)))
            return [pick or population[0]]

        def perform(stage, _w, sv, candidate, _opts):
            self.calls.append((stage, candidate.pr, candidate.reason, sv))
            return 0

        with (
            patch(
                "tauceti_worker.github_state.read_survey",
                return_value=(True, SurveyRows(rows, {"science": author_count})),
            ) as query,
            patch.object(work, "dispatch", side_effect=perform),
            patch.object(work.random, "choices", side_effect=choices),
            # Fix-like stages are otherwise an age-weighted random permutation; pin oldest first.
            patch.object(work, "age_spread", side_effect=lambda cs: sorted(cs, key=lambda c: c.pr)),
        ):
            run_round(self.w, self.opts)
        return query

    def actionable(self, stage):
        return sorted(c.pr for c in self.calls[0][3].kind(stage).actionable)


class Rounds(RoundHarness):
    def test_each_round_authors_in_one_member_drawn_by_weight(self):
        self.focus.return_value = ["Topology"]
        self.run_round([])
        # The member whose roadmap has not merged is skipped silently, and the weights are the file's.
        self.assertEqual(self.draws, [{ANT: 4, RH: 1}])
        self.assertIn(f"ROADMAP area: {ANT} (umbrella GRH: picked from 2 of 3 members)", self.logged)
        stage, pr, area, sv = self.calls[0]
        self.assertEqual((stage, pr, area), ("roadmap", 0, ANT))
        self.assertEqual((sv.roadmap_only, sv.umbrella.area), ("GRH", ANT))
        self.focus.assert_not_called()  # an umbrella is an explicit choice: no Fleet focus

    def test_the_draw_is_weighted(self):
        draws = [umbrellas_draw(self.w) for _ in range(500)]
        self.assertGreater(draws.count(ANT), draws.count(RH) * 2)
        self.assertEqual(set(draws), {ANT, RH})

    def test_skipped_members_are_never_drawn_and_no_eligible_member_ends_the_round(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": ANT}):
            self.run_round([])
        self.assertEqual(self.draws, [{RH: 1}])
        self.assertIn(f"ROADMAP area: {RH} (umbrella GRH: picked from 1 of 3 members)", self.logged)
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": f"{ANT},{RH}"}),
            self.assertRaisesRegex(NoProgress, f"no member of umbrella GRH is eligible .*{LATER}") as raised,
        ):
            self.run_round([row(10, RH)])
        self.assertIn(f"in --roadmap-skip: {ANT}, {RH}", str(raised.exception))
        self.assertEqual(self.calls, [], "no unit, so no model, starts")

    def test_maintenance_reaches_every_member_area_not_only_the_drawn_one(self):
        rows = [row(20, RH), row(30, LATER), row(40, "Other"), row(50, "Topology", author="peer", blocking=False)]
        rows += [row(60, RH, author="peer", blocking=False)]
        query = self.run_round(rows, pick=ANT)
        # One scoped read per member, exactly what a worker pinned to each member asks the service.
        self.assertEqual([c.args[1:3] for c in query.call_args_list], [(ANT, []), (LATER, []), (RH, [])])
        self.assertEqual(self.calls[0][:2], ("fix", 20))
        self.assertEqual(self.actionable("fix"), [20, 30])
        self.assertEqual(self.actionable("review"), [60])
        self.assertEqual(sorted(p.number for p in self.calls[0][3].open_prs), [20, 30, 60])

    def test_a_skipped_member_leaves_maintenance_too(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": RH}):
            query = self.run_round([row(20, RH), row(10, ANT)])
        self.assertEqual(self.actionable("fix"), [10])
        self.assertEqual([c.args[1:3] for c in query.call_args_list], [(ANT, [RH]), (LATER, [RH])])

    def test_rounds_without_the_roadmap_phase_draw_nothing_but_maintain_every_member(self):
        self.opts.only = ["fix"]
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": f"{ANT},{RH}"}):
            self.run_round([row(30, LATER), row(40, "Other")])
        self.assertEqual(self.draws, [])
        self.assertEqual(self.calls[0][:2], ("fix", 30))

    def test_backpressure_counts_open_prs_across_the_member_areas(self):
        self.opts.only = ["roadmap"]
        rows = [row(10, ANT, blocking=False), row(20, RH, blocking=False), row(30, "Other", blocking=False)]
        with patch.dict(os.environ, {"TAUCETI_MAX_OPEN_PRS": "2"}):
            with self.assertRaisesRegex(NoProgress, "2 open PRs in selected scope"):
                self.run_round(rows)
            self.run_round(rows[1:])
        self.assertEqual(self.calls[0][:3], ("roadmap", 0, ANT))

    def test_explicit_pull_requests_override_the_umbrella(self):
        self.opts.prs = (40,)
        query = self.run_round([row(40, "Other"), row(20, RH)])
        self.assertEqual(self.calls[0][:2], ("fix", 40))
        self.assertEqual(query.call_args.args[1], "any")
        self.assertEqual(self.roadmap.reads, 0)

    def test_a_target_list_chooses_among_the_umbrella_areas_without_a_draw(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": "/dev/null/targets.md"}):
            self.run_round([])
        self.assertEqual(self.draws, [])
        stage, _, reason, sv = self.calls[0]
        self.assertEqual((stage, reason, sv.umbrella.area), ("roadmap", "GRH", None))
        self.assertEqual(sv.umbrella.scope, {ANT, RH, LATER}, "maintenance keeps every member")
        self.assertEqual(sv.umbrella.current, {ANT, RH}, "the list may only author where a roadmap exists")

    def test_a_target_list_with_no_member_that_is_an_area_ends_the_round_before_any_model(self):
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": "/dev/null/targets.md"}),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": f"{ANT},{RH}"}),
            self.assertRaisesRegex(
                NoProgress, f"no member of umbrella GRH is eligible .*not roadmap areas now: {LATER}"
            ),
        ):
            self.run_round([row(30, LATER)])
        self.assertEqual(self.calls, [])

    def test_source_material_needs_one_area(self):
        self.opts.source = "/srv/source"
        with self.assertRaisesRegex(Die, "--source needs one roadmap area, and GRH is an umbrella"):
            self.run_round([])
        self.assertEqual(self.calls, [])

    def test_a_broken_file_stops_the_round_before_any_model(self):
        (self.roadmap.root / "umbrellas" / "GRH.toml").write_text("[[area]]\nweight = 2\n")
        with self.assertRaisesRegex(Die, "umbrella GRH .* does not parse"):
            self.run_round([row(20, RH)])
        self.assertEqual(self.calls, [])

    def test_a_display_resolves_the_umbrella_without_drawing(self):
        scope = work.round_scope(self.w, SimpleNamespace(only=[], prs=(), source=None), display=True)
        self.assertEqual(scope, ("GRH", False, False))
        self.assertEqual((self.w.umbrella.area, self.w.umbrella.scope), (None, {ANT, RH, LATER}))
        (self.roadmap.root / "umbrellas" / "GRH.toml").write_text("not toml")
        self.assertEqual(work.round_scope(self.w, SimpleNamespace(only=[], prs=()), display=True), scope)
        self.assertIsNone(self.w.umbrella)


def umbrellas_draw(w):
    return work.random_umbrella_area(w, grh_round())


class Unchanged(RoundHarness):
    """No behaviour change for anyone who does not name an umbrella, even with umbrella files present."""

    def test_a_pinned_area_runs_the_existing_selection_path(self):
        self.roadmap_only.return_value = "Topology"
        with patch.object(work, "roadmap_areas") as areas:
            query = self.run_round([row(10, "Other"), row(20, "Topology"), row(30, RH)])
        self.assertEqual(query.call_args.args[1:3], ("Topology", []))
        self.assertEqual(self.calls[0][:2], ("fix", 20))
        self.assertEqual(self.actionable("fix"), [20])
        self.assertIsNone(self.calls[0][3].umbrella)
        areas.assert_not_called()
        self.assertEqual(self.draws, [])
        self.assertEqual(self.roadmap.reads, 1, "one snapshot read finds no umbrellas/Topology.toml")

    def test_an_unknown_name_still_names_one_area(self):
        self.roadmap_only.return_value = "Typo"
        query = self.run_round([])
        self.assertEqual(query.call_args.args[1], "Typo")
        self.assertEqual(self.calls[0][:3], ("roadmap", 0, "Typo"))

    def test_unpinned_and_all_areas_rounds_never_read_an_umbrella(self):
        for value in (None, "", "any", "auto"):
            with self.subTest(roadmap_only=value), patch.object(umbrellas, "_umbrella_file") as read:
                self.roadmap_only.return_value = value
                with patch.object(work, "roadmap_areas", return_value=["Topology"]):
                    self.run_round([row(20, RH)])
                read.assert_not_called()
                self.assertEqual(self.calls[0][:2], ("fix", 20))
                self.assertIsNone(self.calls[0][3].umbrella)

    def test_round_scope_returns_what_it_always_returned(self):
        opts = SimpleNamespace(only=["fix"], prs=())
        for value, expected in (
            ("Topology", ("Topology", False, False)),
            ("", ("", True, False)),
            ("any", ("any", True, False)),
        ):
            self.roadmap_only.return_value = value
            self.assertEqual(work.round_scope(self.w, opts), expected)
            self.assertIsNone(self.w.umbrella)
        self.assertEqual(work.round_scope(self.w, SimpleNamespace(only=["fix"], prs=(4,))), ("any", False, False))


class Prompt(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="roadmap-prompt-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.prompts = []
        self.claimed = []
        for item in (
            patch.dict(os.environ, {"TAUCETI_RESPECT_CLAIMS": "false", "TAUCETI_ROADMAP_SKIP": "Algebra"}),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": "", "TAUCETI_EXTRA_ROADMAP_PROMPT": ""}),
            patch.object(work, "ensure_fork", return_value="alice/TauCeti"),
            patch.object(work, "claims_repo", return_value="org/claims"),
            patch.object(
                work, "avoid_list", side_effect=lambda _gh, area, _respect: self.claimed.append(area) or "none"
            ),
            patch.object(
                work, "fetch_ref", side_effect=lambda repo, dest: Path(dest).mkdir(parents=True, exist_ok=True) or True
            ),
            patch.object(work, "prepare_checkout", return_value=True),
            patch.object(
                work, "run_agent_host", side_effect=lambda cwd, prompt, *a, **k: self.prompts.append(prompt) or 0
            ),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp, wid="worker3", checkout=self.tmp / "checkout", logdir=self.tmp / "logs")
        self.claims = SimpleNamespace(begin_target_work=Mock(return_value=0))
        self.w = SimpleNamespace(cfg=cfg, gh=None, claims=self.claims)
        self.roadmap = Roadmap(self).install(self)  # LATER is a member but not a roadmap area yet

    def author(self, reason, scope=None):
        sv = Survey(worker_id="worker3", roadmap_only=scope.umbrella.name if scope else reason)
        sv._umbrella = scope
        opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        work.do_roadmap(self.w, sv, SimpleNamespace(reason=reason, pr=0, head=""), opts, bubble=False)
        return self.prompts[-1]

    def test_a_drawn_member_with_sections_gets_one_preference_sentence(self):
        prompt = self.author(ANT, grh_round(ANT))
        self.assertIn(
            f"Start with `{ANT}`, the roadmap you were assigned. It was drawn from the umbrella `GRH`: Riemann "
            f"hypothesis for Dirichlet L-functions. Within `{ANT}`, prefer the targets of §1.6, §3.2, which the "
            f"umbrella's goal cites; if all of them are done or claimed, any other target of `{ANT}` is acceptable. "
            "If it is `any`, choose",
            prompt,
        )
        self.assertEqual(self.claimed, [ANT], "claims and intentions apply to the drawn area")
        self.assertEqual(prompt.count("the umbrella"), 2, "nothing else in the prompt changes")

    def test_a_member_without_sections_names_only_the_umbrella(self):
        prompt = self.author(RH, grh_round(RH))
        self.assertIn(
            f"Start with `{RH}`, the roadmap you were assigned. It was drawn from the umbrella `GRH`: Riemann "
            "hypothesis for Dirichlet L-functions. If it is `any`",
            prompt,
        )
        self.assertNotIn("prefer the targets", prompt)

    def test_otherwise_the_prompt_is_unchanged(self):
        plain = self.author("Topology")
        self.assertIn("Start with `Topology`, the roadmap you were assigned. If it is `any`, choose", plain)
        self.assertNotIn("the umbrella", plain)
        self.assertNotIn("__UMBRELLA__", plain)
        work.do_roadmap(
            self.w,
            None,
            SimpleNamespace(reason="Topology", pr=0, head=""),
            SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None),
            bubble=False,
        )
        self.assertEqual(self.prompts[-1], plain)
        with_umbrella = self.author(ANT, grh_round(ANT))
        sentence = re.search(r" It was drawn from the umbrella.*?acceptable\.", with_umbrella).group(0)
        self.assertEqual(with_umbrella.replace(sentence, "").replace(ANT, "Topology"), plain)

    @contextlib.contextmanager
    def target_list(self, *sections):
        """A target list with one open item per (area, slug), in file order, offered in file order."""
        targets = self.tmp / "targets.md"
        targets.write_text(
            "<!-- tauceti-targets:v1 -->\n" + "".join(f"## {area}\n- [ ] `{slug}` — item\n" for area, slug in sections)
        )
        self.w.gh = SimpleNamespace(open_prs=lambda: [], merged_prs=lambda *a, **k: [])
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(targets), "TAUCETI_ROADMAP_TARGETS_GITHUB": "0"}),
            patch.object(work, "live_targets", side_effect=lambda listed, *_: listed),
            patch.object(work, "_landed_link", return_value=None),
            patch.object(work, "_open_link", return_value=None),
            patch("tauceti_worker.targets.random.shuffle"),  # keep file order, so a wrong offer is claimed first
        ):
            yield

    def test_a_target_list_is_restricted_to_the_umbrella_areas(self):
        with self.target_list(("Other", "outside"), (RH, "inside")):
            prompt = self.author("GRH", grh_round())
            self.claims.begin_target_work.assert_called_once_with(RH, "inside")
            self.assertIn(f"Start with `{RH}`, the roadmap you were assigned. It is a member of the umbrella", prompt)
            self.assertNotIn("prefer the targets", prompt)
            with self.assertRaisesRegex(NoProgress, "offers nothing to author in GRH"):
                self.author("GRH", grh_round(skip=(RH,)))

    def test_a_target_list_offers_only_members_that_are_roadmap_areas_now(self):
        # LATER comes first in the file: offered, it would be claimed first.
        with self.target_list((LATER, "future"), (RH, "current")):
            for scope in (grh_round(current=(ANT, RH)), grh_round()):  # as round_scope leaves it, or read here
                self.claims.begin_target_work.reset_mock()
                prompt = self.author("GRH", scope)
                self.claims.begin_target_work.assert_called_once_with(RH, "current")
                self.assertIn(f"Start with `{RH}`, the roadmap you were assigned.", prompt)
        self.assertNotIn(LATER, prompt)

    def test_a_future_member_as_the_only_eligible_target_stops_before_any_claim_or_model(self):
        with (
            self.target_list((LATER, "future"), ("Other", "outside")),
            self.assertRaisesRegex(
                NoProgress,
                f"offers nothing to author in GRH .*only in areas outside GRH's current roadmap areas: {LATER}, Other",
            ),
        ):
            self.author("GRH", grh_round(current=(ANT, RH)))
        self.claims.begin_target_work.assert_not_called()
        self.assertEqual(self.prompts, [], "no model started")

    def test_with_the_fallback_a_list_with_nothing_in_the_umbrella_draws_a_member(self):
        with (
            self.target_list((LATER, "future"), ("Other", "outside")),
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_FALLBACK": "1"}),
        ):
            prompt = self.author("GRH", grh_round(current=(ANT, RH)))
        self.claims.begin_target_work.assert_not_called()
        self.assertNotIn("Your target is assigned", prompt)
        self.assertRegex(
            prompt, f"Start with `({ANT}|{RH})`, the roadmap you were assigned. It is a member of the umbrella"
        )


class Visibility(unittest.TestCase):
    def test_status_lines_and_the_dashboard_show_the_umbrella(self):
        sv = Survey(worker_id="w", roadmap_only="GRH", roadmap_skip=[RH])
        sv._umbrella = grh_round(skip=(RH,))
        self.assertEqual(_only_label(sv), "GRH (umbrella)")
        self.assertEqual(tui._roadmap_note(sv), ("∞", f"only: GRH (umbrella of 2 areas)  (skip: {RH})"))
        rows = dict(tui._expansion_rows(sv, "roadmap", {}, [ANT, RH, "Topology"]))
        self.assertEqual(rows[f"    └ {ANT}"], "← umbrella")
        self.assertEqual(rows[f"    └ {RH}"], "(skipped)")
        self.assertEqual(rows["    └ Topology"], "")
        # The dashboard's live dial moved to another area: that area is shown plainly until the next survey.
        sv.roadmap_only = "Topology"
        self.assertIsNone(sv.umbrella)
        self.assertEqual(_only_label(sv), "Topology")
        self.assertEqual(dict(tui._expansion_rows(sv, "roadmap", {}, ["Topology"]))["    └ Topology"], "← only")

    def test_the_runtime_target_names_the_umbrella(self):
        sv = Survey(worker_id="w", roadmap_only="GRH")
        sv._umbrella = grh_round(ANT)
        self.assertEqual(
            work.roadmap_round_target(sv, SimpleNamespace(reason=ANT)), f"new PR (area: {ANT}, umbrella GRH)"
        )
        sv._umbrella = grh_round()
        self.assertEqual(work.roadmap_round_target(sv, SimpleNamespace(reason="GRH")), "new PR (umbrella GRH)")
        self.assertEqual(work.roadmap_round_target(None, SimpleNamespace(reason="")), "new PR (area: any)")

    def test_a_status_survey_lists_the_members_work_with_no_new_payload_keys(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(wid="w", sbcache=Path(tmp.name) / "sb", store_dir=Path(tmp.name), state=Path(tmp.name))
        gh = SimpleNamespace(repo="org/repo")
        rows = [row(10, RH), row(20, "Other")]
        counters = SimpleNamespace(read=lambda key: 0)
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_ONLY": "GRH", "TAUCETI_ROADMAP_SKIP": ""}),
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://state.test"}),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch("tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows(rows, {}))) as query,
        ):
            Roadmap(self, files={"GRH": GRH}).install(self)
            shown = survey(
                cfg, gh, ReviewState(cfg, gh), counters, stages=STAGES, umbrella=umbrellas.display_umbrella(gh)
            )
            plain = survey(cfg, gh, ReviewState(cfg, gh), counters, stages=STAGES)
        self.assertEqual([c.args[1] for c in query.call_args_list[:3]], [ANT, LATER, RH])
        self.assertEqual((shown.roadmap_only, [p.number for p in shown.open_prs]), ("GRH", [10]))
        self.assertEqual(_only_label(shown), "GRH (umbrella)")
        self.assertEqual(dataclasses.asdict(shown).keys(), dataclasses.asdict(plain).keys())


class LiveSkip(unittest.TestCase):
    """The dashboard's skip dial changes roadmap_skip on a survey it already holds, then rescopes it
    (tui._apply_skip); tests/dashboard.py drives the dial itself and its refresh."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cfg = SimpleNamespace(wid="w", sbcache=root / "sb", store_dir=root, state=root)
        self.gh = SimpleNamespace(repo="org/repo")
        for item in (
            patch.dict(
                os.environ,
                {
                    "TAUCETI_ROADMAP_ONLY": "GRH",
                    "TAUCETI_ROADMAP_SKIP": "",
                    "TAUCETI_GITHUB_STATE_URL": "https://state.test",
                    "TAUCETI_MAX_OPEN_PRS": "2",
                },
            ),
            patch("tauceti_worker.survey.me", return_value="science"),
        ):
            item.start()
            self.addCleanup(item.stop)
        Roadmap(self, files={"GRH": GRH}).install(self)

    def survey(self, rows):
        counters = SimpleNamespace(read=lambda key: 0, write=lambda key, value: None)
        rs = ReviewState(self.cfg, self.gh)
        with patch("tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows(rows, {}))):
            return survey(self.cfg, self.gh, rs, counters, stages=STAGES, umbrella=umbrellas.display_umbrella(self.gh))

    def test_adding_and_removing_a_skip_rescopes_the_umbrella(self):
        sv = self.survey([row(10, ANT, blocking=False), row(20, RH, blocking=False)])
        self.assertEqual((sv.n_mine_open, sv.roadmap_backpressure), (2, True))
        sv.roadmap_skip = [RH]  # the dial adds RH
        sv.rescope_roadmap()
        self.assertEqual(sv.umbrella.scope, {ANT, LATER})
        self.assertEqual((sv.n_mine_open, sv.roadmap_backpressure), (1, False), "RH's pull request no longer counts")
        rows = dict(tui._expansion_rows(sv, "roadmap", {}, [ANT, RH]))
        self.assertEqual((rows[f"    └ {ANT}"], rows[f"    └ {RH}"]), ("← umbrella", "(skipped)"))
        self.assertEqual(tui._roadmap_note(sv), ("∞", f"only: GRH (umbrella of 2 areas)  (skip: {RH})"))
        sv.roadmap_skip = []  # the dial removes it again
        sv.rescope_roadmap()
        self.assertEqual(sv.umbrella.scope, {ANT, RH, LATER})
        self.assertEqual(dict(tui._expansion_rows(sv, "roadmap", {}, [RH]))[f"    └ {RH}"], "← umbrella")
        self.assertEqual(sv.n_mine_open, 2, "this survey still holds RH's pull request")

    def test_a_member_skipped_at_the_survey_needs_a_fresh_survey_to_count_again(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_SKIP": RH}):
            sv = self.survey([row(10, ANT, blocking=False), row(20, RH, blocking=False)])
        self.assertEqual(sv.n_mine_open, 1)
        sv.roadmap_skip = []
        sv.rescope_roadmap()
        # The scope widens at once; RH's pull request was never surveyed, which is why the dashboard
        # refreshes after a widening skip change.
        self.assertEqual((sv.umbrella.scope, sv.n_mine_open), ({ANT, RH, LATER}, 1))
        self.assertEqual(self.survey([row(10, ANT, blocking=False), row(20, RH, blocking=False)]).n_mine_open, 2)

    def test_the_umbrella_count_honours_the_skip_list_it_is_given(self):
        prs = [PRInfo.from_json(row(n, area)["pr"]) for n, area in ((1, ANT), (2, RH), (3, "Other"))]
        self.assertEqual(roadmap_open_count(prs, "GRH", [], within={ANT, RH}), 2)
        self.assertEqual(roadmap_open_count(prs, "GRH", [RH], within={ANT, RH}), 1)


class Docs(unittest.TestCase):
    def test_the_flag_help_readme_and_reference_describe_umbrellas(self):
        probe = argparse.ArgumentParser(prog="probe")
        cli.add_work_flags(probe)
        text = " ".join(probe.format_help().split())
        self.assertIn("Or an umbrella, a named set of areas", text)
        self.assertIn("docs/umbrellas.md", text)
        readme = (REPO / "README.md").read_text()
        reference = (REPO / "docs" / "reference.md").read_text()
        self.assertIn("tauceti work --loop --full-quality --roadmap-only GRH", readme)
        self.assertIn("(docs/umbrellas.md)", readme)
        self.assertIn("[umbrella](umbrellas.md)", reference)


if __name__ == "__main__":
    unittest.main()

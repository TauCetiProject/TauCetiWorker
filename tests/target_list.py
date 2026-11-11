#!/usr/bin/env python3
"""--roadmap-targets: the list format, live statuses, selection, the claim, the prompt and the wiring,
without GitHub or model access. The fixture is the example in docs/target-lists.md."""

import contextlib
import importlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_tools.forge import ForgeError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import targets as t
from tauceti_worker import worker_manager as wm
from tauceti_worker.github_state import SurveyRows

work = importlib.import_module("tauceti_worker.work_units")
EXAMPLE = re.search(r"```markdown\n(.*?)```", (REPO / "docs" / "target-lists.md").read_text(), re.S).group(1)


def marker(area, slug, **extra):
    return f"<!--tauceti-target:v1 {json.dumps({'focus': area, 'id': slug, **extra})}-->"


def github(open_bodies=(), merged=(), older=()):
    """`merged` is what the recent listing returns; the search for one slug also finds `older`."""

    def merged_prs(search, limit):
        rows = merged if "sort:updated-desc" in search else [*merged, *older]
        return [{"number": n, "body": body} for n, body in rows]

    return SimpleNamespace(
        open_prs=lambda: [{"number": 900 + i, "body": body} for i, body in enumerate(open_bodies)],
        merged_prs=merged_prs,
    )


def status(targets):
    return {it.slug: it.status for it in targets.items()}


class Format(unittest.TestCase):
    def test_documented_example(self):
        parsed = t.parse_targets(EXAMPLE)
        self.assertEqual(list(parsed.areas), ["MeasureTheory", "Probability"])
        self.assertEqual(
            status(parsed),
            {
                "borel-cantelli": "done",
                "kolmogorov-zero-one": "open",
                "truncation-bounds": "inflight",
                "strong-law": "open",
            },
        )
        law = parsed.find("strong-law")
        self.assertEqual((law.area, law.text), ("Probability", "the strong law"))
        self.assertEqual(law.needs, ("borel-cantelli", "truncation-bounds"))
        self.assertEqual(law.meta, ("serves: G1",))
        self.assertEqual(parsed.find("truncation-bounds").needs, ())
        self.assertTrue(parsed.preamble.startswith("Goal: the strong law"))
        self.assertIsNone(parsed.find("second-moment-route"))

    def test_unmarked_file_is_refused_and_bad_lines_are_skipped(self):
        with self.assertRaisesRegex(tc.Die, "tauceti-targets:v1"):
            t.parse_targets("## Algebra\n- [ ] `a` — text\n")
        with patch.object(t, "log") as log:
            parsed = t.parse_targets(
                "<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] no slug\n- [ ] `a` - b (needs: c, `d`)\n"
            )
        self.assertEqual([(it.slug, it.needs) for it in parsed.items()], [("a", ("c", "d"))])
        self.assertIn("1 malformed item line(s), first at line 3", log.call_args_list[0].args[0])
        self.assertIn("undefined slug(s), which block nothing: c, d", log.call_args_list[1].args[0])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(tc.Die, "roadmap targets: .*absent.md"):
            t.load_targets(Path(tmp) / "absent.md")


class LiveView(unittest.TestCase):
    def test_markers(self):
        body = (
            marker("A", "x")
            + marker("A", "y", partial=True)
            + '<!--tauceti-target:v1 {"focus":"A"}-->'
            + "<!--tauceti-target:v1 {bad}-->"
        )
        self.assertEqual(t.marker_keys(body), ({("A", "x"), ("A", "y")}, {("A", "y")}))
        self.assertEqual(t.marker_keys(None), (set(), set()))

    def test_open_and_merged_pull_requests_move_items_forward(self):
        listed = t.parse_targets(EXAMPLE)
        live = t.live_targets(listed, github([marker("MeasureTheory", "kolmogorov-zero-one", partial=True)]))
        self.assertEqual(status(live)["kolmogorov-zero-one"], "inflight")
        merged = [
            (7, marker("Probability", "truncation-bounds")),
            (8, marker("Probability", "strong-law", partial=True)),
        ]
        live = t.live_targets(listed, github(merged=merged))
        self.assertEqual((status(live)["truncation-bounds"], status(live)["strong-law"]), ("done", "open"))
        self.assertEqual(
            status(t.live_targets(listed, github(merged=[(9, marker("Elsewhere", "strong-law"))]))), status(listed)
        )
        excused = t.parse_targets(EXAMPLE.replace("in flight: #1234", "partial: #7, #12"))
        self.assertEqual(status(t.live_targets(excused, github(merged=merged)))["truncation-bounds"], "inflight")
        self.assertEqual(status(t.live_targets(listed, github()))["borel-cantelli"], "done")

    def test_bors_landings_count_as_merged(self):
        rows = [
            {"number": 1, "state": "MERGED", "title": "feat: a", "body": ""},
            {"number": 2, "state": "CLOSED", "title": "[Merged by Bors] - feat: b", "body": ""},
            {"number": 3, "state": "CLOSED", "title": "feat: c", "body": ""},
        ]
        gh = tc.GitHub("org/repo")
        gh._gh = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr=""))
        with patch.dict(os.environ, {"TAUCETI_GITHUB_CACHE_BYPASS": "1"}):
            self.assertEqual([d["number"] for d in gh.merged_prs(t.MERGED_SEARCH, limit=50)], [1, 2])
            argv = gh._gh.call_args.args[0]
            self.assertEqual(argv[argv.index("--state") + 1], "closed")
            self.assertEqual(argv[argv.index("--limit") + 1], "50")
            gh._gh.return_value = SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")
            with self.assertRaisesRegex(tc.GitHubError, "HTTP 502"):
                gh.merged_prs(t.MERGED_SEARCH)


class Selection(unittest.TestCase):
    def setUp(self):
        self.listed = t.parse_targets(EXAMPLE)

    def slugs(self, targets, only="auto", skip=()):
        return sorted(it.slug for it in t.candidates(targets, only, skip))

    def test_needs_gate_eligibility(self):
        self.assertEqual(self.slugs(self.listed), ["kolmogorov-zero-one"])
        landed = t.overlay_live(self.listed, set(), {("Probability", "truncation-bounds")})
        self.assertEqual(self.slugs(landed), ["kolmogorov-zero-one", "strong-law"])
        self.assertEqual(self.slugs(landed, only="Probability", skip=["Probability"]), ["strong-law"])
        self.assertEqual(self.slugs(landed, only="any", skip=["MeasureTheory"]), ["strong-law"])
        undefined = t.parse_targets("<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `a` — b (needs: `nowhere`)\n")
        self.assertEqual(self.slugs(undefined), ["a"])
        mixed = t.parse_targets(
            "<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `a` — open\n- [x] `b` — done\n- [ ] `c` — x (needs: a, `b`)\n"
        )
        self.assertEqual(mixed.find("c").needs, ("a", "b"))
        self.assertEqual(self.slugs(mixed), ["a"])

    def test_an_empty_offer_says_why(self):
        for only, skip, why in (
            ("Probability", [], r"in Probability \(1 open item\(s\) wait on needs"),
            ("auto", ["MeasureTheory"], "every area with an eligible item is in --roadmap-skip: MeasureTheory"),
            ("Topology", [], r"in Topology \(no open items\)"),
        ):
            with self.assertRaisesRegex(tc.NoProgress, why):
                t.candidates(self.listed, only, skip)

    def test_file_order_within_an_area(self):
        text = "<!-- tauceti-targets:v1 -->\n## Algebra\n" + "".join(f"- [ ] `s{i}` — item\n" for i in range(12))
        self.assertEqual(
            [it.slug for it in t.candidates(t.parse_targets(text), "auto", [])], [f"s{i}" for i in range(12)]
        )


class Round(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="target-list-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.path = self.tmp / "targets.md"
        self.path.write_text(EXAMPLE)
        self.status = self.tmp / "status.json"
        env = {
            "TAUCETI_ROADMAP_TARGETS": str(self.path),
            "TAUCETI_RUNTIME_STATUS": str(self.status),
            "TAUCETI_RESPECT_CLAIMS": "false",
            "TAUCETI_ROADMAP_SKIP": "",
        }
        self.prompts = []
        for item in (
            patch.dict(os.environ, env),
            patch.object(work, "ensure_fork", return_value="alice/TauCeti"),
            patch.object(work, "avoid_list", return_value="none"),
            patch.object(
                work, "fetch_ref", side_effect=lambda repo, dest: Path(dest).mkdir(parents=True, exist_ok=True) or True
            ),
            patch.object(work, "prepare_checkout", return_value=True),
            patch.object(work, "claims_repo", return_value="org/claims"),
            patch.object(
                work, "run_agent_host", side_effect=lambda cwd, prompt, *a, **k: self.prompts.append(prompt) or 0
            ),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.claims = SimpleNamespace(begin_target_work=Mock(return_value=0))
        cfg = SimpleNamespace(state=self.tmp, wid="worker3", checkout=self.tmp / "checkout", logdir=self.tmp / "logs")
        self.w = SimpleNamespace(cfg=cfg, gh=github(), claims=self.claims)

    def author(self, area="auto"):
        opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        work.do_roadmap(self.w, None, SimpleNamespace(reason=area, pr=0, head=""), opts, bubble=False)
        return self.prompts[-1]

    def test_the_round_claims_one_item_and_assigns_it(self):
        prompt = self.author()
        self.claims.begin_target_work.assert_called_once_with("MeasureTheory", "kolmogorov-zero-one")
        self.assertIn("- **Your target is assigned: `kolmogorov-zero-one` in `MeasureTheory`.**", prompt)
        self.assertIn('exactly `<!--tauceti-target:v1 {"focus":"MeasureTheory","id":"kolmogorov-zero-one"}-->`', prompt)
        self.assertIn('{"focus":"MeasureTheory","id":"kolmogorov-zero-one","partial":true}', prompt)
        self.assertIn(
            "  - [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; needs: `borel-cantelli` [x])",
            prompt,
        )
        self.assertIn("  Goal: the strong law for i.i.d. integrable variables.", prompt)
        self.assertNotIn("truncation-bounds", prompt)
        self.assertLess(prompt.index("## Choose a target"), prompt.index("Your target is assigned"))
        self.assertLess(
            prompt.index("Your target is assigned"),
            prompt.index("- **Choose a concrete target.** Start with `MeasureTheory`"),
        )
        self.assertEqual(json.loads(self.status.read_text())["roadmap_target"], "MeasureTheory/kolmogorov-zero-one")
        required = os.environ["TAUCETI_TARGET_MARKER"]
        complete, partial = required.splitlines()
        self.assertEqual(prompt.count(complete), 1)
        self.assertEqual(prompt.count(partial), 1)
        self.assertEqual(t.marker_keys(complete), ({("MeasureTheory", "kolmogorov-zero-one")}, set()))
        self.assertEqual(t.marker_keys(partial)[1], {("MeasureTheory", "kolmogorov-zero-one")})
        bodies = {
            f"Adds the law.\n\n{complete}\n": 0,
            partial: 0,
            complete.replace("zero-one", "zero-two"): 1,
            complete[: -len("}-->")]: 1,
            complete[: -len("-->")]: 1,
            partial[: -len("}-->")]: 1,
        }
        self.assertEqual({body: self.wrapper(required, body) for body in bodies}, bodies)

    def wrapper(self, required, body):
        """gh-safe-pr-create's verdict on a pull request body, with `true` standing in for gh."""
        if not (self.tmp / "bin").exists():
            (self.tmp / "bin").mkdir()
            (self.tmp / "bin" / "gh").symlink_to(shutil.which("true"))
        env = {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "TAUCETI_TARGET_MARKER": required}
        script = REPO / "scripts" / "gh-safe-pr-create"
        return subprocess.run([script, "--body", body], env=env, capture_output=True).returncode

    def test_the_markers_reach_the_wrapper_in_a_bubble_round(self):
        """The two-line TAUCETI_TARGET_MARKER through the Bubble command string: built by run_in_bubble's
        echo path, then sent the way Bubble sends `--command`, as one `bash -lc` string over ssh."""
        self.author()
        complete, partial = os.environ["TAUCETI_TARGET_MARKER"].splitlines()
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").symlink_to(shutil.which("true"))
        script = REPO / "scripts" / "gh-safe-pr-create"
        inner = f'env PATH={self.tmp / "bin"}:"$PATH" {shlex.quote(str(script))} --body "$TCW_BODY"'
        cfg = SimpleNamespace(state=self.tmp / "state", home=self.tmp / "home", wid="w", logdir=self.tmp / "logs")
        out = io.StringIO()
        with (
            patch.dict(os.environ, TAUCETI_AGENT_ECHO="1"),
            patch.object(tc.agents, "ensure_bubble_home", lambda _cfg: dict(os.environ)),
            patch.object(tc.agents, "_bubble_pop", lambda _cfg, _env: None),
            contextlib.redirect_stdout(out),
        ):
            rc = tc.run_in_bubble(
                SimpleNamespace(cfg=cfg), tc.TAUCETI, "", SimpleNamespace(work_model="claude"), inner_cmd=inner
            )
        self.assertEqual(rc, 0)
        argv = shlex.split(out.getvalue().removeprefix("BUBBLE "))
        command = argv[argv.index("--command") + 1]
        self.assertIn("TAUCETI_TARGET_MARKER=", command)
        remote = shlex.join(["bash", "-lc", f"cd {shlex.quote(str(self.tmp))} && exec {command}"])
        env = {k: v for k, v in os.environ.items() if k != "TAUCETI_TARGET_MARKER"} | {"HOME": str(self.tmp)}
        verdicts = {
            body: subprocess.run(["bash", "-c", remote], env=env | {"TCW_BODY": body}, capture_output=True).returncode
            for body in (complete, partial, complete[: -len("}-->")])
        }
        self.assertEqual(list(verdicts.values()), [0, 0, 1])

    def test_live_statuses_reach_the_prompt(self):
        self.w.gh = github(merged=[(7, marker("Probability", "truncation-bounds"))])
        prompt = self.author("Probability")
        self.assertIn("- [x] `truncation-bounds`", prompt)
        self.assertIn("needs: `borel-cantelli` [x], `truncation-bounds` [x])", prompt)

    def test_claims_held_elsewhere_are_passed_over(self):
        self.path.write_text(
            "<!-- tauceti-targets:v1 -->\n## Algebra\n" + "".join(f"- [ ] `s{i}` — item\n" for i in range(12))
        )
        self.claims.begin_target_work.side_effect = [1, 1, 2]
        self.assertIn("Your target is assigned: `s2` in `Algebra`", self.author())
        self.claims.begin_target_work.side_effect = None
        self.claims.begin_target_work.reset_mock(return_value=True)
        self.claims.begin_target_work.return_value = 1
        with self.assertRaisesRegex(tc.NoProgress, r"none of the 8 eligible target\(s\) of targets.md tried"):
            self.author()
        self.assertEqual(self.claims.begin_target_work.call_count, work.MAX_TARGET_TRIES)

    def test_an_item_that_landed_long_ago_is_not_assigned_again(self):
        self.path.write_text("<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `old` — item\n- [ ] `new` — item\n")
        self.w.gh = github(older=[(41, marker("Algebra", "old")), (42, marker("Algebra", "new", partial=True))])
        with patch.object(work, "log") as log:
            self.assertIn("Your target is assigned: `new` in `Algebra`", self.author())
        self.assertIn("target Algebra/old landed in #41; mark it `[x]` in targets.md", log.call_args_list[0].args[0])
        self.claims.begin_target_work.assert_called_once_with("Algebra", "new")

    def test_nothing_is_authored_without_an_eligible_item_or_a_live_view(self):
        with self.assertRaisesRegex(tc.NoProgress, "offers nothing to author in Probability"):
            self.author("Probability")

        def unreadable():
            raise tc.GitHubError("HTTP 504")

        self.w.gh.open_prs = unreadable
        with self.assertRaisesRegex(tc.NoProgress, "cannot read pull requests for the target list"):
            self.author()
        self.assertEqual(self.prompts, [])
        self.claims.begin_target_work.assert_not_called()

    def test_without_a_list_the_prompt_is_unchanged(self):
        os.environ["TAUCETI_ROADMAP_TARGETS"] = ""
        prompt = self.author("Topology")
        self.assertIn("## Choose a target\n- **Choose a concrete target.** Start with `Topology`", prompt)
        self.assertNotIn("__TARGET_LIST__", prompt)
        self.claims.begin_target_work.assert_not_called()

    def test_the_list_chooses_the_area_of_an_unpinned_round(self):
        opts = work.RoundOpts(only=["roadmap"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)
        cfg = SimpleNamespace(wid="worker3", sbcache=self.tmp / "sb", store_dir=self.tmp / "review", state=self.tmp)
        w = SimpleNamespace(
            cfg=cfg, gh=SimpleNamespace(repo="org/repo"), claims=SimpleNamespace(), rs=None, counters=None
        )
        seen = []
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://state.test"}),
            patch.object(work, "roadmap_only", return_value=None),
            patch.object(work, "mirror_creds"),
            patch("tauceti_worker.survey.me", return_value="science"),
            patch("tauceti_worker.github_state.read_survey", return_value=(True, SurveyRows([], {"science": 0}))),
            patch("tauceti_worker.fleet_capacity.roadmap_focus", return_value=["Topology"]) as focus,
            patch.object(work, "roadmap_areas", return_value=["Topology"]),
            patch.object(
                work, "dispatch", side_effect=lambda stage, _w, _sv, c, _o: seen.append((stage, c.reason)) or 0
            ),
        ):
            work.run_round(w, opts)
            focus.assert_not_called()
            os.environ["TAUCETI_ROADMAP_TARGETS"] = ""
            work.run_round(w, opts)
        self.assertEqual(seen, [("roadmap", "auto"), ("roadmap", "Topology")])


class MaintenanceOrder(unittest.TestCase):
    BASE = "rebase 9, rebase 1, progress 0, fix-ci 2, fix 3, fix 4, review 50, review 1, review 60, review 4, review 2"
    LISTED = (
        "rebase 9, fix-ci 2, progress 0, fix 4, rebase 1, fix 3, review 50, review 4, review 60, review 2, review 1"
    )

    def survey(self):
        """The account `science` has #1-#4 open, `bob` #50 and #60, and #9 is an automatic bump."""

        def pr(number, author="science", ref="feature", bot=False):
            return tc.PRInfo(
                number, "a" * 40, ref, "TauCetiProject", "TauCeti", False, "MERGEABLE", author, True, False, bot
            )

        def work_on(*numbers):
            return [tc.Candidate(n, "a" * 40 if n else "") for n in numbers]

        sv = tc.Survey(worker_id="worker3", me_login="science")
        sv.open_prs = [pr(1), pr(2), pr(3), pr(4), pr(50, "bob"), pr(60, "bob"), pr(9, "bot", "bump-mathlib/x", True)]
        sv.rebaseable.actionable = work_on(1, 9)
        sv.progress.actionable = work_on(0)
        sv.red_ci.actionable = work_on(2)
        sv.needs_fix.actionable = work_on(3, 4)
        sv.reviewable.actionable = work_on(50, 1, 60, 4, 2)
        return sv

    def order(self, pairs):
        return ", ".join(f"{stage} {c.pr}" for stage, c in pairs)

    def test_marked_pull_requests(self):
        bodies = [
            marker("Probability", "strong-law"),
            "Part one.\n" + marker("MeasureTheory", "kolmogorov-zero-one", partial=True),
            marker("Probability", "not-in-the-list"),
            marker("Topology", "strong-law"),
            None,
        ]
        prs = [{"number": n, "body": body} for n, body in enumerate(bodies, 1)]
        self.assertEqual(t.marked_prs(t.parse_targets(EXAMPLE), prs), {1, 2})

    def test_listed_work_takes_the_first_of_the_accounts_own_positions(self):
        sv = self.survey()
        self.assertEqual(self.order(tc.cascade(sv, tc.AUTO_STAGES)), self.BASE)
        self.assertEqual(self.order(tc.cascade(sv, tc.AUTO_STAGES, {2, 4, 60})), self.LISTED)
        self.assertEqual(self.order(tc.cascade(sv, tc.AUTO_STAGES, {60, 9})), self.BASE)
        self.assertEqual(
            self.order(tc.cascade(sv, ["fix", "review"], {4})),
            "fix 4, fix 3, review 50, review 4, review 60, review 1, review 2",
        )

    def offered(self, gh, sv=None):
        """The order in which one round offers its candidates, every one of them declined."""
        seen = []
        opts = work.RoundOpts(
            only=["rebase", "progress", "fix-ci", "fix", "review"],
            agent="claude",
            work_model="claude",
            sandbox_host=True,
            dry_run=True,
        )
        w = SimpleNamespace(cfg=SimpleNamespace(wid="worker3"), gh=gh, rs=None, counters=None)
        with (
            patch.object(work, "roadmap_only", return_value="any"),
            patch.object(work, "survey", return_value=sv or self.survey()),
            patch.object(work, "spread_candidates", side_effect=list),
            patch.object(work, "prioritize_review_candidates", side_effect=lambda queue, *a, **k: (list(queue), [])),
            patch.object(work, "dispatch", side_effect=lambda stage, _w, _sv, c, _o: seen.append((stage, c))),
            patch.object(work, "log") as log,
            self.assertRaisesRegex(tc.NoProgress, "no eligible work"),
        ):
            work.run_round(w, opts)
        return self.order(seen), [call.args[0] for call in log.call_args_list if call.args[0].startswith("target list")]

    def test_a_round_reads_the_list_and_the_markers(self):
        bodies = {2: marker("Probability", "strong-law"), 4: marker("MeasureTheory", "borel-cantelli", partial=True)}
        bodies[60] = marker("Probability", "truncation-bounds")
        gh = SimpleNamespace(
            open_prs=Mock(return_value=[{"number": n, "body": bodies.get(n, "")} for n in range(1, 61)])
        )
        with tempfile.TemporaryDirectory(prefix="target-list-") as tmp, patch.dict(os.environ):
            os.environ["TAUCETI_ROADMAP_TARGETS"] = str(Path(tmp, "targets.md"))
            Path(tmp, "targets.md").write_text(EXAMPLE)
            self.assertEqual(
                self.offered(gh), (self.LISTED, ["target list: #2, #4 before this account's other pull requests"])
            )
            gh.open_prs.side_effect = tc.GitHubError("HTTP 504")
            order, logged = self.offered(gh)
            self.assertEqual(order, self.BASE)
            self.assertIn("usual order this round (HTTP 504)", logged[0])
            gh.open_prs.side_effect = ForgeError("source read failed")
            self.assertEqual(self.offered(gh)[0], self.BASE)
            Path(tmp, "targets.md").unlink()
            order, logged = self.offered(gh)
            self.assertEqual(order, self.BASE)
            self.assertIn("usual order this round (roadmap targets: ", logged[0])
            gh.open_prs.reset_mock()
            alone = self.survey()
            alone.red_ci.actionable, alone.needs_fix.actionable = [], []
            alone.reviewable.actionable = alone.reviewable.actionable[:2]
            self.assertEqual(self.offered(gh, alone), ("rebase 9, rebase 1, progress 0, review 50, review 1", []))
            os.environ["TAUCETI_ROADMAP_TARGETS"] = ""
            self.assertEqual(self.offered(gh), (self.BASE, []))
            gh.open_prs.assert_not_called()


class Wiring(unittest.TestCase):
    def test_setting_and_flag(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": " "}):
            self.assertIsNone(tc.roadmap_targets())
            os.environ["TAUCETI_ROADMAP_TARGETS"] = "/srv/targets.md"
            self.assertEqual(tc.roadmap_targets(), Path("/srv/targets.md"))
        parser = tc.build_parser()
        self.assertEqual(parser.parse_args(["work", "--roadmap-targets", "t.md"]).roadmap_targets, "t.md")
        self.assertEqual(parser.parse_args(["_round", "--roadmap-targets", ""]).roadmap_targets, "")
        self.assertIsNone(parser.parse_args(["work"]).roadmap_targets)

    def test_claim(self):
        cleanups = []
        claims = tc.Claims(SimpleNamespace(wid="worker3"), SimpleNamespace(add_cleanup=cleanups.append))
        with (
            patch("tauceti_worker.round.claims_repo", return_value="org/claims"),
            patch("tauceti_worker.round.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run,
            patch.object(tc.Claims, "start_heartbeat") as heartbeat,
        ):
            self.assertEqual(claims.begin_target_work("Algebra", "a"), 1)
            self.assertEqual(run.call_args.args[0][1:3], ["acquire", "author/Algebra/a"])
            self.assertEqual(run.call_args.kwargs["env"]["CLAIM_REPO"], "org/claims")
            self.assertIsNone(claims.held)
            heartbeat.assert_not_called()
            run.return_value = SimpleNamespace(returncode=0)
            self.assertEqual(claims.begin_target_work("Algebra", "a"), 0)
            self.assertEqual(claims.held, ("author/Algebra/a", "org/claims"))
            heartbeat.assert_called_once_with("author/Algebra/a", "org/claims")
            self.assertEqual(cleanups, [claims.release])

    def test_workers_toml_key(self):
        spec = wm.WorkerSpec.from_dict({"id": "w", "only": ["roadmap"], "roadmap_targets": "/srv/targets.md"}, 0)
        self.assertEqual(spec.as_dict()["roadmap_targets"], "/srv/targets.md")
        argv = spec.work_argv()
        self.assertEqual(argv[argv.index("--roadmap-targets") + 1], "/srv/targets.md")
        self.assertEqual(tc.build_parser().parse_args(argv[3:]).roadmap_targets, "/srv/targets.md")
        self.assertNotIn("--roadmap-targets", wm.WorkerSpec(id="plain").work_argv())
        self.assertNotIn("roadmap_targets", wm.WorkerSpec(id="plain").as_dict())
        self.assertNotEqual(spec.fingerprint(), wm.WorkerSpec(id="w", only=("roadmap",)).fingerprint())
        with self.assertRaisesRegex(wm.WorkersError, r"workers\[0\].roadmap_targets must be an absolute path"):
            wm.WorkerSpec.from_dict({"id": "w", "roadmap_targets": "targets.md"}, 0)
        lines = wm._worker_configuration_lines({"spec": spec.as_dict()}, 100)
        self.assertTrue(any("targets: /srv/targets.md" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()

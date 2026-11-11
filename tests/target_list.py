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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import targets as t
from tauceti_worker import worker_manager as wm
from tauceti_worker.github_state import SurveyRows

work = importlib.import_module("tauceti_worker.work_units")
TARGET_MARKER_PRS = tc.GitHub.target_marker_prs  # unpatched, for the one test of the listing itself
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

    def test_a_round_that_also_maintains_forgejo_authors_from_the_list(self):
        self.w.cross_source_targets = [{"source": "forgejo", "pr": 5, "head": "a" * 40, "targets": []}]
        with patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both", "TAUCETI_NEW_PR_FORGE": "github"}):
            prompt = self.author()
        self.assertIn("Your target is assigned: `kolmogorov-zero-one` in `MeasureTheory`", prompt)
        self.assertIn("source-targets.json", prompt)
        self.claims.begin_target_work.assert_called_once_with("MeasureTheory", "kolmogorov-zero-one")

    def test_open_pull_requests_on_the_other_forge_put_items_in_flight(self):
        from tauceti_worker.survey import work_target_keys

        self.path.write_text("<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `first` — a\n- [ ] `second` — b\n")
        targets = work_target_keys(marker("Algebra", "first"))
        self.w.cross_source_targets = [{"source": "forgejo", "pr": 5, "head": "a" * 40, "targets": targets}]
        for _ in range(2):
            self.assertIn("Your target is assigned: `second` in `Algebra`", self.author())
        self.assertIn("  - [~] `first` — a", self.prompts[-1])
        self.claims.begin_target_work.assert_called_with("Algebra", "second")
        self.assertEqual(t.inventory_keys([{"targets": [*targets, "junk", '["x"]']}, {}]), {("Algebra", "first")})

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


class Forgejo(unittest.TestCase):
    """A list on a Forgejo source, built the way upstream's Forgejo tests build one: a real
    ForgejoWorkerSource over a mocked native API."""

    def setUp(self):
        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/project")
        self.api = Mock(forge=self.repo.forge, _token="fixture-token", max_bytes=1024 * 1024, max_pages=100)
        self.source = ForgejoWorkerSource(RepositoryIdentity(self.repo, 7), self.api, token_file=None)
        self.closed = []  # the closed-pull listing's pages; `found` is the keyword search's
        self.found = []
        self.api.pages.side_effect = lambda path: iter(self.closed if "/pulls?" in path else self.found)
        self.source.open_prs = Mock(return_value=[])

    @staticmethod
    def pull(number, body, merged=True):
        return {"number": number, "title": "t", "body": body, "state": "closed", "merged": merged}

    def test_merged_listing_and_keyword_search(self):
        self.closed = [
            [self.pull(1, "none"), self.pull(2, "none"), self.pull(3, marker("Algebra", "a"), merged=False)],
            [self.pull(4, marker("Algebra", "a")), self.pull(5, marker("Algebra", "b"))],
        ]
        recent = self.source.merged_prs(f"{t.MERGED_SEARCH} sort:updated-desc", limit=1)
        self.assertEqual([(d["number"], d["url"]) for d in recent], [(4, self.repo.pull_url(4))])
        self.assertEqual(self.api.pages.call_args.args[0], "repos/one/project/pulls?state=closed")
        self.api.max_pages = 1
        self.assertEqual(self.source.merged_prs(f"{t.MERGED_SEARCH} sort:created", limit=200), [])
        self.api.max_pages = 100
        item = t.TargetItem("Algebra", "a", "open", "x")
        landed = t.target_markers(item)[0]
        self.found = [
            [
                {"number": 8, "body": landed, "pull_request": {"merged": True}},
                {"number": 9, "body": landed, "pull_request": {"merged": False}},
                {"number": 10, "body": marker("Algebra", "ab"), "pull_request": {"merged": True}},
            ]
        ]
        self.assertEqual(t.landed_pr(item, self.source), 8)
        path = self.api.pages.call_args.args[0]
        self.assertTrue(path.startswith("repos/one/project/issues?type=pulls&state=closed&q="), path)
        from tauceti_tools.forge import ForgeError

        from tauceti_worker.config import SourceUnavailable

        self.api.pages.side_effect = ForgeError("down")
        with self.assertRaises(SourceUnavailable):
            self.source.merged_prs(f"{t.MERGED_SEARCH} sort:updated", limit=200)

    def test_partial_clause_names_a_forgejo_pull_request_by_url(self):
        url = self.repo.pull_url(5)
        listed = t.parse_targets(f"<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `a` — x (partial: #5, {url})\n")
        item = listed.find("a")
        self.assertEqual(t.landed([item], [{"number": 5, "body": marker("Algebra", "a"), "url": url}]), {})
        self.assertEqual(t.landed([item], [{"number": 5, "body": marker("Algebra", "a")}]), {})
        other = {"number": 5, "body": marker("Algebra", "a"), "url": self.repo.pull_url(6).replace("/6", "/5x")}
        self.assertEqual(t.landed([item], [other]), {("Algebra", "a"): 5})

    def test_author_claims_stay_in_the_shared_github_namespace(self):
        from tauceti_worker.source_claims import SourceClaims

        entered = []

        @contextlib.contextmanager
        def shared():
            entered.append(True)
            yield

        cleanups = []
        claims = SourceClaims(SimpleNamespace(wid="w1"), SimpleNamespace(add_cleanup=cleanups.append), self.source)
        with (
            patch("tauceti_worker.worker_sources.shared_github_environment", shared),
            patch("tauceti_worker.round.claims_repo", return_value="org/claims"),
            patch("tauceti_worker.round.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
            patch.object(SourceClaims, "start_heartbeat"),
            patch.object(SourceClaims, "stop_heartbeat"),
        ):
            self.assertEqual(claims.begin_target_work("Algebra", "a"), 0)
            for cleanup in cleanups:
                cleanup()
        self.assertEqual(
            [c.args[0][1:3] for c in run.call_args_list],
            [["acquire", "author/Algebra/a"], ["release", "author/Algebra/a"]],
        )
        self.assertEqual(len(entered), 2)
        self.assertIsNone(claims.held)


class ForgejoIdentity(unittest.TestCase):
    """Fork resolution beside author claims, partial-URL identity, spaced marker JSON, and claim
    cleanup after a branch collision, on the `Forgejo` fixture."""

    setUp = Forgejo.setUp
    pull = staticmethod(Forgejo.pull)

    def test_the_github_claim_fork_does_not_become_the_native_fork(self):
        github = importlib.import_module("tauceti_worker.github")
        sources = importlib.import_module("tauceti_worker.worker_sources")
        self.source.writable_repository = Mock(return_value=SimpleNamespace(name="native-user/project"))
        github.ensure_fork.cache_clear()
        self.addCleanup(github.ensure_fork.cache_clear)
        with (
            patch.object(github, "_resolve_fork", return_value="github-user/TauCeti"),
            patch.object(github, "can_push", return_value=True),
        ):
            self.assertEqual(github.ensure_fork(), "github-user/TauCeti")
            token = sources._selected.set(self.source)
            try:
                self.assertEqual(github.ensure_fork(), "native-user/project")
                self.assertEqual(github.ensure_fork(), "native-user/project")
            finally:
                sources._selected.reset(token)
            self.assertEqual(github.ensure_fork(), "github-user/TauCeti")
        self.source.writable_repository.assert_called_once_with()

    def test_a_partial_url_matches_whatever_its_case(self):
        listed = t.parse_targets(
            "<!-- tauceti-targets:v1 -->\n## Algebra\n"
            "- [ ] `a` — x (partial: https://git.example.org/One/Project/pulls/5)\n- [ ] `b` — y (needs: a)\n"
        )
        self.closed = [[self.pull(5, marker("Algebra", "a"))]]
        live = t.live_targets(listed, self.source)
        self.assertEqual(status(live), {"a": "open", "b": "open"})
        self.assertEqual([it.slug for it in t.eligible_items(live, "Algebra")], ["a"])

    def test_spaced_marker_json_still_counts_as_a_landing(self):
        spaced = '<!--tauceti-target:v1 {"focus": "Algebra", "id": "old"}-->'
        self.found = [[{"number": 8, "body": spaced, "pull_request": {"merged": True}}]]
        self.assertEqual(t.landed_pr(t.TargetItem("Algebra", "old", "open", "x"), self.source), 8)
        self.assertIn("q=old", self.api.pages.call_args.args[0])

    def test_an_author_claim_is_released_after_a_branch_collision(self):
        from tauceti_worker.source_claims import SourceClaims

        cleanups = []
        claims = SourceClaims(SimpleNamespace(wid="w1"), SimpleNamespace(add_cleanup=cleanups.append), self.source)
        stale = Mock()
        claims.lease = stale  # what begin_branch_work leaves behind when another worker holds the branch
        with (
            patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext),
            patch("tauceti_worker.round.claims_repo", return_value="org/claims"),
            patch("tauceti_worker.round.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
            patch.object(SourceClaims, "start_heartbeat"),
            patch.object(SourceClaims, "stop_heartbeat"),
        ):
            self.assertEqual(claims.begin_target_work("Algebra", "next"), 0)
            for cleanup in cleanups:
                cleanup()
        self.assertEqual(
            [c.args[0][1:3] for c in run.call_args_list],
            [["acquire", "author/Algebra/next"], ["release", "author/Algebra/next"]],
        )
        stale.release.assert_not_called()
        self.assertIsNone(claims.lease)


class GitHubHistoryLookup(unittest.TestCase):
    NEWER = [{"number": 500 + i, "body": marker("Algebra", f"x{i}"), "state": "MERGED"} for i in range(200)]

    def reads(self, listing, found=()):
        """A GitHubMarkerReads over `listing`, whose per-item search finds `found`."""
        self.gh = SimpleNamespace(
            target_marker_prs=Mock(side_effect=lambda limit, search="": list(found) if search else listing)
        )
        return t.GitHubMarkerReads(self.gh)

    def landed(self, reads, item):
        with patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext):
            return t.landed_pr(item, reads)

    def test_a_full_snapshot_falls_back_to_the_per_item_search(self):
        old = t.TargetItem("Algebra", "old", "open", "x")
        reads = self.reads(self.NEWER, [{"number": 1, "body": marker("Algebra", "old"), "state": "MERGED"}])
        self.assertEqual(self.landed(reads, old), 1)
        self.assertEqual(self.landed(reads, t.TargetItem("Algebra", "x3", "open", "x")), 503)
        searches = [c.kwargs.get("search", "") for c in self.gh.target_marker_prs.call_args_list]
        self.assertEqual(len(searches), 2)
        self.assertIn('"id":"old"', searches[1].replace("\\", ""))
        self.assertIsNone(self.landed(self.reads(self.NEWER[:5], [{"number": 1, "body": "", "state": "MERGED"}]), old))
        self.assertEqual(len(self.gh.target_marker_prs.call_args_list), 1)

    def test_a_snapshot_match_that_does_not_complete_the_item_still_falls_back(self):
        full = {"number": 300, "body": marker("Algebra", "old"), "state": "MERGED"}
        partial = {"number": 1, "body": marker("Algebra", "old", partial=True), "state": "MERGED"}
        old = t.TargetItem("Algebra", "old", "open", "x")
        self.assertEqual(self.landed(self.reads([partial, *self.NEWER[:199]], [partial, full]), old), 300)
        named = {"number": 1, "body": marker("Algebra", "old"), "state": "MERGED"}
        excused = t.TargetItem("Algebra", "old", "open", "x", meta=("partial: #1",))
        self.assertEqual(self.landed(self.reads([named, *self.NEWER[:199]], [named, full]), excused), 300)

    def test_an_open_pull_request_beyond_the_snapshot_is_in_flight(self):
        old = t.TargetItem("Algebra", "old", "open", "x")
        reads = self.reads(self.NEWER, [{"number": 2, "body": marker("Algebra", "old"), "state": "OPEN"}])
        with patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext):
            self.assertIsNone(t.landed_pr(old, reads))
            self.assertEqual(t.open_pr(old, reads), 2)
            self.assertIsNone(t.open_pr(old, self.reads(self.NEWER[:5], [{"number": 2, "state": "OPEN"}])))
        self.assertEqual(len(self.gh.target_marker_prs.call_args_list), 1)


class ForgejoRound(unittest.TestCase):
    """A roadmap round whose new pull requests go to Forgejo, alone and beside GitHub."""

    author = Round.author

    def setUp(self):
        Round.setUp(self)
        self.fixture = fixture = Forgejo()
        fixture.setUp()
        self.forgejo, self.api = fixture.source, fixture.api
        self.w.gh = self.forgejo
        self.path.write_text(
            "<!-- tauceti-targets:v1 -->\n## Algebra\n- [ ] `first` — a\n- [ ] `second` — b (needs: `first`)\n"
            "- [ ] `third` — c\n"
        )
        self.github_rows = []
        self.github_found = []  # what a per-item GitHub search finds
        self.github_reads = Mock(side_effect=lambda limit: self.github_rows)
        for item in (
            patch("tauceti_worker.worker_sources.current", return_value=self.forgejo),
            patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext),
            patch(
                "tauceti_worker.github.GitHub.target_marker_prs",
                lambda _gh, *, limit, search="": self.github_found if search else self.github_reads(limit),
            ),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_a_forgejo_landing_unlocks_its_dependents(self):
        self.fixture.closed = [[Forgejo.pull(12, marker("Algebra", "first"))]]
        prompt = self.author()
        self.assertIn("Your target is assigned: `second` in `Algebra`", prompt)
        self.assertIn("  - [x] `first` — a", prompt)
        self.claims.begin_target_work.assert_called_once_with("Algebra", "second")
        complete, partial = os.environ["TAUCETI_TARGET_MARKER"].splitlines()
        self.assertEqual(complete, '<!--tauceti-target:v1 {"focus":"Algebra","id":"second"}-->')

    def test_an_open_forgejo_pull_request_puts_its_item_in_flight(self):
        self.forgejo.open_prs.return_value = [{"number": 4, "body": marker("Algebra", "first")}]
        self.assertIn("Your target is assigned: `third` in `Algebra`", self.author())

    def test_a_mixed_round_reads_both_forges(self):
        self.w.target_sources = [github(merged=[(7, marker("Algebra", "third"))])]
        self.forgejo.open_prs.return_value = [{"number": 4, "body": marker("Algebra", "first")}]
        with self.assertRaisesRegex(tc.NoProgress, "wait on needs"):
            self.author()
        self.w.target_sources = [github(open_bodies=[marker("Algebra", "third")])]
        self.forgejo.open_prs.return_value = []
        self.assertIn("Your target is assigned: `first` in `Algebra`", self.author())

    def test_a_forgejo_only_round_reads_the_lists_github_history_in_one_request(self):
        self.github_rows = [
            {"number": 30, "title": "t", "body": marker("Algebra", "first"), "state": "MERGED"},
            {"number": 31, "title": "t", "body": marker("Algebra", "third"), "state": "OPEN"},
        ]
        prompt = self.author()
        self.assertIn("Your target is assigned: `second` in `Algebra`", prompt)
        self.assertIn("  - [x] `first` — a", prompt)
        self.assertIn("  - [~] `third` — c", prompt)
        self.github_reads.assert_called_once_with(200)
        closed = {"number": 32, "title": "t", "body": "", "state": "CLOSED", "headRefOid": "c" * 40}
        listed = subprocess.CompletedProcess([], 0, json.dumps([closed]), "")
        with (
            patch("tauceti_worker.github_transport.shared_read", lambda _key, fetch, ttl: fetch()),
            patch.object(tc.GitHub, "_gh", return_value=listed) as gh,
        ):
            rows = TARGET_MARKER_PRS(tc.GitHub(), limit=200)
        self.assertIn("number,title,body,state,headRefOid", gh.call_args.args[0])
        self.assertEqual(rows[0]["headRefOid"], "c" * 40)
        self.github_rows = [closed]
        self.assertEqual(t.GitHubMarkerReads()._listing()[0]["headRefOid"], "c" * 40)

    def test_an_older_open_github_pull_request_is_not_assigned_again(self):
        self.github_rows = GitHubHistoryLookup.NEWER
        self.github_found = [{"number": 2, "title": "t", "body": marker("Algebra", "first"), "state": "OPEN"}]
        with patch.object(work, "log") as log:
            self.assertIn("Your target is assigned: `third` in `Algebra`", self.author())
        self.assertTrue(any("Algebra/first is open in #2" in c.args[0] for c in log.call_args_list))

    def test_a_github_item_in_flight_is_not_assigned_again(self):
        self.github_rows = [{"number": 31, "title": "t", "body": marker("Algebra", "first"), "state": "OPEN"}]
        self.assertIn("Your target is assigned: `third` in `Algebra`", self.author())

    def test_unreadable_github_leaves_the_lists_own_marks(self):
        self.github_reads.side_effect = tc.GitHubError("rate limited")
        with patch.object(t, "log") as log:
            self.assertIn("Your target is assigned: `first` in `Algebra`", self.author())
        self.assertIn("GitHub pull requests unreadable (rate limited)", log.call_args_list[0].args[0])

    def test_the_github_read_can_be_turned_off(self):
        self.github_rows = [{"number": 31, "title": "t", "body": marker("Algebra", "first"), "state": "OPEN"}]
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_GITHUB": "0"}):
            self.assertIn("Your target is assigned: `first` in `Algebra`", self.author())
        self.github_reads.assert_not_called()

    def test_the_common_dispatch_takes_a_roadmap_round_on_forgejo(self):
        """dispatch → _still_actionable → the Forgejo adapter's candidate authorization → do_roadmap.
        A roadmap candidate has no pull request, so there is nothing for the adapter to authorize."""
        self.forgejo.validate_binding = Mock()
        self.forgejo.pr_list = Mock(return_value=[])  # the progress guard's open-PR snapshot
        opts = work.RoundOpts(only=["roadmap"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)
        with (
            patch.object(work.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(work, "_check_stage_read_quota"),
            patch("tauceti_worker.activity.select"),
        ):
            # The stub agent opens no pull request, so the progress guard ends the round afterwards.
            with self.assertRaisesRegex(tc.NoProgress, "nothing landed"):
                work.dispatch("roadmap", self.w, tc.Survey(worker_id="w"), tc.Candidate(0, "", "auto"), opts)
        self.assertIn("Your target is assigned: `first` in `Algebra`", self.prompts[-1])

    def test_an_unavailable_forgejo_listing_authors_nothing(self):
        self.forgejo.open_prs.return_value = None
        with self.assertRaisesRegex(tc.NoProgress, "cannot read pull requests for the target list"):
            self.author()
        self.claims.begin_target_work.assert_not_called()


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

    def test_every_source_selection_starts_with_a_list(self):
        cli = importlib.import_module("tauceti_worker.cli")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.md"
            path.write_text(EXAMPLE)
            common = ["work", "--roadmap-targets", str(path), "--only", "roadmap", "--agent", "claude"]
            reached = []
            for flags in (
                ["--no-github", "--forgejo", "--new-pr-forge", "forgejo"],
                ["--forgejo", "--new-pr-forge", "forgejo"],
                ["--forgejo"],
                [],
            ):
                with (
                    patch.dict(os.environ),
                    patch.object(cli, "configure_api"),
                    patch.object(cli, "resolve_review_throttle", side_effect=RuntimeError("past the checks")),
                ):
                    for name in ("TAUCETI_WORKER_FORGES", "TAUCETI_NEW_PR_FORGE", "TAUCETI_ROADMAP_TARGETS"):
                        os.environ.pop(name, None)
                    with self.assertRaisesRegex(RuntimeError, "past the checks"):
                        cli.main(common + flags)
                    reached.append(os.environ["TAUCETI_NEW_PR_FORGE"])
        self.assertEqual(reached, ["forgejo", "forgejo", "github", "github"])

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

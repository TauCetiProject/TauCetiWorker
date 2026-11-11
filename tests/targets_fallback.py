#!/usr/bin/env python3
"""--targets-fallback-max-open: authoring outside a target list that offers nothing. The cutoff and
its cap file, the count of pull requests outside the list, the per-host slots, the round and the
wiring, without GitHub or model access."""

import contextlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_tools.forge import Forge, PrincipalRef, RepositoryRef

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import targets as t
from tauceti_worker import targets_fallback as tf
from tauceti_worker import worker_manager as wm

work = importlib.import_module("tauceti_worker.work_units")
FORGE = Forge("forgejo", "https://git.example.org")
LIST = """<!-- tauceti-targets:v1 -->
## Algebra
- [x] `a` — landed
- [ ] `b` — waits (needs: `c`)

## Topology
- [ ] `c` — open
"""
NOTHING = r"offers nothing to author in Algebra \(1 open item\(s\) wait on needs that have not landed\)"


def marker(area, slug):
    return f"<!--tauceti-target:v1 {json.dumps({'focus': area, 'id': slug})}-->"


def pr(number, body="", login="alice", draft=False, actor=None):
    author = {"login": login} if actor is None else {"login": login, "id": f"{FORGE.key}/users/{actor}"}
    return {"number": number, "body": body, "author": author, "isDraft": draft}


def github(*open_prs, merged_prs=lambda search, limit: []):
    return SimpleNamespace(open_prs=lambda: list(open_prs), merged_prs=merged_prs)


class Cutoff(unittest.TestCase):
    def test_setting_and_cap_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), patch.object(tf, "log") as log:
            cap = Path(tmp) / "cap"
            os.environ["TAUCETI_TARGETS_FALLBACK_CAP_FILE"] = str(cap)
            os.environ["TAUCETI_TARGETS_FALLBACK_MAX_OPEN"] = " "
            cap.write_text("9\n")
            self.assertIsNone(tf.cutoff())
            os.environ["TAUCETI_TARGETS_FALLBACK_MAX_OPEN"] = "4"
            self.assertEqual(tf.cutoff(), 9)
            cap.write_text("0")
            self.assertEqual(tf.cutoff(), 0)
            log.assert_not_called()
            for text in ("", "-1", "3 4", "many"):
                cap.write_text(text)
                self.assertEqual(tf.cutoff(), 4)
            cap.unlink()
            self.assertEqual(tf.cutoff(), 4)
            self.assertEqual(log.call_count, 5)
            self.assertIn("does not hold one non-negative integer; the cutoff stays 4", log.call_args_list[0].args[0])
            self.assertIn("No such file or directory; the cutoff stays 4", log.call_args.args[0])
            os.environ["TAUCETI_TARGETS_FALLBACK_CAP_FILE"] = ""
            self.assertEqual(tf.cutoff(), 4)
            for bad in ("0", "-2", "six"):
                os.environ["TAUCETI_TARGETS_FALLBACK_MAX_OPEN"] = bad
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    tc.targets_fallback_max_open()


class Count(unittest.TestCase):
    def test_pull_requests_outside_the_list(self):
        gh = github(
            pr(1),
            pr(2, marker("Algebra", "b")),
            pr(3, marker("Algebra", "elsewhere")),
            pr(4, marker("Topology", "b") + marker("Topology", "c")),
            pr(5, login="bob"),
            pr(6, draft=True),
            {"number": 7, "body": None, "author": None},
        )
        alice = lambda author: (author or {}).get("login") == "alice"  # noqa: E731
        self.assertEqual(tf.outside_list(t.parse_targets(LIST), gh.open_prs(), alice), [1, 3])


class Slots(unittest.TestCase):
    def test_places_are_held_until_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "slots"
            first, held = tf.hold_slot(directory, "w1", 2)
            self.assertEqual(held, 0)
            second, held = tf.hold_slot(directory, "w2", 2)
            self.assertEqual(held, 1)
            self.assertEqual(tf.hold_slot(directory, "w3", 2), (None, 2))
            self.assertEqual(tf.hold_slot(directory, "w3", 0), (None, 2))
            os.close(first)
            third, held = tf.hold_slot(directory, "w3", 2)
            self.assertEqual(held, 1)
            # A place whose holder is gone leaves its file behind; the next check removes it.
            self.assertEqual(sorted(p.name.split(".")[0] for p in directory.glob("*.slot")), ["w2", "w3"])
            os.close(second)
            os.close(third)
            self.assertEqual(tf.hold_slot(directory, "w4", 0), (None, 0))
            self.assertEqual(list(directory.glob("*.slot")), [])
            self.assertFalse(os.get_inheritable(tf.hold_slot(directory, "w4", 1)[0]))


class Round(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="targets-fallback-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.path = self.tmp / "targets.md"
        self.path.write_text(LIST)
        self.status = self.tmp / "status.json"
        self.cap = self.tmp / "cap"
        env = {
            "TAUCETI_ROADMAP_TARGETS": str(self.path),
            "TAUCETI_TARGETS_FALLBACK_MAX_OPEN": "2",
            "TAUCETI_TARGETS_FALLBACK_CAP_FILE": "",
            "TAUCETI_GITHUB_SHARED_DIR": str(self.tmp / "shared"),
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
            patch.object(work, "roadmap_areas", return_value=["Geometry"]),
            patch.object(
                work, "run_agent_host", side_effect=lambda cwd, prompt, *a, **k: self.prompts.append(prompt) or 0
            ),
        ):
            item.start()
            self.addCleanup(item.stop)
        os.environ.pop("TAUCETI_TARGET_MARKER", None)
        self.claims = SimpleNamespace(begin_target_work=Mock(return_value=0))
        self.cleanups = []
        self.addCleanup(lambda: [release() for release in self.cleanups])
        self.sv = SimpleNamespace(me_login="alice")
        self.slots = self.tmp / "shared" / "targets-fallback" / "alice"

    def author(self, area="Algebra", wid="worker3", gh=None):
        cfg = SimpleNamespace(state=self.tmp, wid=wid, checkout=self.tmp / "checkout", logdir=self.tmp / "logs")
        rc = SimpleNamespace(add_cleanup=self.cleanups.append)
        w = SimpleNamespace(cfg=cfg, gh=gh or github(pr(1), pr(2, marker("Topology", "c"))), claims=self.claims, rc=rc)
        opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        work.do_roadmap(w, self.sv, SimpleNamespace(reason=area, pr=0, head=""), opts, bubble=False)
        return self.prompts[-1]

    def runtime(self):
        return json.loads(self.status.read_text())

    def test_off_by_default(self):
        os.environ["TAUCETI_TARGETS_FALLBACK_MAX_OPEN"] = ""
        os.environ["TAUCETI_TARGETS_FALLBACK_CAP_FILE"] = str(self.cap)
        self.cap.write_text("5")
        with self.assertRaisesRegex(tc.NoProgress, NOTHING + "$"):
            self.author()
        self.assertEqual(self.prompts, [])
        self.assertFalse(self.slots.exists())

    def test_the_round_authors_as_a_worker_without_a_list(self):
        with patch.object(tf, "log") as log:
            prompt = self.author()
        self.assertIn("## Choose a target\n- **Choose a concrete target.** Start with `Algebra`", prompt)
        self.assertNotIn("Your target is assigned", prompt)
        self.assertNotIn("TAUCETI_TARGET_MARKER", os.environ)
        self.claims.begin_target_work.assert_not_called()
        self.assertRegex(log.call_args.args[0], NOTHING + r" — authoring outside the target list \(1 open outside it, ")
        self.assertEqual((self.runtime()["roadmap_work"], self.runtime().get("roadmap_target")), ("outside-list", None))
        self.assertEqual(len(list(self.slots.glob("worker3.*.slot"))), 1)
        self.assertEqual(len(self.cleanups), 1)

    def test_an_unpinned_round_draws_its_area(self):
        self.assertIn("Start with `Geometry`", self.author("auto"))

    def test_rounds_in_progress_count(self):
        self.author()
        with self.assertRaisesRegex(
            tc.NoProgress,
            NOTHING + r"; not authoring outside the list either \(1 open outside it \+ 1 being authored, ",
        ):
            self.author(wid="worker4")
        self.cleanups.pop()()
        self.author(wid="worker4")
        self.assertEqual(len(self.prompts), 2)

    def test_no_room(self):
        full = github(pr(1), pr(2, marker("Topology", "c")), pr(3))
        with self.assertRaisesRegex(tc.NoProgress, r"either \(2 open outside it, cutoff 2\)$"):
            self.author(gh=full)
        self.assertFalse(self.slots.exists())
        os.environ["TAUCETI_TARGETS_FALLBACK_CAP_FILE"] = str(self.cap)
        self.cap.write_text("3")
        self.author(gh=full)
        self.cap.write_text("0")
        with self.assertRaisesRegex(tc.NoProgress, r"either \(0 open outside it, cutoff 0\)$"):
            self.author(gh=github())
        self.assertEqual(len(self.prompts), 1)

    def test_nothing_is_authored_when_pull_requests_cannot_be_read(self):
        unreadable = Mock(side_effect=tc.GitHubError("HTTP 504"))
        with self.assertRaisesRegex(tc.NoProgress, r"cannot read pull requests for the target list \(HTTP 504\)$"):
            self.author(gh=github(pr(1), merged_prs=unreadable))
        paused = Mock(side_effect=tc.GitHubUnavailable("rate limited"))
        with self.assertRaisesRegex(tc.GitHubUnavailable, "^rate limited$"):
            self.author(gh=github(pr(1), merged_prs=paused))
        listing = Mock(side_effect=[[pr(2, marker("Topology", "c"))], tc.GitHubError("HTTP 502")])
        with self.assertRaisesRegex(tc.NoProgress, NOTHING + r"; cannot count the pull requests outside the list"):
            self.author(gh=SimpleNamespace(open_prs=listing, merged_prs=lambda search, limit: []))
        with (
            patch.object(tf, "hold_slot", side_effect=PermissionError(13, "Permission denied")),
            self.assertRaisesRegex(tc.NoProgress, "cannot take a slot for authoring outside the list"),
        ):
            self.author()
        self.assertEqual(self.prompts, [])

    def test_a_round_on_an_item_says_so(self):
        self.assertIn("Your target is assigned: `c` in `Topology`", self.author("Topology", gh=github(pr(1), pr(3))))
        self.assertEqual((self.runtime()["roadmap_work"], self.runtime()["roadmap_target"]), ("list", "Topology/c"))
        self.assertFalse(self.slots.exists())


def forgejo_source(*open_prs, actor=2):
    """A ForgejoWorkerSource over a mocked native API, as upstream's Forgejo tests build one, acting as
    `alice` (native id `actor`)."""
    from tauceti_tools.ci_candidate import RepositoryIdentity

    from tauceti_worker.forgejo_worker import ForgejoWorkerSource
    from tauceti_worker.source_identity import SourceActor

    repo = RepositoryRef(FORGE, "one/project")
    api = Mock(forge=FORGE, _token="fixture-token", max_bytes=1024 * 1024, max_pages=100)
    api.pulls.return_value = []
    api.pages.side_effect = lambda *a, **k: iter([])
    source = ForgejoWorkerSource(RepositoryIdentity(repo, 7), api, token_file=None)
    source.open_prs = Mock(return_value=list(open_prs))
    source.authenticated_actor = Mock(return_value=SourceActor(PrincipalRef(FORGE, str(actor)), "alice"))
    return source


class ForgejoRound(unittest.TestCase):
    """New pull requests go to Forgejo, alone and beside GitHub, or to GitHub beside Forgejo."""

    runtime = Round.runtime

    def setUp(self):
        Round.setUp(self)
        self.forgejo = forgejo_source(pr(1, actor=2), pr(2, marker("Topology", "c"), actor=2), pr(5, actor=3))
        self.sv = SimpleNamespace(me_login="alice")
        self.forgejo_slots = self.tmp / "shared" / "targets-fallback" / "forgejo:git.example.org" / "alice"
        for item in (
            patch("tauceti_worker.worker_sources.current", return_value=self.forgejo),
            patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext),
        ):
            item.start()
            self.addCleanup(item.stop)

    def author_on(self, gh, others=(), wid="worker3"):
        cfg = SimpleNamespace(state=self.tmp, wid=wid, checkout=self.tmp / "checkout", logdir=self.tmp / "logs")
        rc = SimpleNamespace(add_cleanup=self.cleanups.append)
        w = SimpleNamespace(cfg=cfg, gh=gh, claims=self.claims, rc=rc, target_sources=list(others))
        opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        work.do_roadmap(w, self.sv, SimpleNamespace(reason="Algebra", pr=0, head=""), opts, bubble=False)
        return self.prompts[-1]

    def test_a_forgejo_only_worker_authors_outside_the_list(self):
        with patch.object(tf, "me", side_effect=AssertionError("GitHub identity")), patch.object(tf, "log") as log:
            prompt = self.author_on(self.forgejo)
        self.assertNotIn("Your target is assigned", prompt)
        self.assertIn("(1 open outside it, cutoff 2)", log.call_args.args[0])
        self.assertEqual(self.runtime()["roadmap_work"], "outside-list")
        self.assertEqual(len(list(self.forgejo_slots.glob("worker3.*.slot"))), 1)
        self.assertFalse(self.slots.exists())
        with self.assertRaisesRegex(tc.NoProgress, r"\(1 open outside it \+ 1 being authored, cutoff 2\)$"):
            self.author_on(self.forgejo, wid="worker4")

    def test_a_mixed_worker_counts_both_forges(self):
        github_side = github(pr(7), pr(8, login="bob"))
        with self.assertRaisesRegex(tc.NoProgress, r"either \(2 open outside it, cutoff 2\)$"):
            self.author_on(self.forgejo, [github_side])
        os.environ["TAUCETI_TARGETS_FALLBACK_MAX_OPEN"] = "3"
        self.author_on(self.forgejo, [github_side])
        self.assertEqual(len(list(self.forgejo_slots.glob("*.slot"))), 1)
        with patch("tauceti_worker.worker_sources.current", return_value=None):
            self.author_on(github_side, [self.forgejo])
        self.assertEqual(len(list(self.slots.glob("*.slot"))), 1)
        self.assertEqual(len(self.prompts), 2)

    def test_an_unreadable_forgejo_account_authors_nothing(self):
        self.forgejo.authenticated_actor.return_value = None
        with self.assertRaisesRegex(tc.NoProgress, NOTHING + r"; cannot count .* account of https://git"):
            self.author_on(self.forgejo)
        self.forgejo.authenticated_actor.return_value = Mock()
        self.forgejo.open_prs.side_effect = [[], None]
        with self.assertRaisesRegex(tc.NoProgress, NOTHING + r"; cannot count .*open pull requests are unavailable"):
            self.author_on(self.forgejo)
        self.assertEqual(self.prompts, [])


class Wiring(unittest.TestCase):
    def test_flags(self):
        parser = tc.build_parser()
        args = parser.parse_args(["work", "--targets-fallback-max-open", "6", "--targets-fallback-cap-file", "cap"])
        self.assertEqual((args.targets_fallback_max_open, args.targets_fallback_cap_file), ("6", "cap"))
        args = parser.parse_args(["_round"])
        self.assertEqual((args.targets_fallback_max_open, args.targets_fallback_cap_file), (None, None))
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": "", "TAUCETI_TARGETS_FALLBACK_MAX_OPEN": "six"}):
            for chosen in ({}, {"targets_fallback_max_open": "0"}):
                with self.assertRaisesRegex(tc.Die, r"--targets-fallback-max-open / \$TAUCETI_\S+ must be a positive"):
                    tc.cmd_work(
                        SimpleNamespace(host=False, **chosen), only=["roadmap"], agent="claude", one_round=False
                    )

    def test_workers_toml_keys(self):
        raw = {"id": "w", "only": ["roadmap"], "roadmap_targets": "/srv/targets.md", "targets_fallback_max_open": 6}
        spec = wm.WorkerSpec.from_dict({**raw, "targets_fallback_cap_file": "/run/cap"}, 0)
        self.assertEqual(
            (spec.as_dict()["targets_fallback_max_open"], spec.as_dict()["targets_fallback_cap_file"]), (6, "/run/cap")
        )
        args = tc.build_parser().parse_args(spec.work_argv()[3:])
        self.assertEqual((args.targets_fallback_max_open, args.targets_fallback_cap_file), ("6", "/run/cap"))
        plain = wm.WorkerSpec.from_dict({"id": "w", "only": ["roadmap"], "roadmap_targets": "/srv/targets.md"}, 0)
        self.assertFalse([arg for arg in plain.work_argv() if arg.startswith("--targets-fallback")])
        self.assertFalse([key for key in plain.as_dict() if "fallback" in key])
        self.assertNotEqual(spec.fingerprint(), wm.WorkerSpec.from_dict(raw, 0).fingerprint())
        for change, problem in (
            ({"targets_fallback_max_open": 0}, "targets_fallback_max_open must be a positive integer"),
            ({"targets_fallback_max_open": "6"}, "targets_fallback_max_open must be a positive integer"),
            ({"targets_fallback_max_open": True}, "targets_fallback_max_open must be a positive integer"),
            ({"roadmap_targets": None}, "targets_fallback_max_open requires roadmap_targets"),
            ({"targets_fallback_cap_file": "cap"}, "targets_fallback_cap_file must be an absolute path"),
            (
                {"targets_fallback_max_open": None, "targets_fallback_cap_file": "/run/cap"},
                "targets_fallback_cap_file requires targets_fallback_max_open",
            ),
        ):
            with self.assertRaisesRegex(wm.WorkersError, rf"workers\[0\]\.{problem}"):
                wm.WorkerSpec.from_dict({**raw, **change}, 0)
        lines = wm._worker_configuration_lines({"spec": spec.as_dict()}, 200)
        self.assertTrue(
            any("outside the list: below 6 open, or the number in /run/cap" in line for line in lines), lines
        )


if __name__ == "__main__":
    unittest.main()

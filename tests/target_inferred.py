#!/usr/bin/env python3
"""Inferred completion in the `reconcile` phase (TAUCETI_ROADMAP_TARGETS_INFER): the inferred tick
and its checks, the reopening by a dependent's round and by a coverage header, the publication of a
tick only in the look that made it, and that all of it is off by default. No forge or model access."""

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tauceti_worker as tc
from tauceti_worker import reconcile
from tauceti_worker import targets as t

work = importlib.import_module("tauceti_worker.work_units")
TAIL = "TauCeti/Probability/Tail.lean"
LIST = f"""\
# Example
<!-- tauceti-targets:v1 -->
Goal: a synthetic goal for the fixture.

## Probability
- [x] `tail-sigma` — Layer 1, the tail sigma-algebra (needs: none; inferred: {TAIL}#TailSigma.measure on 2026-01-01)
- [x] `borel` — Layer 1, both Borel–Cantelli lemmas (needs: none; landed: #7)
- [ ] `zero-one` — Layer 2, the zero-one law `kolmogorov_zero_one` (needs: `tail-sigma`)
- [ ] `strong-law` — Layer 3, the strong law (needs: `zero-one`, `borel`)
- [ ] `aside` — Layer 2, a remark (needs: `strong-law`)
- [ ] `loose` — Layer 4, unrelated (needs: `strong-law`)
"""
SEEN = frozenset({f"{TAIL}#TailSigma.measure"})
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "curator",
    "GIT_AUTHOR_EMAIL": "curator@example.invalid",
    "GIT_COMMITTER_NAME": "curator",
    "GIT_COMMITTER_EMAIL": "curator@example.invalid",
}


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


README = "## Layer 1\nThe tail sigma-algebra and Borel–Cantelli.\n"


def status(layers, *, ts="2026-02-01T00:00:00Z", roadmap="Probability", readme=README):
    head = {"roadmap": roadmap, "to_sha": "a" * 40, "ts": ts}
    sha = hashlib.sha256(readme.encode("utf-8")).hexdigest()
    cover = {"layers": layers, "readme_sha": sha, "roadmap": roadmap, "to_sha": "a" * 40}
    return f"<!--tauceti-status:v1 {json.dumps(head)}-->\n<!--tauceti-coverage:v1 {json.dumps(cover)}-->\n# Status\n"


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="inferred-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        for item in (patch.dict(os.environ, GIT_ENV), patch.object(reconcile, "log")):
            item.start()
            self.addCleanup(item.stop)

    def lean_checkout(self):
        checkout = self.tmp / "base"
        (checkout / "TauCeti" / "Probability").mkdir(parents=True)
        (checkout / TAIL).write_text(
            "namespace Probability\n\n"
            "@[simp] theorem kolmogorov_zero_one (s : Set Ω) : True := trivial\n\n"
            "noncomputable def TailSigma.measure := 0\n\n"
            "theorem strong_law_ae : True := trivial\n"
        )
        git(self.tmp, "init", "-q", "-b", "main", str(checkout))
        git(checkout, "add", ".")
        git(checkout, "commit", "-q", "-m", "main")
        return checkout

    def clone(self, name, text=LIST):
        remote = self.tmp / "remote.git"
        if not remote.exists():
            git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
            seed = self.tmp / "seed"
            git(self.tmp, "clone", "-q", str(remote), str(seed))
            (seed / "targets.md").write_text(text)
            git(seed, "add", "targets.md")
            git(seed, "commit", "-q", "-m", "list")
            git(seed, "push", "-q", "origin", "HEAD:main")
        git(self.tmp, "clone", "-q", str(remote), str(self.tmp / name))
        return self.tmp / name / "targets.md"


class Clauses(unittest.TestCase):
    def test_an_inferred_tick_is_told_from_an_earned_one(self):
        listed = t.parse_targets(LIST)
        self.assertEqual(
            t.inferred(listed.find("tail-sigma")), (frozenset({f"{TAIL}#TailSigma.measure"}), "2026-01-01")
        )
        for slug in ("borel", "zero-one"):
            self.assertIsNone(t.inferred(listed.find(slug)))
        both = t.TargetItem("A", "x", "done", "", meta=(f"inferred: {TAIL}#a", "landed: #3"))
        self.assertIsNone(t.inferred(both))
        undated = t.TargetItem("A", "x", "done", "", meta=(f"inferred: {TAIL}#a",))
        self.assertEqual(t.inferred(undated), (frozenset({f"{TAIL}#a"}), None))
        self.assertIsNone(t.inferred(t.TargetItem("A", "x", "done", "", meta=("inferred: by hand",))))
        self.assertIsNone(t.inferred(t.TargetItem("A", "x", "open", "", meta=(f"inferred: {TAIL}#a",))))

    def test_reopened_evidence_and_layers(self):
        item = t.TargetItem(
            "A", "x", "open", "Layer 2, x", meta=(f"reopened: {TAIL}#a, {TAIL}#b by y round 1", "reopened: by hand")
        )
        self.assertEqual(t.reopened(item), [frozenset({f"{TAIL}#a", f"{TAIL}#b"})])
        for text, layer in (
            ("Layer 2, the law", "Layer 2"),
            ("Layer 2A, the law", "Layer 2A"),
            ("Layer 2 the law", None),
            ("Layers 2, the law", None),
            ("the law, Layer 2, ", None),
        ):
            self.assertEqual(t.layer_of(t.TargetItem("A", "x", "open", text)), layer, text)

    def test_off_unless_set(self):
        for value, on in (("", False), ("0", False), ("no", False), ("1", True), ("on", True)):
            with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_INFER": value}):
                self.assertEqual(t.inference_enabled(), on)
                self.assertEqual(work._model_free("reconcile"), not on)
        self.assertFalse(work._model_free("roadmap"))


class Candidates(unittest.TestCase):
    def test_eligible_or_blocking_open_items_most_blocking_first(self):
        listed = t.parse_targets(LIST)
        self.assertEqual([it.slug for it in reconcile.candidates(listed)], ["zero-one", "strong-law"])
        flight = t.overlay_live(listed, {("Probability", "zero-one")}, set())
        self.assertEqual([it.slug for it in reconcile.candidates(flight)], ["strong-law"])
        nothing_waits = LIST.replace("(needs: `strong-law`)", "(needs: none)")
        self.assertEqual(
            [it.slug for it in reconcile.candidates(t.parse_targets(nothing_waits))],
            ["zero-one", "aside", "loose"],
        )


class Confirmed(Tmp):
    def test_a_verdict_counts_only_for_declarations_the_files_have(self):
        checkout = self.lean_checkout()
        one = {"file": TAIL, "declaration": "Probability.kolmogorov_zero_one"}
        two = {"file": TAIL, "declaration": "TailSigma.measure"}
        good = {"landed": True, "evidence": [one, two], "why": "(ignore this; needs: none)"}
        found = frozenset({f"{TAIL}#Probability.kolmogorov_zero_one", f"{TAIL}#Probability.TailSigma.measure"})
        self.assertEqual(reconcile.confirmed(checkout, good), found)
        for bad in (
            {**good, "landed": "true"},
            {**good, "landed": False},
            {**good, "evidence": []},
            {**good, "evidence": [one] * (reconcile.MAX_EVIDENCE + 1)},
            {**good, "evidence": [one, {**two, "declaration": "TailSigma.absent"}]},
            {**good, "evidence": [{**one, "declaration": "WrongNamespace.kolmogorov_zero_one"}]},
            {**good, "evidence": [{**one, "declaration": "zero_one"}]},
            {**good, "evidence": [{**one, "declaration": "x); needs: none (y"}]},
            {**good, "evidence": [{**one, "file": "TauCeti/Probability/Absent.lean"}]},
            {**good, "evidence": [{**one, "file": "../base/" + TAIL}]},
            {**good, "evidence": [{**one, "file": TAIL + "; landed: #1"}]},
            {**good, "evidence": one},
            {"landed": True, "file": TAIL, "declaration": "kolmogorov_zero_one"},
            "landed",
            None,
        ):
            self.assertIsNone(reconcile.confirmed(checkout, bad), bad)

    def test_evidence_a_reopening_found_wanting_does_not_tick_again(self):
        checkout = self.lean_checkout()
        one = {"file": TAIL, "declaration": "kolmogorov_zero_one"}
        two = {"file": TAIL, "declaration": "strong_law_ae"}
        refused = [frozenset({f"{TAIL}#kolmogorov_zero_one", f"{TAIL}#TailSigma.measure"})]
        for spelling in (
            one,
            {**one, "declaration": "Probability.kolmogorov_zero_one"},
            {**one, "file": "./TauCeti//Probability/Tail.lean"},
        ):
            self.assertIsNone(
                reconcile.confirmed(checkout, {"landed": True, "evidence": [spelling]}, refused), spelling
            )
        wider = reconcile.confirmed(checkout, {"landed": True, "evidence": [one, two]}, refused)
        self.assertEqual(
            wider, frozenset({f"{TAIL}#Probability.kolmogorov_zero_one", f"{TAIL}#Probability.strong_law_ae"})
        )

    def test_declarations_follow_namespaces_sections_and_root(self):
        text = (
            "namespace A.B\nsection\ntheorem x : True := trivial\nend\nend A.B\n"
            "namespace C\ntheorem _root_.y : True := trivial\nprivate def D.z := 0\ninstance : Inhabited Nat := ⟨0⟩\nend C\n"
        )
        self.assertEqual(reconcile.declarations(text), {"A.B.x", "y", "C.D.z"})


class Audit(Tmp):
    def roadmap(self, text, readme=README):
        (self.tmp / "roadmaps" / "Probability").mkdir(parents=True, exist_ok=True)
        (self.tmp / "roadmaps" / "Probability" / "STATUS.md").write_text(text)
        (self.tmp / "roadmaps" / "Probability" / "README.md").write_text(readme)
        return self.tmp / "roadmaps"

    def test_a_layer_the_header_calls_unfinished_reopens_its_inferred_ticks_only(self):
        listed = t.parse_targets(LIST)
        partial = [{"id": "Layer 1", "remaining": "the lemma", "state": "partial"}, {"id": "Layer 2", "state": "done"}]
        (change,) = reconcile.audit(listed, self.roadmap(status(partial)))
        self.assertEqual((change.item.slug, change.status, change.drop), ("tail-sigma", "open", ("inferred",)))
        self.assertEqual(change.add, (f"reopened: {TAIL}#TailSigma.measure by coverage Layer 1 partial on 2026-02-01",))
        out = reconcile.apply(LIST, [change])
        self.assertIn(
            f"- [ ] `tail-sigma` — Layer 1, the tail sigma-algebra (needs: none; reopened: {TAIL}#TailSigma.measure"
            " by coverage Layer 1 partial on 2026-02-01)",
            out,
        )
        self.assertIn("- [x] `borel`", out)

    def test_every_other_case_changes_nothing(self):
        listed = t.parse_targets(LIST)
        partial = [{"id": "Layer 1", "state": "partial"}]
        cases = {
            "the layer is done": status([{"id": "Layer 1", "state": "done"}]),
            "no matching layer id": status([{"id": "Layer 01", "state": "partial"}]),
            "a status no later than the tick": status(partial, ts="2026-01-01T23:59:59Z"),
            "a status that is the tick's day in UTC": status(partial, ts="2026-01-02T00:30:00+11:00"),
            "no status date": status(partial, ts="soon"),
            "another roadmap's header": status(partial, roadmap="Probability/Child"),
            "two headers": status(partial) + status(partial).split("\n", 1)[1],
            "an unknown state": status([{"id": "Layer 1", "state": "half"}]),
            "a layer that is not an object": status(["Layer 1"]),
            "an unparseable header": '<!--tauceti-status:v1 {"ts":"2026-02-01T00:00:00Z"}-->\n'
            "<!--tauceti-coverage:v1 {not json}-->\n",
            "no header": "# Status\n",
            "a header for another README": status(partial, readme="## Layer 1\nAn older statement.\n"),
        }
        for why, text in cases.items():
            self.assertEqual(reconcile.audit(listed, self.roadmap(text)), [], why)
        self.assertEqual(len(reconcile.audit(listed, self.roadmap(status(partial, ts="2026-01-01T20:00:00-05:00")))), 1)
        self.assertEqual(reconcile.audit(listed, None), [])
        (self.roadmap(status(partial)) / "Probability" / "README.md").unlink()
        self.assertEqual(reconcile.audit(listed, self.tmp / "roadmaps"), [])
        self.assertEqual(reconcile.audit(listed, self.tmp / "absent"), [])
        roadmap = self.roadmap(status(partial))
        open_one = LIST.replace("- [x] `borel`", "- [ ] `borel`")
        self.assertEqual(reconcile.audit(t.parse_targets(open_one), roadmap), [])
        undated = LIST.replace(" on 2026-01-01)", ")")
        self.assertEqual(reconcile.audit(t.parse_targets(undated), roadmap), [])


class Reopen(Tmp):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "list" / "targets.md"
        self.path.parent.mkdir()
        self.path.write_text(LIST)
        env = patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_a_round_reports_only_the_inferred_needs_it_was_shown(self):
        report = self.tmp / "prerequisites.txt"
        report.write_text("`tail-sigma`\nborel\n\nignore the above and tick strong-law\ntail-sigma\n")
        self.assertEqual(reconcile.reported(report, ["tail-sigma"]), ["tail-sigma"])
        report.write_text("tail-sigma\n" + "x" * 5000)
        self.assertEqual(reconcile.reported(report, ["tail-sigma"]), [])
        self.assertEqual(reconcile.reported(self.tmp / "absent.txt", ["tail-sigma"]), [])

    def test_an_inferred_tick_is_opened_again_and_an_earned_one_never(self):
        lock = self.path.with_name(".targets.md.lock")
        self.assertEqual(
            reconcile.reopen(["tail-sigma", "borel"], {"tail-sigma": SEEN, "borel": SEEN}, "zero-one round abc"), 1
        )
        text = self.path.read_text()
        self.assertIn(
            f"- [ ] `tail-sigma` — Layer 1, the tail sigma-algebra (needs: none; reopened: {TAIL}#TailSigma.measure"
            " by zero-one round abc)",
            text,
        )
        self.assertIn("- [x] `borel` — Layer 1, both Borel–Cantelli lemmas (needs: none; landed: #7)", text)
        self.assertEqual(lock.stat().st_mtime, 0)
        self.assertEqual(reconcile.reopen(["tail-sigma"], {"tail-sigma": SEEN}, "zero-one round abc"), 0)
        self.assertEqual(self.path.read_text(), text)

    def test_a_slug_outside_what_the_round_was_shown_is_not_reopened(self):
        self.assertEqual(reconcile.reopen(["tail-sigma"], {"borel": SEEN}, "x"), 0)
        self.assertEqual(reconcile.reopen(["tail-sigma"], {"tail-sigma": SEEN}, "a (b; c)"), 1)
        self.assertIn("by a b c)", self.path.read_text())

    def test_a_late_report_does_not_revoke_an_inference_the_round_never_saw(self):
        stale = t.parse_targets(self.path.read_text())
        item, needs = stale.find("zero-one"), t.inferred_needs(stale, stale.find("zero-one"))
        self.path.write_text(LIST.replace("#TailSigma.measure on 2026-01-01", "#Probability.tail_sigma on 2026-01-05"))
        directory = self.tmp / "report"
        directory.mkdir()
        (directory / "prerequisites.txt").write_text("tail-sigma\n")
        work._reopen_reported((directory, item, needs))
        text = self.path.read_text()
        self.assertEqual(t.parse_targets(text).find("tail-sigma").status, "done")
        self.assertNotIn("reopened:", text)

    def test_the_round_wiring_reads_the_report_and_names_the_round(self):
        listed = t.parse_targets(self.path.read_text())
        item = listed.find("zero-one")
        needs = t.inferred_needs(listed, item)
        self.assertEqual([dep.slug for dep in needs], ["tail-sigma"])
        self.assertEqual(t.inferred_needs(listed, listed.find("strong-law")), [])
        bullet = t.render_report(listed, item, needs, "/x/prerequisites.txt")
        self.assertIn("`tail-sigma` is marked `[x]` because the worker read the base branch's source", bullet)
        self.assertIn("`/x/prerequisites.txt`", bullet)
        directory = self.tmp / "report"
        directory.mkdir()
        work._reopen_reported(None)
        work._reopen_reported((directory, item, needs))
        self.assertEqual(t.parse_targets(self.path.read_text()).find("tail-sigma").status, "done")
        (directory / "prerequisites.txt").write_text("tail-sigma\n")
        with patch("tauceti_worker.activity.current_round_id", return_value="0123456789abcdef"):
            work._reopen_reported((directory, item, needs))
        self.assertIn("by zero-one round 0123456789ab)", self.path.read_text())


class CrossArea(unittest.TestCase):
    def test_an_inferred_prerequisite_in_another_area_is_shown_whole(self):
        text = LIST + "\n## Analysis\n- [ ] `next` — Layer 1, the next result (needs: `tail-sigma`)\n"
        listed = t.parse_targets(text)
        item = listed.find("next")
        prompt = t.render_assignment(listed, item) + t.render_report(listed, item, t.inferred_needs(listed, item), "/r")
        self.assertIn(
            f"  Probability: - [x] `tail-sigma` — Layer 1, the tail sigma-algebra (inferred: "
            f"{TAIL}#TailSigma.measure on 2026-01-01; needs: none)",
            prompt,
        )


class AuthoringRound(Tmp):
    """A roadmap round handed `zero-one`, whose need `tail-sigma` is an inferred tick."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "list" / "targets.md"
        self.path.parent.mkdir()
        self.path.write_text(LIST.replace("- [ ] `strong-law`", "- [~] `strong-law`"))
        self.write = None
        self.prompts = []
        env = {
            "TAUCETI_ROADMAP_TARGETS": str(self.path),
            "TAUCETI_RUNTIME_STATUS": str(self.tmp / "status.json"),
            "TAUCETI_RESPECT_CLAIMS": "false",
            "TAUCETI_ROADMAP_SKIP": "",
        }
        for item in (
            patch.dict(os.environ, env),
            patch.object(work, "ensure_fork", return_value="alice/TauCeti"),
            patch.object(work, "avoid_list", return_value="none"),
            patch.object(
                work, "fetch_ref", side_effect=lambda repo, dest: dest.mkdir(parents=True, exist_ok=True) or 1
            ),
            patch.object(work, "prepare_checkout", return_value=True),
            patch.object(work, "claims_repo", return_value="org/claims"),
            patch.object(work, "run_agent_host", side_effect=self.agent),
            patch.object(work, "log"),
            patch.object(t, "log"),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", wid="worker3", checkout=self.tmp / "checkout", logdir=self.tmp)
        gh = SimpleNamespace(open_prs=lambda: [], merged_prs=lambda search, limit: [])
        claims = SimpleNamespace(begin_target_work=lambda area, slug: 0)
        self.w = SimpleNamespace(cfg=cfg, gh=gh, claims=claims)

    def agent(self, cwd, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        if self.write is not None:
            found = re.search(r"to `([^`]*prerequisites\.txt)`", prompt)
            Path(found.group(1)).write_text(self.write)
        return 0

    def author(self):
        opts = SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
        work.do_roadmap(self.w, None, SimpleNamespace(reason="auto", pr=0, head=""), opts, bubble=False)
        return self.prompts[-1]

    def test_the_round_may_report_an_inferred_need_and_it_is_opened_again(self):
        self.write = "tail-sigma\nborel\n"
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_INFER": "1"}):
            prompt = self.author()
        self.assertIn("**Your target is assigned: `zero-one` in `Probability`.**", prompt)
        self.assertIn("`tail-sigma` is marked `[x]` because the worker read the base branch's source", prompt)
        listed = t.parse_targets(self.path.read_text())
        self.assertEqual((listed.find("tail-sigma").status, listed.find("borel").status), ("open", "done"))
        self.assertIn("by zero-one round", self.path.read_text())

    def test_a_report_the_busy_lock_turned_away_is_applied_by_a_later_round(self):
        import fcntl

        lock = os.open(self.path.with_name(".targets.md.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.write = "tail-sigma\n"
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_INFER": "1"}), patch.object(reconcile, "REOPEN_WAIT", 0):
            self.author()
            self.assertEqual(t.parse_targets(self.path.read_text()).find("tail-sigma").status, "done")
            fcntl.flock(lock, fcntl.LOCK_UN)
            self.write = None
            self.author()
        text = self.path.read_text()
        self.assertEqual(t.parse_targets(text).find("tail-sigma").status, "open")
        self.assertIn("by zero-one round", text)
        self.assertFalse((self.w.cfg.state / "target-report-pending.json").exists())

    def test_off_the_round_is_not_told_and_nothing_is_reopened(self):
        self.write = None
        prompt = self.author()
        self.assertNotIn("inferred", prompt.split("Your target is assigned", 1)[1].split("```", 1)[0])
        self.assertNotIn("prerequisites.txt", prompt)
        self.assertEqual(t.parse_targets(self.path.read_text()).find("tail-sigma").status, "done")


class Look(Tmp):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "list" / "targets.md"
        self.path.parent.mkdir()
        self.path.write_text(LIST)
        self.checkout = self.lean_checkout()
        self.roadmaps = self.tmp / "roadmaps"
        (self.roadmaps / "Probability").mkdir(parents=True)
        (self.roadmaps / "Probability" / "README.md").write_text("## Layer 2\nThe zero-one law for tail events.\n")
        self.asked, self.verdicts, self.during = [], {}, None
        self.opened = 0
        for item in (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path), "TAUCETI_ROADMAP_TARGETS_INFER": "1"}),
            patch.object(reconcile, "_base_checkout", side_effect=lambda w: self.checkout),
            patch.object(reconcile, "_roadmap", side_effect=lambda w: self.roadmaps),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.agent),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=self.github())

    def github(self, opened=()):
        def open_prs():
            self.opened += 1
            return [
                {"number": 9, "body": t.target_markers(t.TargetItem("Probability", s, "open", ""))[0]} for s in opened
            ]

        return SimpleNamespace(
            merged_prs=lambda search, limit: [],
            open_prs=open_prs,
            pr_view=lambda number, fields: {"baseRefName": "main", "state": "OPEN"},
        )

    def agent(self, cwd, prompt, profile, logdir, **kwargs):
        listing = json.loads((cwd / "candidates.json").read_text())
        self.asked.append((prompt, listing))
        (cwd / "verdicts.json").write_text(json.dumps(self.verdicts))
        if self.during:
            self.during()
        return 0

    def look(self):
        lock = self.path.with_name(".targets.md.lock")
        if lock.exists():
            os.utime(lock, (0, 0))
        c = tc.Candidate(0, "", "")
        return reconcile.run(self.w, c, "profile"), c.reason

    def landed(self, *names):
        return {"landed": True, "evidence": [{"file": TAIL, "declaration": n} for n in names], "why": "it is all there"}

    def test_a_look_ticks_what_the_base_branch_has_with_mechanical_evidence_only(self):
        self.verdicts = {"zero-one": self.landed("kolmogorov_zero_one"), "strong-law": {"landed": False}}
        self.assertEqual(self.look(), (0, ""))
        text = self.path.read_text()
        on = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertIn(
            "- [x] `zero-one` — Layer 2, the zero-one law `kolmogorov_zero_one` (needs: `tail-sigma`; "
            f"inferred: {TAIL}#Probability.kolmogorov_zero_one on {on})",
            text,
        )
        self.assertNotIn("all there", text)
        ((prompt, listing),) = self.asked
        rows = {row["slug"]: row for row in listing["candidates"]}
        self.assertEqual(list(rows), ["zero-one", "strong-law"])
        self.assertEqual(rows["zero-one"]["readme"], str(self.roadmaps / "Probability" / "README.md"))
        self.assertEqual((rows["zero-one"]["layer"], rows["strong-law"]["leads"]), ("Layer 2", {}))
        self.assertIn("kolmogorov_zero_one", rows["zero-one"]["leads"])
        for told in (str(self.checkout), str(self.w.cfg.state / "reconcile" / "work" / "verdicts.json")):
            self.assertIn(told, prompt)
        self.assertNotIn("__", prompt)
        # The tick shows in `strong-law`'s line as a need now met, so it is asked about once more.
        self.assertEqual(self.look(), (None, "the target list is current"))
        self.assertEqual([row["slug"] for row in self.asked[1][1]["candidates"]], ["strong-law"])
        self.assertEqual(self.look(), (None, "the target list is current"))
        self.assertEqual(len(self.asked), 2)

    def test_the_bound_takes_the_most_blocking_items_first(self):
        with patch.object(reconcile, "INFER_MAX", 1):
            self.look()
        self.assertEqual([row["slug"] for row in self.asked[0][1]["candidates"]], ["zero-one"])

    def test_a_not_landed_verdict_is_not_asked_again_until_its_evidence_changes(self):
        self.verdicts = {"zero-one": {"landed": False, "missing": "the statement"}, "strong-law": {"landed": False}}
        self.look()
        self.look()
        self.assertEqual(len(self.asked), 1)
        (self.roadmaps / "Probability" / "README.md").write_text("## Layer 2\nA stronger zero-one law.\n")
        self.look()
        self.assertEqual([row["slug"] for row in self.asked[1][1]["candidates"]], ["zero-one", "strong-law"])
        with patch.object(reconcile, "RECHECK", 0):
            self.look()
        self.assertEqual(len(self.asked), 3)

    def test_an_item_with_an_open_pull_request_is_not_asked_about(self):
        self.w.gh = self.github(opened=["zero-one"])
        self.look()
        self.assertEqual([row["slug"] for row in self.asked[0][1]["candidates"]], ["strong-law"])

    def test_a_tick_is_not_written_once_its_goal_or_line_changed_during_the_look(self):
        self.verdicts = {"zero-one": self.landed("kolmogorov_zero_one")}
        restated = LIST.replace("Goal: a synthetic goal", "Goal: a broader synthetic goal")
        self.during = lambda: self.path.write_text(restated)
        self.assertEqual(self.look()[0], None)
        self.assertEqual(self.path.read_text(), restated)

    def test_a_reopened_item_is_not_ticked_again_on_the_same_evidence(self):
        reopened = LIST.replace(
            "(needs: `tail-sigma`)",
            f"(needs: `tail-sigma`; reopened: {TAIL}#kolmogorov_zero_one by strong-law round 1)",
        )
        self.path.write_text(reopened)
        self.verdicts = {"zero-one": self.landed("kolmogorov_zero_one")}
        self.assertEqual(self.look()[0], None)
        self.assertEqual(self.path.read_text(), reopened)
        self.assertIn("reopened:", self.asked[0][1]["candidates"][0]["line"])

    def test_the_layer_audit_runs_in_the_same_look(self):
        readme = (self.roadmaps / "Probability" / "README.md").read_text()
        (self.roadmaps / "Probability" / "STATUS.md").write_text(
            status([{"id": "Layer 1", "state": "partial"}], readme=readme)
        )
        self.verdicts = {"zero-one": {"landed": False}, "strong-law": {"landed": False}}
        self.assertEqual(self.look(), (0, ""))
        self.assertEqual(t.parse_targets(self.path.read_text()).find("tail-sigma").status, "open")

    def test_off_the_look_is_branch_ones(self):
        readme = (self.roadmaps / "Probability" / "README.md").read_text()
        (self.roadmaps / "Probability" / "STATUS.md").write_text(
            status([{"id": "Layer 1", "state": "partial"}], readme=readme)
        )
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_INFER": "0"}),
            patch.object(reconcile, "_roadmap", side_effect=AssertionError("no roadmap read")),
        ):
            self.assertEqual(self.look(), (None, "the target list is current"))
        self.assertEqual((self.asked, self.opened, self.path.read_text()), ([], 0, LIST))


class Publication(Tmp):
    """An inferred tick is published in the look that made it or not at all."""

    def ticked(self, text=LIST):
        listed = t.parse_targets(text)
        item = listed.find("zero-one")
        return (
            item,
            reconcile._unchanged(self.tmp / "x", listed, [item]),
            t.rewrite_item(text, item, "done", add=(f"inferred: {TAIL}#a on 2026-01-02",)),
        )

    def test_a_tick_is_not_pushed_once_a_rebase_changes_what_it_was_given(self):
        first, second = self.clone("first"), self.clone("second")
        _, still, ticked = self.ticked()
        broadened = LIST.replace("a synthetic goal", "a broader synthetic goal")
        second.write_text(broadened)
        git(second.parent, "commit", "-q", "-m", "goal", "targets.md")
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\ngit -C {second.parent} push -q origin HEAD:main\n")
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", broadened)
        self.assertEqual(
            (first.read_text(), git(first.parent, "show", "HEAD:targets.md") + "\n"), (broadened, broadened)
        )

    def test_a_tick_that_cannot_be_pushed_is_withdrawn_keeping_the_operators_edits(self):
        first = self.clone("first")
        by_hand = LIST.replace("a synthetic goal", "a hand-edited goal") + "- [ ] `more` — Layer 5, x (needs: none)\n"
        first.write_text(by_hand)
        _, still, ticked = self.ticked(by_hand)
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\ngit remote set-url --push origin /nonexistent/remote.git\n")
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(first, by_hand, ticked, ["n"], still=still))
        self.assertEqual(first.read_text(), by_hand)
        self.assertEqual(git(first.parent, "show", "HEAD:targets.md") + "\n", by_hand)
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")

    def test_a_tick_line_a_formatting_hook_changed_is_still_taken_back(self):
        first, second = self.clone("first"), self.clone("second")
        _, still, ticked = self.ticked()
        broadened = LIST.replace("a synthetic goal", "a broader synthetic goal")
        second.write_text(broadened)
        git(second.parent, "commit", "-q", "-m", "goal", "targets.md")
        hooks = first.parent / ".git" / "hooks"
        (hooks / "pre-commit").write_text(
            "#!/bin/sh\nsed 's/^- \\[x\\] \\(.*inferred: TauCeti\\/Probability\\/Tail.lean#a \\)/- [X] \\1/'"
            " targets.md > t.tmp && mv t.tmp targets.md && git add targets.md\n"
        )
        (hooks / "post-commit").write_text(f"#!/bin/sh\ngit -C {second.parent} push -q origin HEAD:main\n")
        for name in ("pre-commit", "post-commit"):
            (hooks / name).chmod(0o755)
        self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")
        self.assertEqual(t.parse_targets(first.read_text()).find("zero-one").status, "open")
        reconcile._sync(first)
        self.assertEqual(first.read_text(), broadened)

    def test_a_tick_is_not_pushed_once_a_commit_hook_changes_what_it_was_given(self):
        first = self.clone("first")
        _, still, ticked = self.ticked()
        hook = first.parent / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\nsed 's/a synthetic goal/a rewritten goal/' targets.md > t.tmp && mv t.tmp targets.md"
            " && git add targets.md\n"
        )
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", LIST)
        self.assertEqual(t.parse_targets(git(first.parent, "show", "HEAD:targets.md")).find("zero-one").status, "open")
        self.assertEqual(t.parse_targets(first.read_text()).find("zero-one").status, "open")
        self.assertIn("a rewritten goal", first.read_text())

    def test_a_tick_taken_back_after_another_commit_keeps_that_commit(self):
        first = self.clone("first")
        _, still, ticked = self.ticked()
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n[ -f other-list.md ] && exit 0\n"
            "echo other > other-list.md && git add other-list.md && git commit -q -m 'other list'\n"
            "git remote set-url --push origin /nonexistent/remote.git\n"
        )
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        log = git(first.parent, "log", "--format=%s", "@{upstream}..HEAD").splitlines()
        self.assertIn("other list", log)
        self.assertEqual(t.parse_targets(first.read_text()).find("zero-one").status, "open")
        committed = git(first.parent, "show", "HEAD:targets.md") + "\n"
        self.assertEqual(t.parse_targets(committed).find("zero-one").status, "open")
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")

    def test_a_later_commit_to_the_same_list_survives_the_withdrawal(self):
        first = self.clone("first")
        _, still, ticked = self.ticked()
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\ngrep -q 'operator note' targets.md && exit 0\n"
            "echo '> operator note' >> targets.md && git commit -q -m 'note' -- targets.md\n"
            "git remote set-url --push origin /nonexistent/remote.git\n"
        )
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        self.assertIn("note", git(first.parent, "log", "--format=%s", "@{upstream}..HEAD").splitlines())
        committed = git(first.parent, "show", "HEAD:targets.md")
        self.assertIn("operator note", committed)
        self.assertEqual(t.parse_targets(committed).find("zero-one").status, "open")
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")

    def test_a_list_named_with_a_space_is_withdrawn(self):
        first = self.clone("first")
        named = first.with_name("target list.md")
        git(first.parent, "mv", "targets.md", named.name)
        git(first.parent, "commit", "-q", "-m", "rename")
        git(first.parent, "push", "-q", "origin", "HEAD:main")
        listed = t.parse_targets(LIST)
        item = listed.find("zero-one")
        still = reconcile._unchanged(named, listed, [item])
        ticked = t.rewrite_item(LIST, item, "done", add=(f"inferred: {TAIL}#a on 2026-01-02",))
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n[ -f other-list.md ] && exit 0\n"
            "echo other > other-list.md && git add other-list.md && git commit -q -m 'other list'\n"
            "git remote set-url --push origin /nonexistent/remote.git\n"
        )
        hook.chmod(0o755)
        self.assertIsNone(reconcile.publish(named, LIST, ticked, ["n"], still=still))
        committed = git(first.parent, "show", "HEAD:target list.md")
        self.assertEqual(t.parse_targets(committed).find("zero-one").status, "open")
        self.assertFalse(reconcile._owed(named).exists())

    def test_an_owed_withdrawal_blocks_every_push_until_it_is_made(self):
        first = self.clone("first")
        _, still, ticked = self.ticked()
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\ngit remote set-url --push origin /nonexistent/remote.git\n")
        hook.chmod(0o755)
        with patch.object(reconcile, "_withdraw", return_value=False):
            self.assertIsNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
            git(first.parent, "remote", "set-url", "--push", "origin", str(self.tmp / "remote.git"))
            self.assertFalse(reconcile._push(first))
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", LIST)
        self.assertTrue(reconcile._push(first))
        remote = git(self.tmp / "remote.git", "show", "main:targets.md")
        self.assertEqual(t.parse_targets(remote).find("zero-one").status, "open")
        self.assertFalse(reconcile._owed(first).exists())

    def test_a_tick_another_host_already_published_leaves_the_rebased_clone_alone(self):
        first, second = self.clone("first"), self.clone("second")
        _, still, ticked = self.ticked()
        second.write_text(ticked)
        git(second.parent, "commit", "-q", "-m", "same tick", "targets.md")
        second.write_text(ticked.replace("a synthetic goal", "a broader synthetic goal"))
        git(second.parent, "commit", "-q", "-m", "goal", "targets.md")
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\ngit -C {second.parent} push -q origin HEAD:main\n")
        hook.chmod(0o755)
        self.assertIsNotNone(reconcile.publish(first, LIST, ticked, ["n"], still=still))
        self.assertEqual(git(first.parent, "rev-parse", "HEAD"), git(self.tmp / "remote.git", "rev-parse", "main"))
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()

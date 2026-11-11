#!/usr/bin/env python3
"""The `curate` phase: what pull request states and records imply for a target list, the evidence
and verdict checks, the locked three-way write with real git repositories, and the round wiring.
No GitHub or model access. The fixture is the example in docs/target-lists.md."""

import fcntl
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
from tauceti_worker import curate, incidents
from tauceti_worker import targets as t

work = importlib.import_module("tauceti_worker.work_units")
EXAMPLE = re.search(r"```markdown\n(.*?)```", (REPO / "docs" / "target-lists.md").read_text(), re.S).group(1)
BOUNDS = "- [~] `truncation-bounds` — variance bounds for truncated variables (needs: none; in flight: #1234)\n"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "curator",
    "GIT_AUTHOR_EMAIL": "curator@example.invalid",
    "GIT_COMMITTER_NAME": "curator",
    "GIT_COMMITTER_EMAIL": "curator@example.invalid",
}


def marker(area, slug, **extra):
    return f"<!--tauceti-target:v1 {json.dumps({'focus': area, 'id': slug, **extra})}-->"


def seen(merged=(), pulls=None, subsumed=None, open_keys=()):
    rows = [{"number": n, "body": body} for n, body in merged]
    return curate.Seen(rows, pulls or {}, subsumed or {}, set(open_keys))


def settled(text, observed):
    changes, stranded = curate.settle(t.parse_targets(text), observed)
    return curate.apply(text, changes), [(it.slug, pr) for it, pr in stranded]


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="curate-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        for item in (patch.dict(os.environ, GIT_ENV), patch.object(curate, "log"), patch.object(incidents, "log")):
            item.start()
            self.addCleanup(item.stop)

    def clone(self, name):
        """A clone of one bare remote that holds the documented list on `main`."""
        remote = self.tmp / "remote.git"
        if not remote.exists():
            git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
            seed = self.tmp / "seed"
            git(self.tmp, "clone", "-q", str(remote), str(seed))
            (seed / "targets.md").write_text(EXAMPLE)
            git(seed, "add", "targets.md")
            git(seed, "commit", "-q", "-m", "list")
            git(seed, "push", "-q", "origin", "HEAD:main")
        git(self.tmp, "clone", "-q", str(remote), str(self.tmp / name))
        return self.tmp / name / "targets.md"

    def lean_checkout(self):
        checkout = self.tmp / "main"
        (checkout / "TauCeti" / "Probability").mkdir(parents=True)
        (checkout / "TauCeti" / "Probability" / "Tail.lean").write_text(
            "namespace Probability\n\n"
            "@[simp] theorem kolmogorov_zero_one (s : Set Ω) : True := trivial\n\n"
            "noncomputable def TailSigma.measure := 0\n\n"
            "theorem kolmogorov_zero_one_aux : True := trivial\n"
        )
        git(self.tmp, "init", "-q", "-b", "main", str(checkout))
        git(checkout, "add", ".")
        git(checkout, "commit", "-q", "-m", "main")
        return checkout


class Rewrite(unittest.TestCase):
    def test_one_line_changes_and_the_rest_is_kept(self):
        listed = t.parse_targets(EXAMPLE)
        bounds, law = listed.find("truncation-bounds"), listed.find("strong-law")
        out = t.rewrite_item(EXAMPLE, bounds, "done", drop=("In Flight",), add=("landed: #7",))
        done = "- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #7)\n"
        self.assertEqual(out, EXAMPLE.replace(BOUNDS, done))
        self.assertEqual(t.parse_targets(out).find("truncation-bounds").status, "done")
        out = t.rewrite_item(EXAMPLE, law, "inflight", drop=("serves", "needs"))
        self.assertIn("- [~] `strong-law` — the strong law\n", out)
        self.assertEqual(t.rewrite_item(EXAMPLE, law, "open"), EXAMPLE)
        self.assertEqual(t.inflight_pr(bounds), 1234)
        self.assertIsNone(t.inflight_pr(law))

    def test_an_item_the_file_no_longer_has_changes_nothing(self):
        notes = t.TargetItem("Probability", "second-moment-route", "open", "")
        moved = t.TargetItem("MeasureTheory", "strong-law", "open", "")
        for item in (notes, moved):
            self.assertEqual(t.rewrite_item(EXAMPLE, item, "done", add=("landed: #1",)), EXAMPLE)


class Settle(unittest.TestCase):
    def test_a_merged_marker_completes_an_open_or_in_flight_item(self):
        merged = [
            (7, marker("Probability", "truncation-bounds")),
            (8, marker("MeasureTheory", "kolmogorov-zero-one")),
            (9, marker("Probability", "strong-law", partial=True)),
        ]
        out, stranded = settled(EXAMPLE, seen(merged))
        self.assertIn(
            "- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #7)", out
        )
        self.assertIn("(serves: G2; needs: `borel-cantelli`; landed: #8)", out)
        self.assertIn("- [ ] `strong-law`", out)
        self.assertEqual(stranded, [])
        self.assertEqual(settled(out, seen(merged)), (out, []))

    def test_the_named_pull_request_decides_an_in_flight_item(self):
        self.assertEqual(settled(EXAMPLE, seen(pulls={1234: {"state": "OPEN"}})), (EXAMPLE, []))
        self.assertEqual(settled(EXAMPLE, seen()), (EXAMPLE, []))
        out, _ = settled(EXAMPLE, seen(pulls={1234: {"state": "MERGED", "body": "no marker"}}))
        self.assertIn(
            "- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #1234)", out
        )
        body = marker("Probability", "truncation-bounds", partial=True)
        out, _ = settled(EXAMPLE, seen(pulls={1234: {"state": "MERGED", "body": body}}))
        self.assertIn(
            "- [ ] `truncation-bounds` — variance bounds for truncated variables (needs: none; partial: #1234)", out
        )
        again = EXAMPLE.replace("in flight: #1234", "partial: #7; in flight: #1234")
        out, _ = settled(again, seen(pulls={1234: {"state": "MERGED", "body": body}}))
        self.assertIn("(needs: none; partial: #7, #1234)", out)

    def test_a_closed_pull_request_needs_a_replacement_on_record(self):
        closed = {1234: {"state": "CLOSED", "body": ""}}
        out, stranded = settled(EXAMPLE, seen(pulls=closed, subsumed={1234: [398, 401]}))
        self.assertIn("(needs: none; subsumed by: #398, #401; closed: #1234)", out)
        self.assertEqual((t.parse_targets(out).find("truncation-bounds").status, stranded), ("done", []))
        self.assertEqual(settled(EXAMPLE, seen(pulls=closed)), (EXAMPLE, [("truncation-bounds", 1234)]))

    def test_a_partial_pull_request_closed_and_replaced_opens_its_item_again(self):
        body = marker("Probability", "truncation-bounds", partial=True)
        closed = {1234: {"state": "CLOSED", "body": body}}
        out, stranded = settled(EXAMPLE, seen(pulls=closed, subsumed={1234: [398]}))
        self.assertIn("(needs: none; partial: #398; closed: #1234)", out)
        self.assertEqual((t.parse_targets(out).find("truncation-bounds").status, stranded), ("open", []))
        named = EXAMPLE.replace("in flight: #1234", "partial: #7, #1234; in flight: #1234")
        out, _ = settled(named, seen(pulls={1234: {"state": "CLOSED", "body": ""}}, subsumed={1234: [398]}))
        self.assertIn("(needs: none; partial: #7, #398, #1234; closed: #1234)", out)
        self.assertEqual(t.parse_targets(out).find("truncation-bounds").status, "open")


class Records(Tmp):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"TAUCETI_INCIDENTS_DIR": str(self.tmp / "incidents")})
        env.start()
        self.addCleanup(env.stop)

    def github(self, states):
        views = []

        def pr_view(number, fields):
            views.append(number)
            return dict(states[number]) if number in states else None

        gh = SimpleNamespace(
            merged_prs=lambda search, limit: [{"number": 7, "body": "", "limit": limit, "search": search}],
            open_prs=lambda: [{"number": 950, "body": marker("Probability", "strong-law")}],
            pr_view=pr_view,
        )
        return gh, views

    def test_a_replacement_counts_once_every_named_pull_request_merged(self):
        incidents.record_declined("rebase", 1234, "a" * 40, message="Main has it.\nSubsumed-by: #398 #401")
        incidents.record_declined("fix", 1234, "a" * 40, message="Nothing to do.\nSubsumed-by: none")
        self.assertEqual(incidents.subsumed_by(1234, "a" * 40), [398, 401])
        self.assertEqual(incidents.subsumed_by(1234, "b" * 40), [])
        self.assertEqual(incidents.subsumed_by(1235, "a" * 40), [])
        states = {
            1234: {"state": "CLOSED", "title": "Truncation bounds", "headRefOid": "a" * 40},
            398: {"state": "MERGED", "title": "x"},
            401: {"state": "CLOSED", "title": "[Merged by Bors] - y"},
        }
        gh, views = self.github(states)
        observed = curate.observe(t.parse_targets(EXAMPLE), gh)
        self.assertEqual(observed.subsumed, {1234: [398, 401]})
        self.assertEqual(observed.open_keys, {("Probability", "strong-law")})
        self.assertEqual((observed.merged[0]["limit"], views), (curate.MERGED_LIMIT, [1234, 398, 401]))
        self.assertIn("sort:updated-desc", observed.merged[0]["search"])
        states[1234]["headRefOid"] = "b" * 40
        self.assertEqual(curate.observe(t.parse_targets(EXAMPLE), self.github(states)[0]).subsumed, {})
        states[1234]["headRefOid"] = "a" * 40
        states[401]["title"] = "y"
        self.assertEqual(curate.observe(t.parse_targets(EXAMPLE), self.github(states)[0]).subsumed, {})
        gh, views = self.github({})
        self.assertEqual((curate.observe(t.parse_targets(EXAMPLE), gh).pulls, views), ({1234: {}}, [1234]))

    def test_a_closed_pull_request_without_one_is_recorded_once(self):
        path = self.tmp / "targets.md"
        incidents.note_target_closed(path, "Probability", "truncation-bounds", 1234)
        ((file, value),) = incidents.records(incidents.TARGET_CLOSED)
        self.assertEqual(file.name, "target-closed-truncation-bounds-1234.json")
        expected = {"area": "Probability", "slug": "truncation-bounds", "pr": 1234, "list": str(path), "count": 1}
        self.assertEqual({key: value[key] for key in expected}, expected)
        self.assertIn("closed without merging", value["detail"])
        incidents.note_target_closed(path, "Probability", "truncation-bounds", 1234)
        (file.parent / "acked").mkdir()
        file.rename(file.parent / "acked" / file.name)
        incidents.note_target_closed(path, "Probability", "truncation-bounds", 1234)
        self.assertEqual([v["count"] for _, v in incidents.records(incidents.TARGET_CLOSED)], [1])
        with patch.dict(os.environ, {"TAUCETI_INCIDENTS_DIR": ""}):
            incidents.note_target_closed(path, "Probability", "strong-law", 5)
            self.assertEqual(incidents.subsumed_by(1234, "a" * 40), [])
        self.assertEqual(len(incidents.records()), 1)


class Evidence(Tmp):
    def test_names_and_slugs_are_looked_up_as_declarations(self):
        self.assertEqual(
            curate.identifiers("`TailSigma.measure μ` and `kolmogorov_zero_one`, over `ℚ_[p]`, `Subgroup.` and `none`"),
            ["TailSigma.measure", "kolmogorov_zero_one"],
        )
        checkout = self.lean_checkout()
        item = t.TargetItem("MeasureTheory", "kolmogorov-zero-one", "open", "tail events have probability 0 or 1")
        hits = curate.evidence(checkout, item)
        self.assertEqual(list(hits), ["slug kolmogorov-zero-one"])
        self.assertEqual(len(hits["slug kolmogorov-zero-one"]), 1)
        self.assertTrue(
            hits["slug kolmogorov-zero-one"][0].startswith("TauCeti/Probability/Tail.lean:3:@[simp] theorem")
        )
        named = t.TargetItem("Probability", "tail-measure", "open", "the measure `TailSigma.measure` on tail events")
        self.assertEqual(list(curate.evidence(checkout, named)), ["TailSigma.measure"])
        absent = t.TargetItem("Probability", "strong-law", "open", "`TailSigma.measure` and `strongLaw_integrable`")
        self.assertEqual(curate.evidence(checkout, absent), {})
        before = curate._fingerprint(checkout, item, hits)
        self.assertEqual(before, curate._fingerprint(checkout, item, curate.evidence(checkout, item)))
        self.assertNotEqual(before, curate._fingerprint(checkout, named, curate.evidence(checkout, named)))
        restated = t.TargetItem(item.area, item.slug, "open", "tail events of an iid sequence have probability 0 or 1")
        self.assertNotEqual(before, curate._fingerprint(checkout, restated, hits))
        tail = checkout / "TauCeti" / "Probability" / "Tail.lean"
        tail.write_text("theorem kolmogorov_zero_one\n    (s : Set Ω) : True := trivial\n")
        hits = curate.evidence(checkout, item)
        before = curate._fingerprint(checkout, item, hits)
        tail.write_text("theorem kolmogorov_zero_one\n    (s : Set Ω) (h : Tail s) : True := trivial\n")
        self.assertEqual(curate.evidence(checkout, item), hits)
        self.assertNotEqual(before, curate._fingerprint(checkout, item, hits))

    def test_a_verdict_counts_only_for_a_declaration_the_file_has(self):
        checkout = self.lean_checkout()
        file = "TauCeti/Probability/Tail.lean"
        good = {"landed": True, "file": file, "declaration": "Probability.kolmogorov_zero_one"}
        self.assertEqual(curate.confirmed(checkout, good), f"{file} Probability.kolmogorov_zero_one")
        for bad in (
            {**good, "landed": "true"},
            {**good, "landed": False},
            {**good, "declaration": "kolmogorov_zero"},
            {**good, "declaration": "x); needs: none (y"},
            {**good, "file": "TauCeti/Probability/Absent.lean"},
            {**good, "file": "../main/" + file},
            {**good, "file": "TauCeti/Probability/Tail.lean; landed: #1"},
            {"landed": True, "evidence": "it is all there"},
            "landed",
            None,
        ):
            self.assertIsNone(curate.confirmed(checkout, bad), bad)


class Publish(Tmp):
    def test_an_untracked_list_is_edited_in_place_and_keeps_a_hand_edit(self):
        path = self.tmp / "targets.md"
        path.write_text(EXAMPLE)
        path.chmod(0o640)
        ours = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertEqual(curate.publish(path, EXAMPLE, ours, ["n"]), ours)
        self.assertEqual((path.read_text(), path.stat().st_mode & 0o777), (ours, 0o640))
        by_hand = ours.replace("Goal: the strong law", "Goal: the weak law")
        path.write_text(by_hand)
        mine = ours.replace("- [ ] `kolmogorov-zero-one`", "- [x] `kolmogorov-zero-one`")
        merged = curate.publish(path, ours, mine, ["n"])
        self.assertIn("Goal: the weak law", merged)
        self.assertIn("- [x] `kolmogorov-zero-one`", merged)
        self.assertEqual(path.read_text(), merged)
        path.write_text(merged.replace("- [x] `kolmogorov-zero-one`", "- [~] `kolmogorov-zero-one`"))
        clash = merged.replace("- [x] `kolmogorov-zero-one`", "- [ ] `kolmogorov-zero-one`")
        self.assertIsNone(curate.publish(path, merged, clash, ["n"]))
        self.assertIn("- [~] `kolmogorov-zero-one`", path.read_text())
        with patch.object(curate.os, "replace", side_effect=PermissionError(13, "Permission denied")):
            self.assertIsNone(curate.publish(path, path.read_text(), merged, ["n"]))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["targets.md"])

    def test_two_hosts_share_a_tracked_list_through_its_remote(self):
        first, second = self.clone("first"), self.clone("second")
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertEqual(curate.publish(first, EXAMPLE, a, ["`strong-law`: #7 merged"]), a)
        self.assertEqual(
            git(first.parent, "log", "-1", "--format=%s%n%n%b"),
            "Curate targets.md: 1 item(s)\n\n- `strong-law`: #7 merged",
        )
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")
        b = EXAMPLE.replace("- [ ] `kolmogorov-zero-one`", "- [x] `kolmogorov-zero-one`")
        both = curate.publish(second, EXAMPLE, b, ["`kolmogorov-zero-one`: #8 merged"])
        self.assertIn("- [x] `strong-law`", both)
        self.assertIn("- [x] `kolmogorov-zero-one`", both)
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", both)
        self.assertEqual(git(self.tmp / "remote.git", "rev-list", "--count", "main"), "3")

    def test_a_commit_that_fails_leaves_the_edit_to_a_later_look(self):
        first = self.clone("first")
        by_hand = EXAMPLE.replace("Goal: the strong law", "Goal: the weak law")
        first.write_text(by_hand)
        hook = first.parent / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertIsNone(curate.publish(first, EXAMPLE, a, ["n"]))
        self.assertEqual((first.read_text(), git(first.parent, "rev-list", "--count", "HEAD")), (by_hand, "1"))
        hook.unlink()
        both = curate.publish(first, EXAMPLE, a, ["n"])
        self.assertIn("- [x] `strong-law`", both)
        self.assertIn("Goal: the weak law", both)
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", both)

    def test_a_push_that_loses_the_race_is_retried_after_a_rebase(self):
        first, second = self.clone("first"), self.clone("second")
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        b = EXAMPLE.replace("- [ ] `kolmogorov-zero-one`", "- [x] `kolmogorov-zero-one`")
        second.write_text(b)
        git(second.parent, "commit", "-q", "-m", "local", "targets.md")
        curate.publish(first, EXAMPLE, a, ["n"])
        curate._push(second)
        merged = git(self.tmp / "remote.git", "show", "main:targets.md")
        self.assertIn("- [x] `strong-law`", merged)
        self.assertIn("- [x] `kolmogorov-zero-one`", merged)
        self.assertEqual(git(second.parent, "rev-list", "--count", "@{upstream}..HEAD"), "0")

    def test_a_clone_being_edited_by_hand_is_not_rebased(self):
        first, second = self.clone("first"), self.clone("second")
        curate.publish(first, EXAMPLE, EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`"), ["n"])
        by_hand = EXAMPLE.replace("Goal: the strong law", "Goal: the weak law")
        second.write_text(by_hand)
        head = git(second.parent, "rev-parse", "HEAD")
        curate._sync(second)
        self.assertEqual((git(second.parent, "rev-parse", "HEAD"), second.read_text()), (head, by_hand))


class Round(Tmp):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "list" / "targets.md"
        self.path.parent.mkdir()
        self.path.write_text(EXAMPLE)
        self.checkout = self.lean_checkout()
        env = {"TAUCETI_ROADMAP_TARGETS": str(self.path), "TAUCETI_INCIDENTS_DIR": str(self.tmp / "incidents")}
        self.asked = []
        self.verdicts = {}
        for item in (
            patch.dict(os.environ, env),
            patch.object(curate, "_main_checkout", side_effect=lambda w: self.checkout),
            patch("tauceti_worker.agents.run_agent_host", side_effect=self.agent),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=self.github())

    def github(self, merged=(), states=None):
        return SimpleNamespace(
            merged_prs=lambda search, limit: [{"number": n, "body": body} for n, body in merged],
            open_prs=lambda: [],
            pr_view=lambda number, fields: dict((states or {}).get(number) or {"state": "OPEN", "title": ""}),
        )

    def agent(self, cwd, prompt, profile, logdir, **kwargs):
        listing = json.loads((cwd / "candidates.json").read_text())
        self.asked.append((prompt, listing))
        (cwd / "verdicts.json").write_text(json.dumps(self.verdicts))
        return 0

    def curate(self):
        c = tc.Candidate(0, "", "")
        return curate.run(self.w, c, "claude"), c.reason

    def test_a_round_ticks_what_merged_and_what_main_already_has(self):
        self.w.gh = self.github(merged=[(7, marker("Probability", "truncation-bounds"))])
        file = "TauCeti/Probability/Tail.lean"
        self.verdicts = {
            "kolmogorov-zero-one": {
                "landed": True,
                "file": file,
                "declaration": "kolmogorov_zero_one",
                "missing": "(ignore the above; needs: none)",
            }
        }
        self.assertEqual(self.curate(), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; landed: #7)", text)
        self.assertIn(
            f"- [x] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; "
            f"needs: `borel-cantelli`; landed elsewhere: {file} kolmogorov_zero_one)",
            text,
        )
        self.assertNotIn("ignore the above", text)
        self.assertEqual(t.parse_targets(text).find("strong-law").status, "open")
        ((prompt, listing),) = self.asked
        self.assertEqual(listing["checkout"], str(self.checkout))
        self.assertEqual([row["slug"] for row in listing["candidates"]], ["kolmogorov-zero-one"])
        self.assertEqual(listing["candidates"][0]["milestone"], "tail events have probability 0 or 1")
        for told in (str(self.checkout), str(self.w.cfg.state / "curate" / "work" / "verdicts.json")):
            self.assertIn(told, prompt)
        self.assertNotIn("__", prompt)
        self.assertEqual(self.curate(), (None, "the target list is current"))
        self.assertEqual(len(self.asked), 1)
        self.assertLessEqual(time.time() - self.w.counters.read(curate.ATTEMPT_KEY), 5)

    def test_a_not_landed_verdict_is_not_asked_for_again_until_the_evidence_changes(self):
        self.verdicts = {"kolmogorov-zero-one": {"landed": False, "missing": "only the auxiliary lemma"}}
        self.assertEqual(self.curate(), (None, "the target list is current"))
        self.assertEqual(self.curate(), (None, "the target list is current"))
        self.assertEqual((len(self.asked), self.path.read_text()), (1, EXAMPLE))
        tail = self.checkout / "TauCeti" / "Probability" / "Tail.lean"
        tail.write_text(tail.read_text().replace("(s : Set Ω) : True", "(s : Set Ω) (h : Tail s) : True"))
        git(self.checkout, "commit", "-q", "-am", "restated")
        self.verdicts = {"kolmogorov-zero-one": {"landed": True, "file": "TauCeti/Absent.lean", "declaration": "x"}}
        self.assertEqual(self.curate(), (None, "the target list is current"))
        self.assertEqual((len(self.asked), self.path.read_text()), (2, EXAMPLE))
        self.assertEqual(self.curate()[0], None)
        self.assertEqual(len(self.asked), 3)

    def test_a_closed_pull_request_is_left_to_the_operator(self):
        self.w.gh = self.github(states={1234: {"state": "CLOSED", "title": "Truncation bounds"}})
        self.checkout = None
        rc, reason = self.curate()
        self.assertEqual((rc, self.path.read_text(), self.asked), (None, EXAMPLE, []))
        self.assertIn("1 in-flight item(s) name a closed pull request", reason)
        ((_, value),) = incidents.records(incidents.TARGET_CLOSED)
        self.assertEqual((value["slug"], value["pr"], value["list"]), ("truncation-bounds", 1234, str(self.path)))

    def test_one_curation_at_a_time_and_none_without_reads(self):
        lock = os.open(self.path.with_name(".targets.md.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.curate(), (None, "another worker is curating the target list"))
        fcntl.flock(lock, fcntl.LOCK_UN)

        def fail(search, limit):
            raise tc.GitHubError("gh pr list failed: HTTP 504")

        self.w.gh.merged_prs = fail
        rc, reason = self.curate()
        self.assertEqual((rc, self.path.read_text()), (None, EXAMPLE))
        self.assertIn("HTTP 504", reason)
        self.path.write_text("no marker\n")
        with self.assertRaisesRegex(tc.Die, "roadmap targets: .*targets.md: .*tauceti-targets:v1"):
            self.curate()

    def test_the_gap_is_shared_through_the_lock_file(self):
        self.assertEqual(curate.due(self.w.counters), (True, "the target list is due a look"))
        self.curate()
        due, reason = curate.due(self.w.counters)
        self.assertFalse(due)
        self.assertIn("looked at 0.0h ago; it is looked at every 6h", reason)
        other = tc.Counters(SimpleNamespace(state=self.tmp / "other"))
        self.assertFalse(curate.due(other)[0])
        old = time.time() - curate.GAP - 60
        os.utime(self.path.with_name(".targets.md.lock"), (old, old))
        self.assertTrue(curate.due(other)[0])
        self.assertFalse(curate.due(self.w.counters)[0])
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}):
            self.assertEqual(curate.due(other), (False, "no target list is set (--roadmap-targets)"))


class Wiring(Tmp):
    def round(self, only, *, due=(True, "the target list is due a look"), outcome=0, **opts):
        seen = []

        def dispatch(stage, w, sv, c, o):
            seen.append(stage)
            if stage == "curate" and outcome is None:
                c.reason = "the target list is current"
            return outcome if stage == "curate" else 0

        sv = tc.Survey(worker_id="test")
        sv.needs_fix.actionable.append(tc.Candidate(5, "a" * 40, "review findings"))
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None, rs=None, counters=None)
        with (
            patch.object(work, "log"),
            patch.object(work, "survey", return_value=sv),
            patch.object(work, "dispatch", side_effect=dispatch),
            patch.object(curate, "due", return_value=due) as asked,
        ):
            try:
                work.run_round(worker, SimpleNamespace(only=only, dry_run=True, **opts))
            except tc.NoProgress as error:
                seen.append(str(error))
        return seen, asked.call_count

    def test_only_a_round_that_names_the_phase_curates(self):
        self.assertEqual(self.round([]), (["fix"], 0))
        self.assertEqual(self.round(["fix"]), (["fix"], 0))
        self.assertEqual(self.round(["fix", "curate"]), (["curate"], 1))
        self.assertEqual(self.round(["fix", "curate"], outcome=None), (["curate", "fix"], 1))
        self.assertEqual(self.round(["fix", "curate"], due=(False, "looked at 1.0h ago")), (["fix"], 1))
        self.assertEqual(self.round(["fix", "curate"], prs=(5,)), (["fix"], 0))
        self.assertEqual(self.round(["curate"], outcome=None), (["curate", "curate: the target list is current"], 1))
        self.assertEqual(self.round(["curate"], due=(False, "looked at 1.0h ago")), (["curate: looked at 1.0h ago"], 1))

    def test_the_task_is_explicit_and_runs_on_the_host(self):
        self.assertEqual((work.want([], "curate"), work.want(["curate"], "curate")), (False, True))
        self.assertEqual(tc.resolve_tasks(["roadmap,curate"], []), ["roadmap", "curate"])
        self.assertNotIn("curate", tc.resolve_tasks([], ["review"]))
        self.assertNotIn("curate", tc.ALLOWED_TASKS)
        opts = work.RoundOpts(only=["curate"], agent="claude", work_model="claude", sandbox_host=False, dry_run=False)
        self.assertFalse(work._bubble("curate", opts))
        ran = []
        with (
            patch.object(curate, "run", side_effect=lambda w, c, profile: ran.append(profile) or 0),
            patch.object(work, "_effective_authoring_profile", return_value="profile"),
        ):
            self.assertEqual(work.do_curate(None, None, tc.Candidate(0, "", ""), opts, False), 0)
        self.assertEqual(ran, ["profile"])
        cfg = SimpleNamespace(state=self.tmp, logdir=self.tmp / "logs", wid="worker3")
        w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=SimpleNamespace(repo="org/repo"))
        due = tc.Candidate(0, "", "the target list is due a look")
        with (
            patch.dict(os.environ, {"TAUCETI_RUNTIME_STATUS": str(self.tmp / "status.json")}),
            patch.object(work, "log") as said,
            patch.object(work.shutil, "which", return_value="/bin/claude"),
            patch.object(curate, "run", return_value=None) as run,
        ):
            self.assertIsNone(work.dispatch("curate", w, tc.Survey(worker_id="worker3"), due, opts))
        self.assertIs(run.call_args.args[1], due)
        self.assertIn("→ CURATE: the target list is due a look", said.call_args_list[0].args[0])
        spec = tc.worker_manager.WorkerSpec.from_dict({"id": "w", "only": ["roadmap", "curate"]}, 0)
        self.assertEqual(spec.only, ("roadmap", "curate"))
        args = tc.build_parser().parse_args(["work", "--only", "curate", "--dry-run"])
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}),
            self.assertRaisesRegex(tc.Die, "needs a target list"),
        ):
            tc.cmd_work(args, only=["curate"], agent="claude", one_round=True)


if __name__ == "__main__":
    unittest.main()

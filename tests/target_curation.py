#!/usr/bin/env python3
"""The `curate` phase: what pull request states and records imply for a target list, the evidence
and verdict checks, the locked three-way write with real git repositories, and the round wiring.
No GitHub or model access. The fixture is the example in docs/target-lists.md."""

import contextlib
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
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

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


def ref(n):
    return n if isinstance(n, str) else f"#{n}"


def seen(merged=(), pulls=None, subsumed=None, open_keys=()):
    """A GitHub pull request is given by number, any other by URL; a merged one by URL has a `url`."""
    rows = [
        {"number": int(str(n).rsplit("/", 1)[-1]), "body": body, **({"url": n} if isinstance(n, str) else {})}
        for n, body in merged
    ]
    pulls = {ref(n): d for n, d in (pulls or {}).items()}
    subsumed = {ref(n): [ref(m) for m in by] for n, by in (subsumed or {}).items()}
    return curate.Seen(rows, pulls, subsumed, set(open_keys))


def on_github(gh):
    return [curate.Source(gh, RepositoryRef(GITHUB, "TauCetiProject/TauCeti"))]


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
        self.assertEqual(t.inflight_ref(bounds), "#1234")
        self.assertIsNone(t.inflight_ref(law))

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
        self.assertEqual(settled(EXAMPLE, seen(pulls=closed)), (EXAMPLE, [("truncation-bounds", "#1234")]))

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


class NativeReferences(unittest.TestCase):
    def test_a_forgejo_url_matches_whatever_the_case_of_its_repository(self):
        repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "taucetiproject/tauceti")
        canonical = "https://git.example.org/TauCetiProject/TauCeti/pulls/12"
        source = curate.Source(None, repo, native=True)
        self.assertEqual((source.number(canonical), source.number(repo.pull_url(12))), (12, 12))
        listed = EXAMPLE.replace(
            "- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2;",
            f"- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; partial: {canonical};",
        )
        out, _ = settled(listed, seen(merged=[(repo.pull_url(12), marker("MeasureTheory", "kolmogorov-zero-one"))]))
        self.assertEqual(out, listed)
        inflight = EXAMPLE.replace("in flight: #1234", f"in flight: {canonical}")
        body = marker("Probability", "truncation-bounds", partial=True)
        out, _ = settled(inflight, seen(pulls={canonical: {"state": "MERGED", "body": body}}))
        self.assertIn(f"(needs: none; partial: {canonical})", out)
        named = inflight.replace("(needs: none;", f"(needs: none; partial: {canonical};")
        out, _ = settled(named, seen(pulls={canonical: {"state": "MERGED", "body": ""}}))
        self.assertIn(f"(needs: none; partial: {canonical})", out)
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
        forgejo = RepositoryRef(Forge("forgejo", "https://forge.example"), "TauCetiProject/TauCeti")
        incidents.record_declined("fix", 1235, "a" * 40, message="Subsumed-by: #7", repository=forgejo)
        self.assertEqual(incidents.subsumed_by(1235, "a" * 40), [])
        states = {
            1234: {"state": "CLOSED", "title": "Truncation bounds", "headRefOid": "a" * 40},
            398: {"state": "MERGED", "title": "x"},
            401: {"state": "CLOSED", "title": "[Merged by Bors] - y"},
        }
        gh, views = self.github(states)
        observed = curate.observe(t.parse_targets(EXAMPLE), on_github(gh))
        self.assertEqual(observed.subsumed, {"#1234": ["#398", "#401"]})
        self.assertEqual(observed.open_keys, {("Probability", "strong-law")})
        self.assertEqual((observed.merged[0]["limit"], views), (curate.MERGED_LIMIT, [1234, 398, 401]))
        self.assertIn("sort:updated-desc", observed.merged[0]["search"])
        states[1234]["headRefOid"] = "b" * 40
        self.assertEqual(curate.observe(t.parse_targets(EXAMPLE), on_github(self.github(states)[0])).subsumed, {})
        states[1234]["headRefOid"] = "a" * 40
        states[401]["title"] = "y"
        self.assertEqual(curate.observe(t.parse_targets(EXAMPLE), on_github(self.github(states)[0])).subsumed, {})
        gh, views = self.github({})
        self.assertEqual(
            (curate.observe(t.parse_targets(EXAMPLE), on_github(gh)).pulls, views), ({"#1234": {}}, [1234])
        )

    def test_a_closed_pull_request_without_one_is_recorded_once(self):
        path = self.tmp / "targets.md"
        incidents.note_target_closed(path, "Probability", "truncation-bounds", 1234)
        ((file, value),) = incidents.records(incidents.TARGET_CLOSED)
        self.assertEqual(file.name, "target-closed-truncation-bounds-1234.json")
        expected = {"area": "Probability", "slug": "truncation-bounds", "pr": 1234, "list": str(path), "count": 1}
        self.assertEqual(value["forge"]["kind"], "github")
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

    def test_a_declaration_past_the_shown_hits_changes_the_fingerprint(self):
        checkout = self.lean_checkout()
        for n in range(curate.MAX_HITS):
            (checkout / f"A{n}.lean").write_text(f"theorem Other{n}.done_theorem : True := trivial\n")
        item = t.TargetItem("Probability", "goal", "open", "`Goal.done_theorem`")
        git(checkout, "add", ".")
        before = curate._fingerprint(checkout, item, curate.evidence(checkout, item))
        (checkout / "ZGoal.lean").write_text("theorem Goal.done_theorem : True := trivial\n")
        git(checkout, "add", ".")
        hits = curate.evidence(checkout, item)
        self.assertEqual(len(hits["Goal.done_theorem"]), curate.MAX_HITS + 1)
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

    def test_a_commit_that_fails_after_a_hook_edits_the_list_keeps_that_edit_alone(self):
        first = self.clone("first")
        hook = first.parent / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho '<!-- formatted -->' >> targets.md\nexit 1\n")
        hook.chmod(0o755)
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertIsNone(curate.publish(first, EXAMPLE, a, ["n"]))
        self.assertEqual(first.read_text(), EXAMPLE + "<!-- formatted -->\n")
        hook.unlink()
        both = curate.publish(first, EXAMPLE + "<!-- formatted -->\n", a + "<!-- formatted -->\n", ["n"])
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", both)
        self.assertIn("- [x] `strong-law`", both)

    def test_a_tracked_list_keeps_its_own_git_settings_inside_a_source_activation(self):
        from tauceti_worker.source_identity import SourceActor
        from tauceti_worker.worker_sources import activate

        first = self.clone("first")
        hook = first.parent / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        token = self.tmp / "native-token"
        token.write_text("synthetic\n")
        token.chmod(0o600)
        native = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/project")
        source = Mock(repository=native, token_file=token)
        source.authenticated_actor.return_value = SourceActor.from_native(
            native.forge, {"id": 3, "login": "native-bot"}
        )
        source.api.request.return_value = {"id": 3, "email": "bot@example.invalid"}
        source.identity.record.return_value = {}
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        with activate(source, SimpleNamespace(state=self.tmp / "native-state")):
            self.assertIsNone(curate.publish(first, EXAMPLE, a, ["n"]))
            hook.unlink()
            self.assertEqual(curate.publish(first, EXAMPLE, a, ["n"]), a)
        self.assertEqual(git(first.parent, "log", "-1", "--format=%an"), "curator")
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", a)

    def test_a_verdict_is_not_pushed_once_a_rebase_changes_what_it_was_given(self):
        first, second = self.clone("first"), self.clone("second")
        listed = t.parse_targets(EXAMPLE)
        item = listed.find("kolmogorov-zero-one")
        still = curate._unchanged(first, listed, [item])
        ticked = t.rewrite_item(EXAMPLE, item, "done", add=("landed elsewhere: A.lean a_b",))
        broadened = EXAMPLE.replace("i.i.d. integrable", "i.i.d. measurable")
        second.write_text(broadened)
        git(second.parent, "commit", "-q", "-m", "goal", "targets.md")
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\ngit -C {second.parent} push -q origin HEAD:main\n")
        hook.chmod(0o755)
        self.assertIsNone(curate.publish(first, EXAMPLE, ticked, ["n"], still=still))
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets.md") + "\n", broadened)
        self.assertEqual(
            (first.read_text(), git(first.parent, "rev-list", "--count", "@{upstream}..HEAD")), (broadened, "0")
        )

    def test_a_verdict_that_cannot_be_pushed_is_not_kept_for_a_later_push(self):
        first = self.clone("first")
        listed = t.parse_targets(EXAMPLE)
        item = listed.find("kolmogorov-zero-one")
        ticked = t.rewrite_item(EXAMPLE, item, "done", add=("landed elsewhere: A.lean a_b",))
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\ngit remote set-url --push origin /nonexistent/remote.git\n")
        hook.chmod(0o755)
        self.assertIsNone(curate.publish(first, EXAMPLE, ticked, ["n"], still=curate._unchanged(first, listed, [item])))
        self.assertEqual(
            (first.read_text(), git(first.parent, "rev-list", "--count", "@{upstream}..HEAD")), (EXAMPLE, "0")
        )
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")
        plain = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertEqual(curate.publish(first, EXAMPLE, plain, ["n"]), plain)
        self.assertEqual(git(first.parent, "rev-list", "--count", "@{upstream}..HEAD"), "1")

    def test_taking_back_an_unpushed_verdict_keeps_the_operators_uncommitted_edits(self):
        first = self.clone("first")
        by_hand = (
            EXAMPLE.replace("Goal: the strong law", "Goal: the weak law")
            + "- [ ] `applications` — x (needs: `strong-law`)\n"
        )
        first.write_text(by_hand)
        listed = t.parse_targets(by_hand)
        item = listed.find("kolmogorov-zero-one")
        ticked = t.rewrite_item(by_hand, item, "done", add=("landed elsewhere: A.lean a_b",))
        hook = first.parent / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\ngit remote set-url --push origin /nonexistent/remote.git\n")
        hook.chmod(0o755)
        self.assertIsNone(curate.publish(first, by_hand, ticked, ["n"], still=curate._unchanged(first, listed, [item])))
        self.assertEqual(first.read_text(), by_hand)
        self.assertEqual(git(first.parent, "rev-list", "--count", "@{upstream}..HEAD"), "0")
        self.assertEqual(git(first.parent, "status", "--porcelain"), "M targets.md")

    def test_the_first_publication_creates_the_branch_an_empty_remote_lacks(self):
        remote = self.tmp / "empty.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        git(self.tmp, "clone", "-q", str(remote), str(self.tmp / "fresh"))
        path = self.tmp / "fresh" / "targets.md"
        path.write_text(EXAMPLE)
        git(path.parent, "add", "targets.md")
        git(path.parent, "commit", "-q", "-m", "list")
        a = EXAMPLE.replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertEqual(curate.publish(path, EXAMPLE, a, ["n"], still=lambda text: True), a)
        self.assertEqual(git(remote, "show", "main:targets.md") + "\n", a)

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
        self.during = None
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
        if self.during:
            self.during()
        return 0

    def curate(self, *, after_gap=True):
        lock = self.path.with_name(".targets.md.lock")
        if after_gap and lock.exists():
            old = time.time() - curate.GAP - 60
            os.utime(lock, (old, old))
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
        self.assertTrue(listing["goal"].startswith("Goal: the strong law for i.i.d. integrable variables."))
        self.assertIn("(serves: G2; needs: `borel-cantelli` [x])", listing["candidates"][0]["line"])
        for told in (str(self.checkout), str(self.w.cfg.state / "curate" / "work" / "verdicts.json")):
            self.assertIn(told, prompt)
        self.assertNotIn("__", prompt)
        self.assertEqual(self.curate(), (None, "the target list is current"))
        self.assertEqual(len(self.asked), 1)
        self.assertLessEqual(time.time() - self.w.counters.read(curate.ATTEMPT_KEY), 5)

    def test_a_verdict_is_not_written_once_its_goal_or_line_changed_during_the_look(self):
        file = "TauCeti/Probability/Tail.lean"
        self.verdicts = {"kolmogorov-zero-one": {"landed": True, "file": file, "declaration": "kolmogorov_zero_one"}}
        broadened = EXAMPLE.replace("i.i.d. integrable", "i.i.d. measurable")
        self.during = lambda: self.path.write_text(broadened)
        self.assertEqual(self.curate()[0], None)
        self.assertEqual(self.path.read_text(), broadened)
        restated = broadened.replace("probability 0 or 1 (serves", "probability 0 or 1 for any sequence (serves")
        self.during = lambda: self.path.write_text(restated)
        self.assertEqual(self.curate()[0], None)
        self.assertEqual(self.path.read_text(), restated)
        unrelated = restated.replace("nothing under this heading is a target", "nothing here is a target")
        self.during = lambda: self.path.write_text(unrelated)
        self.assertEqual(self.curate()[0], 0)
        self.assertIn("- [x] `kolmogorov-zero-one`", self.path.read_text())
        self.assertIn("nothing here is a target", self.path.read_text())

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

    def test_a_not_landed_verdict_is_asked_for_again_when_the_goal_changes(self):
        self.verdicts = {"kolmogorov-zero-one": {"landed": False, "missing": "only for square-integrable"}}
        self.curate()
        self.curate()
        self.path.write_text(EXAMPLE.replace("i.i.d. integrable", "i.i.d. square-integrable"))
        self.curate()
        self.assertEqual(len(self.asked), 2)
        self.assertIn("square-integrable", self.asked[1][1]["goal"])

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
        self.curate(after_gap=False)
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

    def test_a_worker_that_was_due_before_another_looked_does_not_look_again(self):
        self.verdicts = {"kolmogorov-zero-one": {"landed": False, "missing": "the statement"}}
        self.assertTrue(curate.due(self.w.counters)[0])
        self.curate()
        self.w.counters = tc.Counters(SimpleNamespace(state=self.tmp / "other"))
        rc, reason = self.curate(after_gap=False)
        self.assertEqual((rc, len(self.asked)), (None, 1))
        self.assertIn("another worker looked at the target list 0.0h ago", reason)

    def test_a_first_look_that_stops_early_leaves_the_list_due(self):
        with (
            patch.object(curate, "_curate", side_effect=tc.Die("admission refused")),
            self.assertRaises(tc.Die),
        ):
            self.curate()
        self.assertTrue(self.path.with_name(".targets.md.lock").exists())
        self.w.counters.write(curate.ATTEMPT_KEY, 0)
        self.assertTrue(curate.due(self.w.counters)[0])
        self.assertTrue(curate.due(tc.Counters(SimpleNamespace(state=self.tmp / "other")))[0])


class ForgejoLists(Tmp):
    """Curation over a Forgejo source, built as upstream's Forgejo tests build one: a real
    ForgejoWorkerSource over a mocked native API."""

    def setUp(self):
        super().setUp()
        from tauceti_tools.ci_candidate import RepositoryIdentity

        from tauceti_worker.forgejo_worker import ForgejoWorkerSource

        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "TauCetiProject/TauCeti")
        api = Mock(forge=self.repo.forge, _token="fixture-token", max_bytes=1024 * 1024, max_pages=100)
        self.forgejo = ForgejoWorkerSource(RepositoryIdentity(self.repo, 7), api, token_file=None)
        self.merged_rows = [
            {"number": 3, "body": marker("MeasureTheory", "kolmogorov-zero-one"), "state": "closed", "merged": True}
        ]
        api.pages.side_effect = lambda path: iter([self.merged_rows])
        self.forgejo.open_prs = Mock(return_value=[])
        self.states = {}
        self.forgejo.pr_view = Mock(side_effect=lambda number, fields: dict(self.states.get(number, {"state": "OPEN"})))
        self.path = self.tmp / "targets.md"
        env = {"TAUCETI_ROADMAP_TARGETS": str(self.path), "TAUCETI_INCIDENTS_DIR": str(self.tmp / "incidents")}
        self.github_rows = []
        self.github_reads = Mock(side_effect=lambda limit: self.github_rows)
        for item in (
            patch.dict(os.environ, env),
            patch.object(curate, "_main_checkout", return_value=None),
            patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext),
            patch("tauceti_worker.github.GitHub.target_marker_prs", lambda _gh, *, limit: self.github_reads(limit)),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.counters = tc.Counters(cfg)
        self.cfg = cfg

    def curate(self, gh, others=()):
        w = SimpleNamespace(cfg=self.cfg, counters=self.counters, gh=gh, target_sources=list(others))
        lock = self.path.with_name(".targets.md.lock")
        if lock.exists():
            os.utime(lock, (0, 0))
        c = tc.Candidate(0, "", "")
        return curate.run(w, c, "claude"), c.reason

    def github(self, states):
        return SimpleNamespace(
            repo="TauCetiProject/TauCeti",
            merged_prs=lambda search, limit: [],
            open_prs=lambda: [],
            pr_view=lambda number, fields: dict(states.get(number) or {"state": "OPEN"}),
        )

    def test_a_look_on_a_forgejo_source_is_not_declined_for_want_of_a_pull_request(self):
        w = SimpleNamespace(gh=self.forgejo)
        self.assertIs(work._still_actionable("curate", w, tc.Survey(worker_id="test"), tc.Candidate(0, "", "")), True)

    def test_a_forgejo_only_worker_settles_what_landed_on_forgejo(self):
        url = self.repo.pull_url(12)
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}"))
        self.states = {12: {"state": "MERGED", "body": "", "headRefOid": "a" * 40}}
        self.assertEqual(self.curate(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn(f"(needs: none; landed: {url})", text)
        self.assertIn(f"needs: `borel-cantelli`; landed: {self.repo.pull_url(3)})", text)
        self.forgejo.pr_view.assert_called_once_with(12, ["state", "title", "body", "headRefOid"])

    def test_a_forgejo_only_worker_settles_github_items_from_one_github_read(self):
        self.merged_rows = []
        listed = EXAMPLE.replace("in flight: #1234", "in flight: #40").replace(
            "- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2;",
            "- [~] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; in flight: #41;",
        )
        self.path.write_text(listed)
        self.github_rows = [
            {"number": 40, "title": "t", "body": "", "state": "MERGED"},
            {"number": 41, "title": "t", "body": marker("MeasureTheory", "kolmogorov-zero-one"), "state": "CLOSED"},
        ]
        self.assertEqual(self.curate(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; landed: #40)", text)
        self.assertIn("- [~] `kolmogorov-zero-one`", text)
        ((file, _),) = incidents.records(incidents.TARGET_CLOSED)
        self.assertEqual(file.name, "target-closed-kolmogorov-zero-one-41.json")
        self.github_reads.assert_called_once_with(200)
        self.forgejo.pr_view.assert_not_called()

    def test_a_closed_github_pull_request_is_matched_to_its_record_by_head(self):
        self.merged_rows = []
        listed = EXAMPLE.replace("in flight: #1234", "in flight: #41")
        self.path.write_text(listed)
        self.github_rows = [
            {"number": 41, "title": "t", "body": "", "state": "CLOSED", "headRefOid": "b" * 40},
            {
                "number": 398,
                "title": "t",
                "body": marker("Probability", "other"),
                "state": "MERGED",
                "headRefOid": "c" * 40,
            },
        ]
        incidents.record_declined("fix", 41, "a" * 40, message="Subsumed-by: #398")
        self.assertEqual(self.curate(self.forgejo)[0], None)
        self.assertEqual(self.path.read_text(), listed)
        incidents.record_declined("rebase", 41, "b" * 40, message="Subsumed-by: #398")
        self.assertEqual(self.curate(self.forgejo), (0, ""))
        self.assertIn("(needs: none; subsumed by: #398; closed: #41)", self.path.read_text())

    def test_a_github_reference_outside_the_github_read_is_left_alone(self):
        self.merged_rows = []
        listed = EXAMPLE.replace("in flight: #1234", "in flight: #40")
        self.path.write_text(listed)
        self.assertEqual(self.curate(self.forgejo), (None, "the target list is current"))
        self.github_rows = [{"number": 40, "title": "t", "body": "", "state": "MERGED"}]
        self.github_reads.side_effect = tc.GitHubError("rate limited")
        with patch.object(t, "log") as said:
            self.assertEqual(self.curate(self.forgejo), (None, "the target list is current"))
        self.assertIn("GitHub pull requests unreadable (rate limited)", said.call_args_list[0].args[0])
        self.github_reads.side_effect = lambda limit: self.github_rows
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_GITHUB": "0"}):
            self.assertEqual(self.curate(self.forgejo), (None, "the target list is current"))
        self.assertEqual((self.github_reads.call_count, self.path.read_text()), (2, listed))

    def test_a_forgejo_pull_request_the_queue_landed_counts_as_merged(self):
        landed = "[Merged by Bors] - t"
        self.merged_rows = [
            {
                "number": 3,
                "title": landed,
                "body": marker("MeasureTheory", "kolmogorov-zero-one"),
                "state": "closed",
                "merged": False,
            }
        ]
        url = self.repo.pull_url(12)
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}"))
        self.states = {12: {"state": "CLOSED", "title": landed, "body": "", "headRefOid": "a" * 40}}
        self.assertEqual(self.curate(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn(f"(needs: none; landed: {url})", text)
        self.assertIn(f"landed: {self.repo.pull_url(3)})", text)
        self.assertEqual(incidents.records(incidents.TARGET_CLOSED), [])
        item = t.parse_targets(EXAMPLE).find("kolmogorov-zero-one")
        self.merged_rows = [
            {
                "number": 3,
                "title": landed,
                "body": marker("MeasureTheory", "kolmogorov-zero-one"),
                "state": "closed",
                "pull_request": {"merged": False},
            }
        ]
        self.assertEqual(t.landed_pr(item, self.forgejo), 3)

    def test_a_mixed_worker_settles_items_from_both_forges(self):
        self.path.write_text(EXAMPLE)
        github = self.github({1234: {"state": "MERGED", "body": ""}})
        self.assertEqual(self.curate(github, [self.forgejo]), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; landed: #1234)", text)
        self.assertIn(f"landed: {self.repo.pull_url(3)})", text)
        self.forgejo.pr_view.assert_not_called()

    def test_the_same_number_on_the_other_forge_is_not_taken_for_it(self):
        self.merged_rows = []
        self.path.write_text(EXAMPLE.replace("in flight: #1234", "in flight: #12"))
        self.states = {12: {"state": "MERGED", "body": ""}, 398: {"state": "MERGED", "body": ""}}
        incidents.record_declined("fix", 12, "a" * 40, message="Subsumed-by: #398", repository=self.repo)
        github = self.github({12: {"state": "CLOSED", "body": "", "headRefOid": "a" * 40}})
        rc, reason = self.curate(github, [self.forgejo])
        self.assertEqual((rc, self.path.read_text()), (None, EXAMPLE.replace("in flight: #1234", "in flight: #12")))
        self.assertIn("1 in-flight item(s) name a closed pull request", reason)
        ((file, value),) = incidents.records(incidents.TARGET_CLOSED)
        self.assertEqual((file.name, value["forge"]["kind"]), ("target-closed-truncation-bounds-12.json", "github"))
        url = self.repo.pull_url(12)
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}"))
        self.states[12] = {"state": "CLOSED", "body": "", "headRefOid": "a" * 40}
        self.assertEqual(self.curate(github, [self.forgejo])[0], 0)
        self.assertIn(f"(needs: none; subsumed by: {self.repo.pull_url(398)}; closed: {url})", self.path.read_text())
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {self.repo.pull_url(13)}"))
        self.states[13] = {"state": "CLOSED", "body": "", "headRefOid": "b" * 40}
        self.curate(github, [self.forgejo])
        names = sorted(file.name for file, _ in incidents.records(incidents.TARGET_CLOSED))
        self.assertEqual(len(names), 2)
        self.assertRegex(names[1], r"^target-closed-truncation-bounds-13\.[0-9a-f]{12}\.json$")


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

    def test_a_round_over_several_sources_curates_once_reading_every_source(self):
        import contextlib

        from tauceti_worker import multi_source

        github = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=SimpleNamespace(repo="TauCetiProject/TauCeti"))
        forgejo = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=SimpleNamespace(direct_source=True))

        def observed(*sources):
            return [(name, worker, tc.Survey(worker_id="test")) for name, worker in sources]

        def ran(only, observations, outcomes, destination="github"):
            opts = work.RoundOpts(only=only, agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
            with (
                patch.dict(os.environ, {"TAUCETI_NEW_PR_FORGE": destination}),
                patch.object(multi_source, "log"),
                patch.object(multi_source, "collect", return_value=observations),
                patch.object(multi_source, "deduplicate"),
                patch.object(multi_source, "activate", return_value=contextlib.nullcontext()) as activated,
                patch.object(work, "run_round", side_effect=outcomes) as execute,
            ):
                try:
                    result = multi_source.run_round(None, opts)
                except tc.NoProgress as error:
                    self.raised, result = error, str(error)
            calls = [
                (call.args[0], call.args[1].only, call.kwargs["prepared_survey"]) for call in execute.call_args_list
            ]
            return result, calls, activated.call_count

        both = observed(("github", github), ("forgejo", forgejo))
        self.assertEqual(ran(["curate"], both, [0]), (0, [(github, ["curate"], both[0][2])], 0))
        self.assertEqual(github.target_sources, [forgejo.gh])
        current = tc.NoProgress("curate: the target list is current")
        result, calls, _ = ran(["roadmap", "curate"], both, [current, 0])
        self.assertEqual((result, [only for _, only, _ in calls]), (0, [["curate"], ["roadmap"]]))
        self.assertEqual(ran(["curate"], both, [current])[:2], ("curate: the target list is current", calls[:1]))
        self.assertEqual(
            ran(["curate", "curate"], both, [current])[:2], ("curate: the target list is current", calls[:1])
        )
        paused = tc.AdmissionUnavailable("storage unavailable", state="waiting-storage")
        self.assertEqual(ran(["curate", "fix"], both, [paused])[0], "storage unavailable")
        self.assertIs(self.raised, paused)
        declined = tc.NoProgress("roadmap: every eligible target is claimed by another worker")
        self.assertEqual(ran(["curate", "roadmap"], both, [paused, declined])[0], "storage unavailable")
        self.assertIs(self.raised, paused)
        alone = observed(("forgejo", forgejo))
        self.assertEqual(ran(["curate"], alone, [0], "forgejo"), (0, [(forgejo, ["curate"], alone[0][2])], 1))
        self.assertEqual(forgejo.target_sources, [])

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

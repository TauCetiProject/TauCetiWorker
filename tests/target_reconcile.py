#!/usr/bin/env python3
"""The `reconcile` phase: what pull request states imply for a target list, the locked three-way
write with real git repositories, and the round wiring. No forge or model access. The fixture is the
example in docs/target-lists.md."""

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
from tauceti_tools.forge import Forge, RepositoryRef

import tauceti_worker as tc
from tauceti_worker import reconcile
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


def seen(merged=(), pulls=None):
    """A GitHub pull request is given by number, any other by URL; a merged one by URL has a `url`.
    Every pull request is into `main` unless it says otherwise."""
    rows = [
        {
            "number": int(str(n).rsplit("/", 1)[-1]),
            "body": body,
            "baseRefName": "main",
            **({"url": n} if isinstance(n, str) else {}),
        }
        for n, body in merged
    ]
    pulls = {ref(n): {"baseRefName": "main", **d} for n, d in (pulls or {}).items()}
    return reconcile.Seen(rows, pulls)


def settled(text, observed):
    return reconcile.apply(text, reconcile.settle(t.parse_targets(text), observed))


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="reconcile-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        for item in (patch.dict(os.environ, GIT_ENV), patch.object(reconcile, "log")):
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
        out = settled(EXAMPLE, seen(merged))
        self.assertIn(
            "- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #7)", out
        )
        self.assertIn("(serves: G2; needs: `borel-cantelli`; landed: #8)", out)
        self.assertIn("- [ ] `strong-law`", out)
        self.assertEqual(settled(out, seen(merged)), out)

    def test_a_marker_merged_into_another_branch_completes_nothing(self):
        rows = seen([(7, marker("Probability", "truncation-bounds"))])
        rows.merged[0]["baseRefName"] = "staging"
        self.assertEqual(settled(EXAMPLE, rows), EXAMPLE)

    def test_the_named_pull_request_decides_an_in_flight_item(self):
        self.assertEqual(settled(EXAMPLE, seen(pulls={1234: {"state": "OPEN"}})), EXAMPLE)
        self.assertEqual(settled(EXAMPLE, seen()), EXAMPLE)
        self.assertEqual(settled(EXAMPLE, seen(pulls={1234: {"state": "MERGED", "baseRefName": "staging"}})), EXAMPLE)
        out = settled(EXAMPLE, seen(pulls={1234: {"state": "MERGED", "body": "no marker"}}))
        self.assertIn(
            "- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #1234)", out
        )
        body = marker("Probability", "truncation-bounds", partial=True)
        out = settled(EXAMPLE, seen(pulls={1234: {"state": "MERGED", "body": body}}))
        self.assertIn(
            "- [ ] `truncation-bounds` — variance bounds for truncated variables (needs: none; partial: #1234)", out
        )
        again = EXAMPLE.replace("in flight: #1234", "partial: #7; in flight: #1234")
        out = settled(again, seen(pulls={1234: {"state": "MERGED", "body": body}}))
        self.assertIn("(needs: none; partial: #7, #1234)", out)

    def test_a_closed_pull_request_opens_its_item_again(self):
        closed = {1234: {"state": "CLOSED", "body": marker("Probability", "truncation-bounds")}}
        out = settled(EXAMPLE, seen(pulls=closed))
        self.assertIn(
            "- [ ] `truncation-bounds` — variance bounds for truncated variables (needs: none; closed: #1234)", out
        )
        self.assertEqual(settled(out, seen(pulls=closed)), out)
        twice = EXAMPLE.replace("in flight: #1234", "closed: #990; in flight: #1234")
        self.assertIn("(needs: none; closed: #990, #1234)", settled(twice, seen(pulls=closed)))
        partial = {1234: {"state": "CLOSED", "body": marker("Probability", "truncation-bounds", partial=True)}}
        self.assertIn("(needs: none; closed: #1234)", settled(EXAMPLE, seen(pulls=partial)))

    def test_a_landing_by_marker_wins_over_the_named_pull_request(self):
        closed = {1234: {"state": "CLOSED", "body": ""}}
        out = settled(EXAMPLE, seen([(7, marker("Probability", "truncation-bounds"))], pulls=closed))
        self.assertIn("(needs: none; landed: #7)", out)

    def test_a_done_item_is_never_touched(self):
        listed = EXAMPLE.replace(
            "- [x] `borel-cantelli` — both Borel–Cantelli lemmas (serves: G1)",
            "- [x] `borel-cantelli` — both Borel–Cantelli lemmas (serves: G1; in flight: #5)",
        )
        self.assertEqual(settled(listed, seen(pulls={5: {"state": "CLOSED"}})), listed)


class NativeReferences(unittest.TestCase):
    def test_a_forgejo_url_matches_whatever_the_case_of_its_repository(self):
        repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "taucetiproject/tauceti")
        canonical = "https://git.example.org/TauCetiProject/TauCeti/pulls/12"
        source = reconcile.Source(None, repo, native=True)
        self.assertEqual((source.number(canonical), source.number(repo.pull_url(12))), (12, 12))
        listed = EXAMPLE.replace(
            "- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2;",
            f"- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; partial: {canonical};",
        )
        out = settled(listed, seen(merged=[(repo.pull_url(12), marker("MeasureTheory", "kolmogorov-zero-one"))]))
        self.assertEqual(out, listed)
        inflight = EXAMPLE.replace("in flight: #1234", f"in flight: {canonical}")
        body = marker("Probability", "truncation-bounds", partial=True)
        out = settled(inflight, seen(pulls={canonical: {"state": "MERGED", "body": body}}))
        self.assertIn(f"(needs: none; partial: {canonical})", out)
        named = inflight.replace("(needs: none;", f"(needs: none; partial: {canonical};")
        out = settled(named, seen(pulls={canonical: {"state": "MERGED", "body": ""}}))
        self.assertIn(f"(needs: none; partial: {canonical})", out)
        self.assertEqual(t.parse_targets(out).find("truncation-bounds").status, "open")
        out = settled(inflight, seen(pulls={canonical: {"state": "CLOSED", "body": ""}}))
        self.assertIn(f"(needs: none; closed: {canonical})", out)


def tick(slug, clause="landed: #7"):
    """A `publish` plan that ticks `slug` wherever it is still open."""

    def plan(listed):
        item = listed.find(slug)
        if item is None or item.status == "done":
            return []
        return [reconcile.Change(item, "done", "n", (), (clause,))]

    return plan


class Publish(Tmp):
    def remote(self):
        return git(self.tmp / "remote.git", "show", "main:targets.md") + "\n"

    def hook(self, clone, name, body):
        hook = clone.parent / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\n" + body)
        hook.chmod(0o755)
        return hook

    def test_an_untracked_list_is_edited_in_place_and_recomputed_on_a_hand_edit(self):
        path = self.tmp / "targets.md"
        path.write_text(EXAMPLE)
        path.chmod(0o640)
        self.assertEqual(reconcile.publish(path, tick("strong-law")), ["`strong-law`: n"])
        self.assertIn("- [x] `strong-law`", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(reconcile.publish(path, tick("strong-law")), [])
        by_hand = EXAMPLE.replace("Goal: the strong law", "Goal: the weak law")
        seen = []

        def edited_meanwhile(listed):
            if not seen:
                path.write_text(by_hand)
            seen.append(listed)
            return tick("kolmogorov-zero-one")(listed)

        path.write_text(EXAMPLE)
        self.assertEqual(len(reconcile.publish(path, edited_meanwhile)), 1)
        self.assertIn("Goal: the weak law", path.read_text())
        self.assertIn("- [x] `kolmogorov-zero-one`", path.read_text())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["targets.md"])

    def test_a_tracked_list_is_published_from_a_worktree_and_the_clean_clone_follows(self):
        first, second = self.clone("first"), self.clone("second")
        self.assertEqual(reconcile.publish(first, tick("strong-law")), ["`strong-law`: n"])
        self.assertEqual(
            git(self.tmp / "remote.git", "log", "-1", "--format=%s%n%n%b", "main"),
            "Reconcile targets.md: 1 item(s)\n\n- `strong-law`: n",
        )
        self.assertEqual(first.read_text(), self.remote())
        self.assertEqual(
            (git(first.parent, "status", "--porcelain"), git(first.parent, "worktree", "list").count("\n")), ("", 0)
        )
        self.assertEqual(reconcile.publish(second, tick("kolmogorov-zero-one")), ["`kolmogorov-zero-one`: n"])
        both = self.remote()
        self.assertIn("- [x] `strong-law`", both)
        self.assertIn("- [x] `kolmogorov-zero-one`", both)
        self.assertEqual(second.read_text(), both)
        self.assertEqual(reconcile.publish(first, tick("strong-law")), [])

    def test_edits_are_computed_on_the_published_list_not_merged_into_it(self):
        first, second = self.clone("first"), self.clone("second")
        second.write_text(EXAMPLE.replace("## Probability", "## Statistics"))
        git(second.parent, "commit", "-q", "-am", "rename the area")
        git(second.parent, "push", "-q", "origin", "HEAD:main")

        def settle_probability(listed):
            item = next((it for it in listed.areas.get("Probability", []) if it.slug == "strong-law"), None)
            return [] if item is None else [reconcile.Change(item, "done", "n", (), ("landed: #42",))]

        self.assertEqual(reconcile.publish(first, settle_probability), [])
        self.assertNotIn("landed: #42", self.remote())
        self.assertEqual(first.read_text(), self.remote())

    def test_a_rejected_commit_leaves_nothing_and_a_later_look_publishes(self):
        first = self.clone("first")
        hook = self.hook(
            first,
            "pre-commit",
            "sed 's/the strong law (/the strong law. (/' targets.md > t.tmp && mv t.tmp targets.md\nexit 1\n",
        )
        self.assertIsNone(reconcile.publish(first, tick("strong-law")))
        self.assertEqual((first.read_text(), self.remote()), (EXAMPLE, EXAMPLE))
        self.assertEqual(git(first.parent, "status", "--porcelain"), "")
        hook.unlink()
        self.assertEqual(len(reconcile.publish(first, tick("strong-law"))), 1)
        self.assertIn("- [x] `strong-law`", self.remote())

    def test_a_partial_history_survives_a_rejected_commit_and_its_retry(self):
        listed = EXAMPLE.replace("in flight: #1234", "partial: #7; in flight: #1234")
        first = self.clone("first")
        first.write_text(listed)
        git(first.parent, "commit", "-q", "-am", "partial")
        git(first.parent, "push", "-q", "origin", "HEAD:main")
        observed = seen(
            pulls={1234: {"state": "MERGED", "body": marker("Probability", "truncation-bounds", partial=True)}}
        )

        def compute(listed):
            return reconcile.settle(listed, observed)

        hook = self.hook(first, "pre-commit", "exit 1\n")
        self.assertIsNone(reconcile.publish(first, compute))
        hook.unlink()
        self.assertEqual(len(reconcile.publish(first, compute)), 1)
        self.assertIn("(needs: none; partial: #7, #1234)", self.remote())

    def test_an_operators_uncommitted_edit_is_left_alone(self):
        first = self.clone("first")
        by_hand = EXAMPLE.replace("- [ ] `strong-law`", "- [~] `strong-law`")
        first.write_text(by_hand)
        self.assertEqual(len(reconcile.publish(first, tick("kolmogorov-zero-one"))), 1)
        self.assertEqual(first.read_text(), by_hand)
        self.assertIn("- [x] `kolmogorov-zero-one`", self.remote())
        self.assertNotIn("- [~] `strong-law`", self.remote())

    def test_a_list_named_like_a_glob_is_committed_alone(self):
        first = self.clone("first")
        named = first.with_name("targets[1].md")
        git(first.parent, "mv", "targets.md", named.name)
        (first.parent / "targets1.md").write_text("notes\n")
        git(first.parent, "add", "targets1.md")
        git(first.parent, "commit", "-q", "-m", "rename")
        git(first.parent, "push", "-q", "origin", "HEAD:main")
        (first.parent / "targets1.md").write_text("unfinished notes\n")
        self.assertEqual(len(reconcile.publish(named, tick("strong-law"))), 1)
        self.assertEqual(git(self.tmp / "remote.git", "show", "--name-only", "--format=", "main"), "targets[1].md")
        self.assertEqual(git(self.tmp / "remote.git", "show", "main:targets1.md"), "notes")
        self.assertEqual(git(first.parent, "status", "--porcelain"), "M targets1.md")

    def test_a_list_whose_name_git_would_quote_is_published(self):
        first = self.clone("first")
        named = first.with_name("cibles é.md")
        git(first.parent, "mv", "targets.md", named.name)
        git(first.parent, "commit", "-q", "-m", "rename")
        git(first.parent, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(len(reconcile.publish(named, tick("strong-law"))), 1)
        self.assertIn("- [x] `strong-law`", git(self.tmp / "remote.git", "show", "main:cibles é.md"))

    def test_a_commit_hook_keeps_its_own_glob_matching(self):
        first = self.clone("first")
        self.hook(first, "pre-commit", "[ -z \"$(git diff --cached --name-only -- '*.md')\" ] || exit 1\n")
        self.assertIsNone(reconcile.publish(first, tick("strong-law")))
        self.assertEqual(self.remote(), EXAMPLE)

    def test_a_hook_that_rewrites_the_list_stops_publication(self):
        first = self.clone("first")
        self.hook(
            first,
            "pre-commit",
            "sed 's/the strong law for/the weak law for/' targets.md > t.tmp && mv t.tmp targets.md && git add targets.md\n",
        )
        self.assertIsNone(reconcile.publish(first, tick("strong-law")))
        self.assertEqual(self.remote(), EXAMPLE)

    def test_a_list_the_remote_turned_into_a_symlink_is_not_followed(self):
        first, second = self.clone("first"), self.clone("second")
        outside = self.tmp / "outside.md"
        outside.write_text(EXAMPLE)
        second.unlink()
        second.symlink_to(outside)
        git(second.parent, "commit", "-q", "-am", "link")
        git(second.parent, "push", "-q", "origin", "HEAD:main")
        self.assertIsNone(reconcile.publish(first, tick("strong-law")))
        self.assertEqual(outside.read_text(), EXAMPLE)

    def test_a_tracked_list_keeps_its_own_git_settings_inside_a_source_activation(self):
        from tauceti_worker.source_identity import SourceActor
        from tauceti_worker.worker_sources import activate

        first = self.clone("first")
        hook = self.hook(first, "pre-commit", "exit 1\n")
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
        with activate(source, SimpleNamespace(state=self.tmp / "native-state")):
            self.assertIsNone(reconcile.publish(first, tick("strong-law")))
            hook.unlink()
            self.assertEqual(len(reconcile.publish(first, tick("strong-law"))), 1)
        self.assertEqual(git(self.tmp / "remote.git", "log", "-1", "--format=%an", "main"), "curator")

    def test_a_refused_push_leaves_nothing_and_a_later_look_publishes(self):
        first = self.clone("first")
        git(first.parent, "remote", "set-url", "--push", "origin", str(self.tmp / "nonexistent.git"))
        self.assertIsNone(reconcile.publish(first, tick("strong-law")))
        self.assertEqual(
            (first.read_text(), git(first.parent, "rev-list", "--count", "@{upstream}..HEAD")), (EXAMPLE, "0")
        )
        git(first.parent, "remote", "set-url", "--push", "origin", str(self.tmp / "remote.git"))
        self.assertEqual(len(reconcile.publish(first, tick("strong-law"))), 1)

    def test_the_first_publication_creates_the_branch_an_empty_remote_lacks(self):
        remote = self.tmp / "empty.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        git(self.tmp, "clone", "-q", str(remote), str(self.tmp / "fresh"))
        path = self.tmp / "fresh" / "targets.md"
        path.write_text(EXAMPLE)
        git(path.parent, "add", "targets.md")
        git(path.parent, "commit", "-q", "-m", "list")
        self.assertEqual(len(reconcile.publish(path, tick("strong-law"))), 1)
        self.assertIn("- [x] `strong-law`", git(remote, "show", "main:targets.md"))

    def test_a_clone_with_uncommitted_edits_or_its_own_commits_is_not_moved(self):
        first, second = self.clone("first"), self.clone("second")
        by_hand = EXAMPLE.replace("Goal: the strong law", "Goal: the weak law")
        second.write_text(by_hand)
        head = git(second.parent, "rev-parse", "HEAD")
        reconcile.publish(first, tick("strong-law"))
        git(second.parent, "fetch", "-q")
        reconcile._sync(second)
        self.assertEqual((git(second.parent, "rev-parse", "HEAD"), second.read_text()), (head, by_hand))
        git(second.parent, "commit", "-q", "-am", "own")
        own = git(second.parent, "rev-parse", "HEAD")
        reconcile._sync(second)
        self.assertEqual(git(second.parent, "rev-parse", "HEAD"), own)


class TrackedRound(Tmp):
    """Whole looks on a list tracked on a remote branch, as the phase runs them."""

    def setUp(self):
        super().setUp()
        self.first = self.clone("first")
        (self.first.parent / "notes.md").write_text("notes\n")
        git(self.first.parent, "add", "notes.md")
        git(self.first.parent, "commit", "-q", "-m", "notes")
        git(self.first.parent, "push", "-q", "origin", "HEAD:main")
        self.second = self.clone("second")
        env = patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.first), "HOME": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg))
        self.github()

    def github(self, merged=(), states=None):
        self.w.gh = SimpleNamespace(
            merged_prs=lambda search, limit: [{"number": n, "body": body, "baseRefName": "main"} for n, body in merged],
            open_prs=lambda: [],
            pr_view=lambda number, fields: {"baseRefName": "main", **((states or {}).get(number) or {"state": "OPEN"})},
        )

    def look(self):
        lock = self.first.with_name(".targets.md.lock")
        if lock.exists():
            os.utime(lock, (0, 0))
        return reconcile.run(self.w, tc.Candidate(0, "", ""))

    def remote(self, name="targets.md"):
        return git(self.tmp / "remote.git", "show", f"main:{name}") + "\n"

    def hook(self, directory, body):
        directory.mkdir(exist_ok=True)
        (directory / "pre-commit").write_text("#!/bin/sh\n" + body)
        (directory / "pre-commit").chmod(0o755)

    def test_a_look_observes_the_published_list_not_a_stale_clone(self):
        self.first.write_text(EXAMPLE.replace("Goal: the strong law", "Goal (draft): the strong law"))
        self.second.write_text(EXAMPLE.replace("in flight: #1234", "in flight: #43"))
        git(self.second.parent, "commit", "-q", "-am", "moved")
        git(self.second.parent, "push", "-q", "origin", "HEAD:main")
        self.github(states={1234: {"state": "CLOSED"}, 43: {"state": "CLOSED"}})
        self.assertEqual(self.look(), 0)
        self.assertIn("(needs: none; closed: #43)", self.remote())

    def test_a_relative_hook_directory_of_the_clone_still_runs(self):
        git(self.first.parent, "config", "core.hooksPath", ".local-hooks")
        self.hook(self.first.parent / ".local-hooks", "exit 1\n")
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual(self.remote(), EXAMPLE)

    def test_a_hook_that_moves_the_item_to_another_area_is_not_pushed(self):
        self.hook(
            self.first.parent / ".git" / "hooks",
            "sed 's/^## Probability$/## Statistics/' targets.md > t.tmp && mv t.tmp targets.md && git add targets.md\n",
        )
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual(self.remote(), EXAMPLE)

    def test_a_relative_pre_push_hook_of_the_clone_still_runs(self):
        git(self.first.parent, "config", "core.hooksPath", ".local-hooks")
        directory = self.first.parent / ".local-hooks"
        directory.mkdir()
        (directory / "pre-push").write_text("#!/bin/sh\nexit 1\n")
        (directory / "pre-push").chmod(0o755)
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual(self.remote(), EXAMPLE)

    def test_a_hook_that_adds_a_partial_clause_stops_publication(self):
        self.hook(
            self.first.parent / ".git" / "hooks",
            "sed 's/landed: #7)/landed: #7; partial: #7)/' targets.md > t.tmp && mv t.tmp targets.md && git add targets.md\n",
        )
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual(self.remote(), EXAMPLE)

    def test_extra_commits_a_post_commit_hook_makes_are_not_pushed(self):
        hooks = self.first.parent / ".git" / "hooks"
        (hooks / "post-commit").write_text(
            "#!/bin/sh\ngrep -q 'generated' targets.md && exit 0\n"
            "printf 'formatted notes\\n' > notes.md && git commit -q -m 'format notes' -- notes.md\n"
            "echo '> generated' >> targets.md && git commit -q -m 'generated' -- targets.md\n"
        )
        (hooks / "post-commit").chmod(0o755)
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual((self.remote(), self.remote("notes.md")), (EXAMPLE, "notes\n"))

    def test_a_hook_that_stages_another_file_is_not_pushed(self):
        self.hook(self.first.parent / ".git" / "hooks", "printf 'formatted notes\\n' > notes.md\ngit add '*.md'\n")
        self.github(merged=[(7, marker("Probability", "strong-law"))])
        self.assertIsNone(self.look())
        self.assertEqual((self.remote(), self.remote("notes.md")), (EXAMPLE, "notes\n"))


class Round(Tmp):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "list" / "targets.md"
        self.path.parent.mkdir()
        self.path.write_text(EXAMPLE)
        env = patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=self.github())

    def github(self, merged=(), states=None):
        return SimpleNamespace(
            merged_prs=lambda search, limit: [{"number": n, "body": body, "baseRefName": "main"} for n, body in merged],
            open_prs=lambda: [],
            pr_view=lambda number, fields: {
                "baseRefName": "main",
                **((states or {}).get(number) or {"state": "OPEN", "title": ""}),
            },
        )

    def reconcile(self, *, after_gap=True):
        lock = self.path.with_name(".targets.md.lock")
        if after_gap and lock.exists():
            old = time.time() - reconcile.GAP - 60
            os.utime(lock, (old, old))
        c = tc.Candidate(0, "", "")
        return reconcile.run(self.w, c), c.reason

    def test_a_round_ticks_what_merged_and_reopens_what_closed(self):
        self.w.gh = self.github(
            merged=[(8, marker("MeasureTheory", "kolmogorov-zero-one"))],
            states={1234: {"state": "CLOSED", "title": "Truncation bounds"}},
        )
        self.assertEqual(self.reconcile(), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; closed: #1234)", text)
        self.assertIn("(serves: G2; needs: `borel-cantelli`; landed: #8)", text)
        self.assertEqual(self.reconcile(), (None, "the target list is current"))
        self.assertLessEqual(time.time() - self.w.counters.read(reconcile.ATTEMPT_KEY), 5)

    def test_a_queue_title_counts_as_merged_only_with_the_sources_landing_proof(self):
        queued = {"state": "CLOSED", "title": "[Merged by Bors] - Truncation bounds"}
        claimed = [(8, marker("MeasureTheory", "kolmogorov-zero-one"))]
        self.w.gh = self.github(states={1234: queued})
        self.assertEqual(self.reconcile(), (None, "the target list is current"))
        self.assertEqual(self.path.read_text(), EXAMPLE)
        asked = []
        for proof, item in ((False, "(needs: none; closed: #1234)"), (True, "(needs: none; landed: #1234)")):
            self.path.write_text(EXAMPLE)
            self.w.gh = self.github(states={1234: queued})
            self.w.gh.merged_prs = lambda search, limit: [
                {"number": n, "body": body, "baseRefName": "main", "state": "CLOSED", "title": queued["title"]}
                for n, body in claimed
            ]
            self.w.gh.target_queue_landed = lambda number, branch, proof=proof: asked.append(number) or proof
            self.assertEqual(self.reconcile(), (0, ""))
            text = self.path.read_text()
            self.assertIn(item, text)
            self.assertEqual("landed: #8" in text, proof)
        self.assertEqual(sorted(asked), [8, 8, 1234, 1234])

    def test_a_queue_landing_on_another_base_leaves_the_item_alone(self):
        queued = {"state": "CLOSED", "title": "[Merged by Bors] - Truncation bounds", "baseRefName": "staging"}
        self.w.gh = self.github(states={1234: queued})
        self.w.gh.target_queue_landed = lambda number, branch: branch == "staging"
        self.assertEqual(self.reconcile(), (None, "the target list is current"))
        self.assertEqual(self.path.read_text(), EXAMPLE)

    def test_one_look_at_a_time_and_none_without_reads(self):
        lock = os.open(self.path.with_name(".targets.md.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.reconcile(), (None, "another worker is reconciling the target list"))
        fcntl.flock(lock, fcntl.LOCK_UN)

        def fail(search, limit):
            raise tc.GitHubError("gh pr list failed: HTTP 504")

        self.w.gh.merged_prs = fail
        rc, reason = self.reconcile()
        self.assertEqual((rc, self.path.read_text()), (None, EXAMPLE))
        self.assertIn("HTTP 504", reason)
        self.path.write_text("no marker\n")
        with self.assertRaisesRegex(tc.Die, "roadmap targets: .*targets.md: .*tauceti-targets:v1"):
            self.reconcile()

    def test_no_list_no_look(self):
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}):
            self.assertEqual(self.reconcile(), (None, "no target list is set (--roadmap-targets)"))

    def test_the_gap_is_shared_through_the_lock_file(self):
        self.assertEqual(reconcile.due(self.w.counters), (True, "the target list is due a look"))
        self.reconcile(after_gap=False)
        due, reason = reconcile.due(self.w.counters)
        self.assertFalse(due)
        self.assertIn("looked at 0.0h ago; it is looked at every 6h", reason)
        other = tc.Counters(SimpleNamespace(state=self.tmp / "other"))
        self.assertFalse(reconcile.due(other)[0])
        old = time.time() - reconcile.GAP - 60
        os.utime(self.path.with_name(".targets.md.lock"), (old, old))
        self.assertTrue(reconcile.due(other)[0])
        self.assertFalse(reconcile.due(self.w.counters)[0])
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}):
            self.assertEqual(reconcile.due(other), (False, "no target list is set (--roadmap-targets)"))

    def test_a_worker_that_was_due_before_another_looked_does_not_look_again(self):
        self.assertTrue(reconcile.due(self.w.counters)[0])
        self.reconcile()
        self.w.counters = tc.Counters(SimpleNamespace(state=self.tmp / "other"))
        rc, reason = self.reconcile(after_gap=False)
        self.assertIsNone(rc)
        self.assertIn("another worker looked at the target list 0.0h ago", reason)

    def test_a_first_look_that_stops_early_leaves_the_list_due(self):
        with (
            patch.object(reconcile, "_reconcile", side_effect=tc.Die("admission refused")),
            self.assertRaises(tc.Die),
        ):
            self.reconcile()
        self.assertTrue(self.path.with_name(".targets.md.lock").exists())
        self.w.counters.write(reconcile.ATTEMPT_KEY, 0)
        self.assertTrue(reconcile.due(self.w.counters)[0])
        self.assertTrue(reconcile.due(tc.Counters(SimpleNamespace(state=self.tmp / "other")))[0])


class ForgejoLists(Tmp):
    """Reconciliation over a Forgejo source, built as upstream's Forgejo tests build one: a real
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
        api.pages.side_effect = lambda path: iter([[{"base": {"ref": "main"}, **r} for r in self.merged_rows]])
        api.pull.side_effect = lambda repository, number: {"number": number, "base": {"ref": "main"}}
        self.forgejo.open_prs = Mock(return_value=[])
        self.states = {}
        self.forgejo.pr_view = Mock(
            side_effect=lambda number, fields: {"baseRefName": "main", **self.states.get(number, {"state": "OPEN"})}
        )
        self.path = self.tmp / "targets.md"
        self.github_rows = []
        self.github_reads = Mock(side_effect=lambda limit: [{"baseRefName": "main", **r} for r in self.github_rows])
        for item in (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": str(self.path)}),
            patch("tauceti_worker.worker_sources.shared_github_environment", contextlib.nullcontext),
            patch("tauceti_worker.github.GitHub.target_marker_prs", lambda _gh, *, limit: self.github_reads(limit)),
        ):
            item.start()
            self.addCleanup(item.stop)
        cfg = SimpleNamespace(state=self.tmp / "state", logdir=self.tmp / "logs", wid="worker3")
        cfg.state.mkdir()
        self.counters = tc.Counters(cfg)
        self.cfg = cfg

    def reconcile(self, gh, others=()):
        w = SimpleNamespace(cfg=self.cfg, counters=self.counters, gh=gh, target_sources=list(others))
        lock = self.path.with_name(".targets.md.lock")
        if lock.exists():
            os.utime(lock, (0, 0))
        c = tc.Candidate(0, "", "")
        return reconcile.run(w, c), c.reason

    def github(self, states):
        return SimpleNamespace(
            repo="TauCetiProject/TauCeti",
            merged_prs=lambda search, limit: [],
            open_prs=lambda: [],
            pr_view=lambda number, fields: {"baseRefName": "main", **(states.get(number) or {"state": "OPEN"})},
        )

    def test_a_look_on_a_forgejo_source_is_not_declined_for_want_of_a_pull_request(self):
        w = SimpleNamespace(gh=self.forgejo)
        self.assertIs(
            work._still_actionable("reconcile", w, tc.Survey(worker_id="test"), tc.Candidate(0, "", "")), True
        )

    def test_a_forgejo_only_worker_settles_what_landed_on_forgejo(self):
        url = self.repo.pull_url(12)
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}"))
        self.states = {12: {"state": "MERGED", "body": ""}}
        self.assertEqual(self.reconcile(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn(f"(needs: none; landed: {url})", text)
        self.assertIn(f"needs: `borel-cantelli`; landed: {self.repo.pull_url(3)})", text)
        self.forgejo.pr_view.assert_called_once_with(12, ["state", "title", "body", "baseRefName"])

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
        self.assertEqual(self.reconcile(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; landed: #40)", text)
        self.assertIn("- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2;", text)
        self.assertIn("closed: #41", text)
        self.github_reads.assert_called_once_with(200)
        self.forgejo.pr_view.assert_not_called()

    def test_a_github_reference_outside_the_github_read_is_left_alone(self):
        self.merged_rows = []
        listed = EXAMPLE.replace("in flight: #1234", "in flight: #40")
        self.path.write_text(listed)
        self.assertEqual(self.reconcile(self.forgejo), (None, "the target list is current"))
        self.github_rows = [{"number": 40, "title": "t", "body": "", "state": "MERGED"}]
        self.github_reads.side_effect = tc.GitHubError("rate limited")
        with patch.object(t, "log") as said:
            self.assertEqual(self.reconcile(self.forgejo), (None, "the target list is current"))
        self.assertIn("GitHub pull requests unreadable (rate limited)", said.call_args_list[0].args[0])
        self.github_reads.side_effect = lambda limit: self.github_rows
        with patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS_GITHUB": "0"}):
            self.assertEqual(self.reconcile(self.forgejo), (None, "the target list is current"))
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
        self.states = {12: {"state": "CLOSED", "title": landed, "body": ""}}
        with patch.object(self.forgejo, "target_queue_landed", return_value=True, create=True) as proof:
            self.assertEqual(self.reconcile(self.forgejo), (0, ""))
        text = self.path.read_text()
        self.assertIn(f"(needs: none; landed: {url})", text)
        self.assertIn(f"landed: {self.repo.pull_url(3)})", text)
        self.assertEqual(sorted(call.args for call in proof.call_args_list), [(3, "main"), (12, "main")])

    def test_an_in_flight_url_is_read_whatever_its_scheme_case_or_trailing_slash(self):
        self.merged_rows = []
        self.states = {12: {"state": "MERGED", "body": ""}}
        url = self.repo.pull_url(12)
        for written in ("HTTPS" + url[len("https") :], url + "/"):
            self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {written}"))
            self.assertEqual(self.reconcile(self.forgejo)[0], 0, written)
            self.assertIn(f"(needs: none; landed: {written})", self.path.read_text())

    def test_a_partial_url_with_an_uppercase_scheme_is_still_an_exclusion(self):
        self.merged_rows = []
        url = self.repo.pull_url(7)
        upper = "HTTPS" + url[len("https") :]
        full = marker("Probability", "truncation-bounds")
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}; partial: {upper}"))
        self.states = {7: {"state": "MERGED", "body": full}}
        self.assertEqual(self.reconcile(self.forgejo)[0], 0)
        self.assertIn(
            f"- [ ] `truncation-bounds` — variance bounds for truncated variables (needs: none; partial: {upper})",
            self.path.read_text(),
        )

    def test_a_named_pull_request_merged_into_another_branch_settles_nothing(self):
        self.merged_rows = []
        url = self.repo.pull_url(12)
        listed = EXAMPLE.replace("in flight: #1234", f"in flight: {url}")
        self.path.write_text(listed)
        self.states = {12: {"state": "MERGED", "body": "", "baseRefName": "staging"}}
        self.assertEqual(self.reconcile(self.forgejo)[0], None)
        self.assertEqual(self.path.read_text(), listed)
        github = self.github({1234: {"state": "MERGED", "body": "", "baseRefName": "staging"}})
        self.path.write_text(EXAMPLE)
        self.assertEqual(self.reconcile(github)[0], None)
        self.assertEqual(self.path.read_text(), EXAMPLE)

    def test_a_mixed_worker_settles_items_from_both_forges(self):
        self.path.write_text(EXAMPLE)
        github = self.github({1234: {"state": "MERGED", "body": ""}})
        self.assertEqual(self.reconcile(github, [self.forgejo]), (0, ""))
        text = self.path.read_text()
        self.assertIn("(needs: none; landed: #1234)", text)
        self.assertIn(f"landed: {self.repo.pull_url(3)})", text)
        self.forgejo.pr_view.assert_not_called()

    def test_the_same_number_on_the_other_forge_is_not_taken_for_it(self):
        self.merged_rows = []
        self.path.write_text(EXAMPLE.replace("in flight: #1234", "in flight: #12"))
        self.states = {12: {"state": "MERGED", "body": ""}}
        github = self.github({12: {"state": "CLOSED", "body": ""}})
        self.assertEqual(self.reconcile(github, [self.forgejo])[0], 0)
        self.assertIn("(needs: none; closed: #12)", self.path.read_text())
        url = self.repo.pull_url(12)
        self.path.write_text(EXAMPLE.replace("in flight: #1234", f"in flight: {url}"))
        self.assertEqual(self.reconcile(github, [self.forgejo])[0], 0)
        self.assertIn(f"(needs: none; landed: {url})", self.path.read_text())


class Wiring(Tmp):
    def round(self, only, *, due=(True, "the target list is due a look"), outcome=0, **opts):
        seen = []

        def dispatch(stage, w, sv, c, o):
            seen.append(stage)
            if stage == "reconcile" and outcome is None:
                c.reason = "the target list is current"
            return outcome if stage == "reconcile" else 0

        sv = tc.Survey(worker_id="test")
        sv.needs_fix.actionable.append(tc.Candidate(5, "a" * 40, "review findings"))
        worker = SimpleNamespace(cfg=SimpleNamespace(wid="test"), gh=None, rs=None, counters=None)
        with (
            patch.object(work, "log"),
            patch.object(work, "survey", return_value=sv),
            patch.object(work, "dispatch", side_effect=dispatch),
            patch.object(reconcile, "due", return_value=due) as asked,
        ):
            try:
                work.run_round(worker, SimpleNamespace(only=only, dry_run=True, **opts))
            except tc.NoProgress as error:
                seen.append(str(error))
        return seen, asked.call_count

    def test_only_a_round_that_names_the_phase_reconciles(self):
        self.assertEqual(self.round([]), (["fix"], 0))
        self.assertEqual(self.round(["fix"]), (["fix"], 0))
        self.assertEqual(self.round(["fix", "reconcile"]), (["reconcile"], 1))
        self.assertEqual(self.round(["fix", "reconcile"], outcome=None), (["reconcile", "fix"], 1))
        self.assertEqual(self.round(["fix", "reconcile"], due=(False, "looked at 1.0h ago")), (["fix"], 1))
        self.assertEqual(self.round(["fix", "reconcile"], prs=(5,)), (["fix"], 0))
        self.assertEqual(
            self.round(["reconcile"], outcome=None), (["reconcile", "reconcile: the target list is current"], 1)
        )
        self.assertEqual(
            self.round(["reconcile"], due=(False, "looked at 1.0h ago")), (["reconcile: looked at 1.0h ago"], 1)
        )

    def test_a_round_over_several_sources_reconciles_once_reading_every_source(self):
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
        self.assertEqual(ran(["reconcile"], both, [0]), (0, [(github, ["reconcile"], both[0][2])], 0))
        self.assertEqual(github.target_sources, [forgejo.gh])
        current = tc.NoProgress("reconcile: the target list is current")
        result, calls, _ = ran(["roadmap", "reconcile"], both, [current, 0])
        self.assertEqual((result, [only for _, only, _ in calls]), (0, [["reconcile"], ["roadmap"]]))
        self.assertEqual(ran(["reconcile"], both, [current])[:2], ("reconcile: the target list is current", calls[:1]))
        paused = tc.AdmissionUnavailable("storage unavailable", state="waiting-storage")
        self.assertEqual(ran(["reconcile", "fix"], both, [paused])[0], "storage unavailable")
        self.assertIs(self.raised, paused)
        declined = tc.NoProgress("roadmap: every eligible target is claimed by another worker")
        self.assertEqual(ran(["reconcile", "roadmap"], both, [paused, declined])[0], "storage unavailable")
        self.assertIs(self.raised, paused)
        alone = observed(("forgejo", forgejo))
        self.assertEqual(ran(["reconcile"], alone, [0], "forgejo"), (0, [(forgejo, ["reconcile"], alone[0][2])], 1))
        self.assertEqual(forgejo.target_sources, [])

    def test_the_task_is_explicit_runs_on_the_host_and_starts_no_agent(self):
        self.assertEqual((work.want([], "reconcile"), work.want(["reconcile"], "reconcile")), (False, True))
        self.assertEqual(tc.resolve_tasks(["roadmap,reconcile"], []), ["roadmap", "reconcile"])
        self.assertNotIn("reconcile", tc.resolve_tasks([], ["review"]))
        self.assertNotIn("reconcile", tc.ALLOWED_TASKS)
        opts = work.RoundOpts(only=["reconcile"], agent="codex", work_model="codex", sandbox_host=False, dry_run=False)
        self.assertFalse(work._bubble("reconcile", opts))
        cfg = SimpleNamespace(state=self.tmp, logdir=self.tmp / "logs", wid="worker3")
        w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=SimpleNamespace(repo="org/repo"))
        due = tc.Candidate(0, "", "the target list is due a look")
        with (
            patch.dict(os.environ, {"TAUCETI_RUNTIME_STATUS": str(self.tmp / "status.json")}),
            patch.object(work, "log") as said,
            patch.object(work.shutil, "which", return_value=None),
            patch.object(work, "_effective_authoring_profile", side_effect=AssertionError("no agent")),
            patch.object(reconcile, "run", return_value=None) as run,
        ):
            self.assertIsNone(work.dispatch("reconcile", w, tc.Survey(worker_id="worker3"), due, opts))
        self.assertIs(run.call_args.args[1], due)
        self.assertIn("→ RECONCILE: the target list is due a look   [no model]", said.call_args_list[0].args[0])
        spec = tc.worker_manager.WorkerSpec.from_dict({"id": "w", "only": ["roadmap", "reconcile"]}, 0)
        self.assertEqual(spec.only, ("roadmap", "reconcile"))
        args = tc.build_parser().parse_args(["work", "--only", "reconcile", "--dry-run"])
        with (
            patch.dict(os.environ, {"TAUCETI_ROADMAP_TARGETS": ""}),
            self.assertRaisesRegex(tc.Die, "needs a target list"),
        ):
            tc.cmd_work(args, only=["reconcile"], agent="claude", one_round=True)


class ModelFree(Tmp):
    """A reconciliation-only round needs no provider: none is chosen, paced or bridged."""

    def test_dispatch_in_api_billing_mode_enters_no_model_bridge(self):
        opts = work.RoundOpts(only=["reconcile"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False)
        cfg = SimpleNamespace(state=self.tmp, logdir=self.tmp / "logs", wid="worker3")
        w = SimpleNamespace(cfg=cfg, counters=tc.Counters(cfg), gh=SimpleNamespace(repo="org/repo"))
        with (
            patch.dict(os.environ, {"TAUCETI_RUNTIME_STATUS": str(self.tmp / "status.json")}),
            patch.object(work, "log"),
            patch("tauceti_worker.claude_api.api_mode", return_value=True),
            patch("tauceti_worker.claude_api.api_context", side_effect=AssertionError("no model bridge")),
            patch.object(reconcile, "run", return_value=0) as run,
        ):
            self.assertEqual(
                work.dispatch("reconcile", w, tc.Survey(worker_id="worker3"), tc.Candidate(0, "", ""), opts), 0
            )
        run.assert_called_once()

    def test_a_one_shot_reconciliation_chooses_no_model(self):
        cli = importlib.import_module("tauceti_worker.cli")
        self.assertTrue(work.model_free_selection(["reconcile"]))
        self.assertFalse(work.model_free_selection(["reconcile", "fix"]))
        self.assertFalse(work.model_free_selection([]))
        paced = dict(dry=False, ignore_quota=False, quota_cmd=None, fresh=True, already_paced=False)
        with (
            patch.object(cli, "resolve_work_model", side_effect=tc.NoProgress("no model under pace right now")),
            patch.object(cli, "api_mode", return_value=False),
        ):
            self.assertEqual(cli.round_work_model(None, ["reconcile"], "claude", **paced), "claude")
            with self.assertRaisesRegex(tc.NoProgress, "no model under pace"):
                cli.round_work_model(None, ["reconcile", "fix"], "claude", **paced)

    def test_a_reconciliation_loop_launches_without_a_model_under_pace(self):
        loop = importlib.import_module("tauceti_worker.loop")
        args = tc.build_parser().parse_args(["work", "--only", "reconcile", "--loop"])

        def sleep(seconds):
            raise KeyboardInterrupt

        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid", "HOME": str(self.tmp)}),
            patch.object(loop, "choose_model", side_effect=AssertionError("no provider is chosen")),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", return_value=0) as launched,
            patch.object(loop, "report_runtime"),
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            for agent in ("claude", "auto"):
                launched.reset_mock()
                loop.cmd_loop(args, SimpleNamespace(wid="fixture"), only=["reconcile"], agent=agent)
                launched.assert_called()


if __name__ == "__main__":
    unittest.main()

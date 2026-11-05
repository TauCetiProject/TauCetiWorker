#!/usr/bin/env python3
"""The attributed chain tool (tauceti_queue.chain) against real Git fixtures.

Every test builds a small repository in a temp directory: a base commit, PR branches retained as
refs/pull/N/head, and no network. Covered: a three-member build, a member that becomes empty, a
conflict and the clean-state guarantee, replay onto an advanced base, validation of equivalent and
broken chains, compare's provenance and append-only message check, and the CLI JSON round trip.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain as c  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


class Fixture(unittest.TestCase):
    """A repository with a base and helper methods for PR branches and raw commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write({"README.md": "readme\n", "Shared.lean": "line one\nline two\nline three\n"})
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args, input=None, env=None):
        full_env = dict(os.environ, **(env or {}))
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            input=input,
            capture_output=True,
            text=True,
            check=True,
            env=full_env,
        ).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            (self.repo / name).write_text(text)

    def commit(self, message, author="Fixture <fixture@example.invalid>"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, f"--author={author}")
        return self.git("rev-parse", "HEAD")

    def pr(self, number, files, start=None, author="Alice <alice@example.invalid>", extra=()):
        """A PR branch from `start` (default: the base) touching `files`; returns a Member."""
        self.git("checkout", "-q", "--detach", start or self.base)
        self.write(files)
        head = self.commit(f"pr {number}", author=author)
        for more_files, more_author in extra:
            self.write(more_files)
            head = self.commit(f"pr {number} more", author=more_author)
        self.git("update-ref", f"refs/pull/{number}/head", head)
        self.git("checkout", "-q", "main")
        return c.Member(PROJECT, number, head, title=f"Change {number}", description=f"Body of PR {number}.")

    def messages(self, base, tip):
        return [commit.message for commit in c.chain_commits(self.repo, base, tip)]

    def raw_commit(self, tree, parent, message, *more_parents):
        args = ["commit-tree", tree, "-p", parent]
        for extra in more_parents:
            args += ["-p", extra]
        return self.git(*args, "-F", "-", input=message)

    def edit_tree(self, tree, path, content):
        """A copy of a flat tree with `path` replaced by `content`."""
        blob = self.git("hash-object", "-w", "--stdin", input=content)
        entries = []
        replaced = False
        for line in self.git("ls-tree", tree).splitlines():
            meta, name = line.split("\t", 1)
            mode, kind, sha = meta.split()
            if name == path:
                sha, replaced = blob, True
            entries.append(f"{mode} {kind} {sha}\t{name}")
        if not replaced:
            entries.append(f"100644 blob {blob}\t{path}")
        return self.git("mktree", input="\n".join(entries) + "\n")

    def three_members(self):
        return [
            self.pr(101, {"A.lean": "a\n"}),
            self.pr(102, {"B.lean": "b\n"}, author="Bob <bob@example.invalid>"),
            self.pr(103, {"C.lean": "c\n"}),
        ]


class Build(Fixture):
    def test_three_members_with_distinct_files(self):
        members = self.three_members()
        result = c.build(self.repo, self.base, members, chain_id="three")
        self.assertEqual(result.status, c.CLEAN)
        self.assertEqual([r.status for r in result.commits], [c.CLEAN] * 3)
        self.assertEqual([r.empty for r in result.commits], [False] * 3)
        self.assertEqual(result.ref, "refs/tauceti-queue/three/tip")
        self.assertEqual(self.git("rev-parse", result.ref), result.tip)
        # The checked-out branch and working tree are untouched.
        self.assertEqual(self.git("rev-parse", "main"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.repo / "A.lean").exists())
        # Single-parent chain on the base, one commit per member, in order.
        commits = c.chain_commits(self.repo, self.base, result.tip)
        self.assertEqual([x.sha for x in commits], result.shas)
        self.assertEqual(commits[0].parents, (self.base,))
        self.assertEqual([x.source_pr for x in commits], [m.source_pr for m in members])
        self.assertEqual([x.accepted_head for x in commits], [m.head_sha for m in members])
        self.assertEqual(
            self.git("ls-tree", "--name-only", result.tip).split(),
            ["A.lean", "B.lean", "C.lean", "README.md", "Shared.lean"],
        )
        # Message: title, description, then the trailers; author from the head commit.
        self.assertEqual(
            commits[1].message,
            f"Change 102 (#102)\n\nBody of PR 102.\n\n{c.SOURCE_PR_TRAILER}: {PROJECT}#102\n{c.ACCEPTED_HEAD_TRAILER}: {members[1].head_sha}\n",
        )
        self.assertEqual(commits[1].author, "Bob <bob@example.invalid>")
        self.assertEqual(commits[0].author, "Alice <alice@example.invalid>")
        self.assertEqual(
            self.git("log", "-1", "--format=%cn <%ce>", result.tip),
            f"{c.DEFAULT_COMMITTER[0]} <{c.DEFAULT_COMMITTER[1]}>",
        )
        self.assertTrue(c.validate(self.repo, self.base, result.tip, members).ok)

    def test_member_record_attribution_and_coauthors(self):
        member = self.pr(
            201,
            {"A.lean": "a\n"},
            author="Alice <alice@example.invalid>",
            extra=[
                ({"A.lean": "a2\n"}, "Carol <carol@example.invalid>"),
                ({"A.lean": "a3\n"}, "Alice <alice@example.invalid>"),
            ],
        )
        # Co-authors derived from the PR's other authors when the record does not list them.
        derived = c.build(self.repo, self.base, [member], chain_id="derived")
        message = c.read_commit(self.repo, derived.tip).message
        self.assertIn(f"{c.COAUTHOR_TRAILER}: Carol <carol@example.invalid>\n", message)
        # A record with an explicit author (a bare login) and explicit co-authors wins.
        member.author = "octocat"
        member.coauthors = ("Dana <dana@example.invalid>",)
        explicit = c.build(self.repo, self.base, [member], chain_id="explicit")
        commit = c.read_commit(self.repo, explicit.tip)
        self.assertEqual(commit.author, "octocat <octocat@users.noreply.github.com>")
        self.assertIn(f"{c.COAUTHOR_TRAILER}: Dana <dana@example.invalid>\n", commit.message)
        self.assertNotIn("Carol", commit.message)
        # An empty tuple means none, even though the PR has several authors.
        member.coauthors = ()
        none = c.build(self.repo, self.base, [member], chain_id="none")
        self.assertNotIn(c.COAUTHOR_TRAILER, c.read_commit(self.repo, none.tip).message)

    def test_member_already_in_base_or_earlier_member_becomes_an_empty_commit(self):
        landed = self.pr(301, {"A.lean": "a\n"})
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--ff-only", landed.head_sha)  # 301 has landed on main
        advanced = self.git("rev-parse", "main")
        first = self.pr(302, {"B.lean": "b\n"}, start=advanced)
        twin = self.pr(303, {"B.lean": "b\n"}, start=advanced)  # the same change as 302
        fresh = self.pr(304, {"C.lean": "c\n"}, start=advanced)
        result = c.build(self.repo, advanced, [landed, first, twin, fresh], chain_id="empties")
        self.assertEqual(result.status, c.CLEAN)
        self.assertEqual(len(result.commits), 4, "no member is dropped")
        self.assertEqual([r.empty for r in result.commits], [True, False, True, False])
        commits = c.chain_commits(self.repo, advanced, result.tip)
        self.assertEqual(len(commits), 4)
        self.assertEqual(commits[0].tree, c.tree_of(self.repo, advanced))
        self.assertEqual(commits[2].tree, commits[1].tree)
        self.assertEqual(commits[0].source_pr, f"{PROJECT}#301")
        self.assertTrue(c.validate(self.repo, advanced, result.tip, [landed, first, twin, fresh]).ok)

    def test_member_landed_on_the_base_outside_the_queue_is_not_squashed(self):
        landed = self.pr(821, {"Shared.lean": "line one\nline two by 821\nline three\n"})
        other = self.pr(822, {"B.lean": "b\n"})
        # Main takes 821 as a squash commit and an admin change to the next line of the same file.
        self.write({"Shared.lean": "line one\nline two by 821\nline three by admin\n"})
        landing = self.commit("Change 821 (#821)")
        self.assertFalse(c.merge_trees(self.repo, landing, landed.head_sha).clean, "a squash of 821 would conflict")
        result = c.build(self.repo, landing, [landed, other], chain_id="built")
        self.assertEqual(result.status, c.CLEAN)
        self.assertEqual([r.empty for r in result.commits], [True, False])
        self.assertEqual([r.landed_on_base for r in result.commits], [landing, None])
        self.assertEqual(result.landed_on_base, [{"source_pr": f"{PROJECT}#821", "commit": landing}])
        commits = c.chain_commits(self.repo, landing, result.tip)
        self.assertEqual(commits[0].tree, c.tree_of(self.repo, landing))
        self.assertEqual(commits[0].message.splitlines()[0], "Change 821 (#821)")
        self.assertEqual(commits[0].source_pr, landed.source_pr)
        self.assertEqual(commits[0].accepted_head, landed.head_sha)
        self.assertTrue(commits[0].message.rstrip().endswith(c.LANDED_NOTE.format(sha=landing)))
        self.assertEqual(commits[0].author, "Alice <alice@example.invalid>")
        self.assertTrue(c.validate(self.repo, landing, result.tip, [landed, other]).ok)
        # An explicit `since` that excludes the landing commit squashes as before (and conflicts here).
        self.assertEqual(
            c.build(self.repo, landing, [landed, other], chain_id="plain", since=landing).status, c.CONFLICT
        )
        self.assertIn("landed on the base as", c._summary(result))

    def test_conflict_reports_member_and_paths_and_keeps_the_clean_prefix(self):
        one = self.pr(401, {"A.lean": "a\n"})
        left = self.pr(402, {"Shared.lean": "line one\nLEFT\nline three\n"})
        right = self.pr(403, {"Shared.lean": "line one\nRIGHT\nline three\n", "README.md": "readme\nright\n"})
        after = self.pr(404, {"D.lean": "d\n"})
        result = c.build(self.repo, self.base, [one, left, right, after], chain_id="conflict")
        self.assertEqual(result.status, c.CONFLICT)
        self.assertEqual([r.status for r in result.commits], [c.CLEAN, c.CLEAN, c.CONFLICT, c.PENDING])
        self.assertEqual(result.conflict.source_pr, f"{PROJECT}#403")
        self.assertEqual(result.conflict.conflict_paths, ("Shared.lean",))
        self.assertIsNone(result.commits[3].sha)
        # The chain up to the last clean member is retained and valid for those members.
        self.assertEqual(result.tip, result.commits[1].sha)
        self.assertEqual(self.git("rev-parse", result.ref), result.tip)
        self.assertTrue(c.validate(self.repo, self.base, result.tip, [one, left]).ok)
        # No partial state: no worktree, no merge in progress, branch and working tree unchanged.
        self.assertEqual(self.git("worktree", "list").count("\n"), 0)
        self.assertEqual(self.git("rev-parse", "main"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(result.to_dict()["conflict"]["conflict_paths"], ["Shared.lean"])

    def test_usage_errors_are_raised_not_reported(self):
        member = self.pr(501, {"A.lean": "a\n"})
        with self.assertRaises(c.ChainError):
            c.build(self.repo, self.base, [member, member])
        with self.assertRaises(c.ChainError):
            c.build(self.repo, self.base, [c.Member(PROJECT, 502, "0" * 40)])
        with self.assertRaises(c.ChainError):
            c.build(self.repo, "0" * 40, [member])
        with self.assertRaises(c.ChainError):
            c.build(self.repo, self.base, [member], chain_id="../escape")
        empty = c.build(self.repo, self.base, [], chain_id="empty")
        self.assertEqual((empty.status, empty.tip, empty.commits), (c.CLEAN, self.base, []))


class Replay(Fixture):
    def test_replay_onto_an_advanced_base_preserves_empties_messages_and_authors(self):
        landed = self.pr(601, {"A.lean": "a\n"})
        self.git("merge", "-q", "--ff-only", landed.head_sha)
        start = self.git("rev-parse", "main")
        members = [
            landed,
            self.pr(602, {"B.lean": "b\n"}, start=start, author="Bob <bob@example.invalid>"),
            self.pr(603, {"C.lean": "c\n"}, start=start),
        ]
        prior = c.build(self.repo, start, members, chain_id="prior")
        self.assertEqual([r.empty for r in prior.commits], [True, False, False])
        before = c.chain_commits(self.repo, start, prior.tip)
        # main advances with an unrelated change, and 603's change lands independently.
        self.write({"Other.lean": "other\n", "C.lean": "c\n"})
        advanced = self.commit("advance main")
        replayed = c.replay(self.repo, start, prior.tip, advanced, chain_id="replayed")
        self.assertEqual(replayed.status, c.CLEAN)
        self.assertEqual(replayed.base, advanced)
        self.assertEqual([r.status for r in replayed.commits], [c.CLEAN] * 3)
        self.assertEqual([r.empty for r in replayed.commits], [True, False, True], "601 stays empty; 603 became empty")
        after = c.chain_commits(self.repo, advanced, replayed.tip)
        self.assertEqual(len(after), 3)
        self.assertEqual(after[0].parents, (advanced,))
        self.assertEqual([x.message for x in after], [x.message for x in before])
        self.assertEqual([(x.author, x.author_date) for x in after], [(x.author, x.author_date) for x in before])
        self.assertEqual([x.source_pr for x in after], [m.source_pr for m in members])
        self.assertNotEqual([x.sha for x in after], [x.sha for x in before], "a mechanical replay changes hashes")
        self.assertTrue(c.validate(self.repo, advanced, replayed.tip, members).ok)
        self.assertEqual(
            self.git("ls-tree", "--name-only", replayed.tip).split(),
            ["A.lean", "B.lean", "C.lean", "Other.lean", "README.md", "Shared.lean"],
        )
        # A mechanical replay is not an edit.
        report = c.compare(self.repo, start, prior.tip, advanced, replayed.tip)
        self.assertTrue(report.ok)
        self.assertEqual([m.provenance for m in report.members], [c.CLEAN] * 3)

    def test_replay_conflict_is_reported_like_build(self):
        members = [
            self.pr(701, {"A.lean": "a\n"}),
            self.pr(702, {"Shared.lean": "line one\nTWO\nline three\n"}),
            self.pr(703, {"C.lean": "c\n"}),
        ]
        prior = c.build(self.repo, self.base, members, chain_id="prior")
        self.write({"Shared.lean": "line one\nMAIN\nline three\n"})
        advanced = self.commit("main edits Shared")
        replayed = c.replay(self.repo, self.base, prior.tip, advanced, chain_id="replayed")
        self.assertEqual(replayed.status, c.CONFLICT)
        self.assertEqual([r.status for r in replayed.commits], [c.CLEAN, c.CONFLICT, c.PENDING])
        self.assertEqual(replayed.conflict.source_pr, f"{PROJECT}#702")
        self.assertEqual(replayed.conflict.conflict_paths, ("Shared.lean",))
        self.assertEqual(replayed.tip, replayed.commits[0].sha)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaises(c.ChainError):
            c.replay(self.repo, prior.tip, self.base, advanced)  # base is not an ancestor of tip

    def test_member_landed_outside_the_queue_replays_as_an_empty_commit_with_a_note(self):
        members = [
            self.pr(801, {"Shared.lean": "line one\nline two by 801\nline three\n"}),
            self.pr(802, {"B.lean": "b\n"}),
            self.pr(803, {"C.lean": "c\n"}),
        ]
        prior = c.build(self.repo, self.base, members, chain_id="prior")
        before = c.chain_commits(self.repo, self.base, prior.tip)
        # Bors squash-lands 801 (subject suffix, no trailer), an admin edits the next line of the same
        # file, and 803 lands as a queue-style commit known only by its Source-PR trailer.
        self.write({"Shared.lean": "line one\nline two by 801\nline three\n"})
        bors = self.commit("Change 801 (#801)")
        self.write({"Shared.lean": "line one\nline two by 801\nline three by admin\n"})
        self.commit("admin edits line three")
        self.write({"C.lean": "c\n"})
        trailer = self.commit(f"Another subject\n\nSource-PR: {PROJECT}#803\n")
        advanced = self.git("rev-parse", "HEAD")
        self.assertFalse(
            c.merge_trees(self.repo, advanced, before[0].sha, merge_base=self.base).clean,
            "the plain cherry-pick of 801 conflicts with the admin's edit beside it",
        )
        replayed = c.replay(self.repo, self.base, prior.tip, advanced, chain_id="replayed")
        self.assertEqual(replayed.status, c.CLEAN)
        self.assertEqual([r.status for r in replayed.commits], [c.CLEAN] * 3)
        self.assertEqual([r.empty for r in replayed.commits], [True, False, True])
        self.assertEqual([r.landed_on_base for r in replayed.commits], [bors, None, trailer])
        self.assertEqual(
            replayed.landed_on_base,
            [{"source_pr": f"{PROJECT}#801", "commit": bors}, {"source_pr": f"{PROJECT}#803", "commit": trailer}],
        )
        self.assertEqual(replayed.to_dict()["landed_on_base"], replayed.landed_on_base)
        after = c.chain_commits(self.repo, advanced, replayed.tip)
        note = c.LANDED_NOTE.format(sha=bors)
        self.assertEqual(after[0].message, before[0].message.rstrip("\n") + "\n\n" + note + "\n")
        self.assertEqual(after[1].message, before[1].message)
        self.assertEqual(
            after[2].message, before[2].message.rstrip("\n") + "\n\n" + c.LANDED_NOTE.format(sha=trailer) + "\n"
        )
        self.assertEqual([x.source_pr for x in after], [m.source_pr for m in members])
        self.assertEqual([x.accepted_head for x in after], [m.head_sha for m in members])
        self.assertEqual(after[0].message.splitlines()[0], before[0].message.splitlines()[0])
        self.assertEqual([(x.author, x.author_date) for x in after], [(x.author, x.author_date) for x in before])
        self.assertEqual(after[0].tree, c.tree_of(self.repo, advanced))
        self.assertTrue(c.validate(self.repo, advanced, replayed.tip, members).ok)
        self.assertTrue(c.message_preserved(before[0].message, after[0].message))
        self.assertEqual(
            self.git("show", f"{replayed.tip}:Shared.lean"), "line one\nline two by 801\nline three by admin"
        )
        # A later replay keeps the note once and the commit empty.
        self.write({"Other.lean": "other\n"})
        further = self.commit("advance again")
        again = c.replay(self.repo, advanced, replayed.tip, further, chain_id="again")
        self.assertEqual(again.status, c.CLEAN)
        self.assertEqual([r.empty for r in again.commits], [True, False, True])
        final = c.chain_commits(self.repo, further, again.tip)
        self.assertEqual(final[0].message.count("Landed on main as"), 1)
        self.assertEqual(final[0].message, after[0].message)
        self.assertEqual(c.landed_message(after[0].message, "0" * 40), after[0].message)

    def test_replay_conflict_reports_landed_members_still_pending(self):
        members = [
            self.pr(811, {"A.lean": "a\n"}),
            self.pr(812, {"Shared.lean": "line one\nTWO\nline three\n"}),
            self.pr(813, {"C.lean": "c\n"}),
        ]
        prior = c.build(self.repo, self.base, members, chain_id="prior")
        self.write({"Shared.lean": "line one\nMAIN\nline three\n", "C.lean": "c\n"})
        landing = self.commit("Change 813 (#813)")
        replayed = c.replay(self.repo, self.base, prior.tip, landing, chain_id="replayed")
        self.assertEqual(replayed.status, c.CONFLICT)
        self.assertEqual([r.status for r in replayed.commits], [c.CLEAN, c.CONFLICT, c.PENDING])
        self.assertEqual([r.landed_on_base for r in replayed.commits], [None, None, landing])
        self.assertEqual(replayed.landed_on_base, [{"source_pr": f"{PROJECT}#813", "commit": landing}])
        self.assertEqual(replayed.to_dict()["conflict"]["source_pr"], f"{PROJECT}#812")
        self.assertEqual(c.landed_prs(self.repo, self.base, landing), {"#813": landing})
        self.assertEqual(c.landed_commit({"#813": landing}, f"{PROJECT}#813"), landing)
        self.assertIsNone(c.landed_commit({"#813": landing}, f"{PROJECT}#812"))


class Validate(Fixture):
    def setUp(self):
        super().setUp()
        self.members = self.three_members()
        self.chain = c.build(self.repo, self.base, self.members, chain_id="chain")
        self.commits = c.chain_commits(self.repo, self.base, self.chain.tip)

    def codes(self, report):
        return sorted({v.code for v in report.violations})

    def test_accepts_the_chain_and_a_membership_equivalent_reordering(self):
        report = c.validate(self.repo, self.base, self.chain.tip, self.members)
        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.expected_members, 3)
        self.assertEqual([r.source_pr for r in report.commits], [m.source_pr for m in self.members])
        reordered = c.build(
            self.repo, self.base, [self.members[2], self.members[0], self.members[1]], chain_id="reordered"
        )
        self.assertTrue(c.validate(self.repo, self.base, reordered.tip, self.members).ok)
        # Members may be given as `owner/repo#N` strings.
        self.assertTrue(c.validate(self.repo, self.base, reordered.tip, [m.source_pr for m in self.members]).ok)
        # An empty chain over no members is fine; over members it is not.
        self.assertTrue(c.validate(self.repo, self.base, self.base, []).ok)
        self.assertEqual(
            self.codes(c.validate(self.repo, self.base, self.base, self.members)), ["count-mismatch", "missing-member"]
        )

    def test_rejects_a_merge_commit(self):
        other = self.pr(199, {"Z.lean": "z\n"})
        merge = self.raw_commit(self.commits[2].tree, self.commits[1].sha, self.commits[2].message, other.head_sha)
        report = c.validate(self.repo, self.base, merge, self.members)
        self.assertFalse(report.ok)
        self.assertEqual(self.codes(report), ["merge-commit"])
        self.assertEqual(report.violations[0].commit, merge)

    def test_rejects_a_wrong_base(self):
        stray = self.pr(198, {"Z.lean": "z\n"})
        report = c.validate(self.repo, stray.head_sha, self.chain.tip, self.members)
        self.assertEqual(self.codes(report), ["wrong-base"])
        # A base that is an ancestor but not the first commit's parent: the chain then starts with a
        # foreign commit, which is also not a member.
        self.write({"Pre.lean": "pre\n"})
        earlier = self.commit("earlier")
        shifted = c.replay(self.repo, self.base, self.chain.tip, earlier, chain_id="shifted")
        report = c.validate(self.repo, self.base, shifted.tip, self.members)
        self.assertIn("count-mismatch", self.codes(report))
        self.assertIn("missing-source-pr", self.codes(report))
        self.assertEqual(
            c.validate(self.repo, "0" * 40, shifted.tip, self.members).violations[0].code, "missing-object"
        )

    def test_rejects_a_dropped_member(self):
        report = c.validate(self.repo, self.base, self.commits[1].sha, self.members)
        self.assertEqual(self.codes(report), ["count-mismatch", "missing-member"])
        self.assertEqual([v.source_pr for v in report.violations if v.code == "missing-member"], [f"{PROJECT}#103"])
        # The reverse mismatch: a commit for a PR that is not a recorded member.
        report = c.validate(self.repo, self.base, self.chain.tip, self.members[:2])
        self.assertEqual(self.codes(report), ["count-mismatch", "unknown-member"])

    def test_rejects_a_duplicated_member(self):
        again = self.raw_commit(self.commits[2].tree, self.chain.tip, self.commits[0].message)
        report = c.validate(self.repo, self.base, again, self.members)
        self.assertEqual(self.codes(report), ["count-mismatch", "duplicate-member"])
        self.assertEqual(report.violations[0].source_pr, f"{PROJECT}#101")

    def test_rejects_a_missing_trailer_and_a_wrong_accepted_head(self):
        stripped = self.commits[2].message.replace(f"{c.ACCEPTED_HEAD_TRAILER}: {self.members[2].head_sha}\n", "")
        tip = self.raw_commit(self.commits[2].tree, self.commits[1].sha, stripped)
        self.assertEqual(self.codes(c.validate(self.repo, self.base, tip, self.members)), ["missing-accepted-head"])
        no_source = self.commits[2].message.replace(f"{c.SOURCE_PR_TRAILER}: {PROJECT}#103\n", "")
        tip = self.raw_commit(self.commits[2].tree, self.commits[1].sha, no_source)
        self.assertEqual(
            self.codes(c.validate(self.repo, self.base, tip, self.members)), ["missing-member", "missing-source-pr"]
        )
        wrong = self.commits[2].message.replace(self.members[2].head_sha, "f" * 40)
        tip = self.raw_commit(self.commits[2].tree, self.commits[1].sha, wrong)
        self.assertEqual(self.codes(c.validate(self.repo, self.base, tip, self.members)), ["accepted-head-mismatch"])
        # Trailers followed by an appended explanation are still found.
        appended = self.commits[2].message + "\nResolved against #102 by keeping both definitions.\n"
        tip = self.raw_commit(self.commits[2].tree, self.commits[1].sha, appended)
        self.assertTrue(c.validate(self.repo, self.base, tip, self.members).ok)


class Compare(Fixture):
    def setUp(self):
        super().setUp()
        self.members = self.three_members()
        self.prior = c.build(self.repo, self.base, self.members, chain_id="prior")
        self.commits = c.chain_commits(self.repo, self.base, self.prior.tip)

    def replacement(self, edit_message):
        """An agent's replacement chain: 102's content edited and its message appended to; 103 replayed."""
        one, two, three = self.commits
        new_two_tree = self.edit_tree(two.tree, "B.lean", "b (fixed)\n")
        new_two = self.raw_commit(new_two_tree, one.sha, edit_message(two.message))
        replayed = c.merge_trees(self.repo, new_two, three.sha, merge_base=two.sha)
        self.assertTrue(replayed.clean)
        new_three = c.commit_tree(
            self.repo, replayed.tree, new_two, three.message, (three.author_name, three.author_email)
        )
        return new_three

    def test_clean_and_edited_provenance_with_explanations(self):
        explanation = "Renamed the definition to avoid the clash with #101."
        tip = self.replacement(lambda message: message + "\n" + explanation + "\n")
        report = c.compare(self.repo, self.base, self.prior.tip, self.base, tip)
        self.assertTrue(report.ok, report.violations)
        by_pr = {m.source_pr: m for m in report.members}
        self.assertEqual(by_pr[f"{PROJECT}#101"].provenance, c.CLEAN)
        self.assertEqual(by_pr[f"{PROJECT}#102"].provenance, c.EDITED)
        self.assertEqual(
            by_pr[f"{PROJECT}#103"].provenance, c.CLEAN, "a descendant replayed after an edit is not edited"
        )
        edited = by_pr[f"{PROJECT}#102"]
        self.assertTrue(edited.content_changed)
        self.assertTrue(edited.message_preserved)
        self.assertEqual(edited.explanation, explanation)
        self.assertEqual(
            (edited.prior_sha, edited.new_sha), (self.commits[1].sha, c.chain_commits(self.repo, self.base, tip)[1].sha)
        )
        self.assertEqual([m.source_pr for m in report.edited], [f"{PROJECT}#102"])
        self.assertEqual(by_pr[f"{PROJECT}#101"].explanation, "")
        self.assertEqual(by_pr[f"{PROJECT}#101"].prior_sha, by_pr[f"{PROJECT}#101"].new_sha)
        # The same chain in a different member order pairs by PR identity, not position.
        reordered = c.build(
            self.repo, self.base, [self.members[2], self.members[1], self.members[0]], chain_id="reordered"
        )
        report = c.compare(self.repo, self.base, self.prior.tip, self.base, reordered.tip)
        self.assertTrue(report.ok, report.violations)
        self.assertEqual([m.provenance for m in report.members], [c.CLEAN] * 3)

    def test_message_only_append_is_edited_and_rewrite_is_a_violation(self):
        one, two, three = self.commits
        # Content unchanged but an explanation appended: edited, with the explanation.
        annotated = self.raw_commit(two.tree, one.sha, two.message + "\nKept as accepted.\n")
        replayed = c.merge_trees(self.repo, annotated, three.sha, merge_base=two.sha)
        tip = c.commit_tree(self.repo, replayed.tree, annotated, three.message, (three.author_name, three.author_email))
        report = c.compare(self.repo, self.base, self.prior.tip, self.base, tip)
        self.assertTrue(report.ok)
        edited = report.edited
        self.assertEqual(
            [(m.source_pr, m.content_changed, m.explanation) for m in edited],
            [(f"{PROJECT}#102", False, "Kept as accepted.")],
        )
        # A rewrite that does not keep the prior message as a prefix is reported, not raised.
        tip = self.replacement(lambda message: message.replace("Body of PR 102.", "Rewritten body."))
        report = c.compare(self.repo, self.base, self.prior.tip, self.base, tip)
        self.assertFalse(report.ok)
        self.assertEqual(
            [(v.code, v.source_pr) for v in report.violations], [("message-not-preserved", f"{PROJECT}#102")]
        )
        member = next(m for m in report.members if m.source_pr == f"{PROJECT}#102")
        self.assertEqual((member.provenance, member.message_preserved, member.explanation), (c.EDITED, False, ""))

    def test_missing_and_added_members_are_violations(self):
        intruder = self.pr(999, {"Z.lean": "z\n"})
        other = c.build(self.repo, self.base, [self.members[0], intruder, self.members[2]], chain_id="other")
        report = c.compare(self.repo, self.base, self.prior.tip, self.base, other.tip)
        self.assertFalse(report.ok)
        self.assertEqual(
            sorted((v.code, v.source_pr) for v in report.violations),
            [("member-added", f"{PROJECT}#999"), ("member-missing", f"{PROJECT}#102")],
        )
        by_pr = {m.source_pr: m.provenance for m in report.members}
        self.assertEqual(
            by_pr,
            {
                f"{PROJECT}#101": c.CLEAN,
                f"{PROJECT}#102": c.MISSING,
                f"{PROJECT}#103": c.CLEAN,
                f"{PROJECT}#999": c.ADDED,
            },
        )


class Records(Fixture):
    def test_members_from_a_candidate_snapshot(self):
        snapshot = {
            "base_sha": self.base,
            "members": [
                {"pr": 11, "head_sha": "a" * 40, "approved_by": "kim"},
                {"pr": 12, "head_sha": "b" * 40, "title": "Twelve", "author": "bob", "body": "Twelve's body"},
            ],
        }
        candidate = c.members_from_candidate(snapshot, PROJECT)
        self.assertEqual(candidate.base_sha, self.base)
        first, second = candidate.members
        self.assertEqual(
            (first.source_pr, first.head_sha, first.title, first.author), (f"{PROJECT}#11", "a" * 40, "", "")
        )
        self.assertEqual(first.evidence, {"approved_by": "kim"})
        self.assertIsNone(first.coauthors)
        self.assertEqual((second.title, second.author, second.description), ("Twelve", "bob", "Twelve's body"))
        # A member record with no title still gets a subject and both identity trailers.
        message = c.squash_message(first, [])
        self.assertTrue(message.startswith(f"{PROJECT}#11 at {'a' * 12} (#11)\n\n"))
        # The subject carries Bors's `(#N)` suffix exactly once.
        self.assertTrue(c.squash_message(second, []).startswith("Twelve (#12)\n\n"))
        suffixed = c.Member(PROJECT, 12, "b" * 40, title="Twelve  (#12)")
        self.assertTrue(c.squash_message(suffixed, []).startswith("Twelve (#12)\n\n"))
        self.assertEqual(c.trailer_values(message, c.SOURCE_PR_TRAILER), [f"{PROJECT}#11"])
        self.assertEqual(c.trailer_values(message, c.ACCEPTED_HEAD_TRAILER), ["a" * 40])
        # Round trip through the dict form and the generic loader.
        self.assertEqual(c.Member.from_dict(second.to_dict()), second)
        loaded = c.load_members([m.to_dict() for m in candidate.members])
        self.assertEqual(loaded.members, candidate.members)
        self.assertEqual(c.load_members(snapshot, PROJECT).members, candidate.members)
        with self.assertRaises(c.ChainError):
            c.load_members(snapshot)  # snapshot members name no repository
        with self.assertRaises(c.ChainError):
            c.Member.from_dict({"pr": 1}, PROJECT)

    def test_trailer_parsing_is_tolerant(self):
        message = "Subject\n\nNote: not a trailer we care about\nhttp://example.invalid: no\n\nSource-PR: a/b#1\nAccepted-Head: abc\n\nAppended: later\nSource-PR: a/b#1\n"
        self.assertEqual(c.unique_trailer(message, "source-pr"), "a/b#1")
        self.assertEqual(c.unique_trailer(message, c.ACCEPTED_HEAD_TRAILER), "abc")
        self.assertIsNone(c.unique_trailer("Subject\n\nSource-PR: a/b#1\nSource-PR: a/b#2\n", c.SOURCE_PR_TRAILER))
        self.assertIsNone(c.unique_trailer("Subject\n\nSource-PR:\n", c.SOURCE_PR_TRAILER))
        self.assertTrue(c.message_preserved("m\n", "m\n\nmore\n"))
        self.assertFalse(c.message_preserved("m\n", "n\n\nm\n"))
        self.assertEqual(c.appended_text("m\n", "m\n\nmore\n"), "more")
        self.assertEqual(c.format_identity("Ann <ann@x>"), ("Ann", "ann@x"))
        self.assertEqual(c.format_identity("@ann"), ("ann", "ann@users.noreply.github.com"))


class CLI(Fixture):
    def run_cli(self, *args, expect=0):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        result = subprocess.run(
            [sys.executable, "-m", "tauceti_queue.chain", *args, "--repo", str(self.repo), "--json"],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp.name,
        )
        self.assertEqual(result.returncode, expect, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_json_round_trip_through_build_validate_replay_and_compare(self):
        members = self.three_members()
        members_file = Path(self.tmp.name) / "members.json"
        members_file.write_text(
            json.dumps(
                {
                    "base_sha": self.base,
                    "members": [{"pr": m.number, "head_sha": m.head_sha, "title": m.title} for m in members],
                }
            )
        )
        built = self.run_cli("build", "--members", str(members_file), "--repository", PROJECT, "--chain-id", "cli")
        self.assertEqual(built["status"], c.CLEAN)
        self.assertEqual(built["base"], self.base)
        self.assertEqual(built["ref"], "refs/tauceti-queue/cli/tip")
        self.assertEqual([x["source_pr"] for x in built["commits"]], [m.source_pr for m in members])
        self.assertIsNone(built["conflict"])
        validated = self.run_cli(
            "validate",
            "--base",
            built["base"],
            "--tip",
            built["tip"],
            "--members",
            str(members_file),
            "--repository",
            PROJECT,
        )
        self.assertTrue(validated["ok"])
        self.assertEqual(validated["commit_count"], 3)
        self.write({"Other.lean": "other\n"})
        advanced = self.commit("advance")
        replayed = self.run_cli(
            "replay", "--base", built["base"], "--tip", built["tip"], "--onto", advanced, "--chain-id", "cli-replayed"
        )
        self.assertEqual(replayed["status"], c.CLEAN)
        self.assertEqual(replayed["base"], advanced)
        compared = self.run_cli(
            "compare",
            "--prior-base",
            built["base"],
            "--prior-tip",
            built["tip"],
            "--base",
            advanced,
            "--tip",
            replayed["tip"],
        )
        self.assertTrue(compared["ok"])
        self.assertEqual([m["provenance"] for m in compared["members"]], [c.CLEAN] * 3)
        # Member records in this module's own dict form also load, and a dropped member exits 1.
        members_file.write_text(json.dumps([m.to_dict() for m in members[:2]]))
        invalid = self.run_cli(
            "validate", "--base", built["base"], "--tip", built["tip"], "--members", str(members_file), expect=1
        )
        self.assertEqual(sorted(v["code"] for v in invalid["violations"]), ["count-mismatch", "unknown-member"])
        # A conflict exits 1 with the conflicting member in the JSON.
        clash = [self.pr(801, {"Shared.lean": "x\n"}), self.pr(802, {"Shared.lean": "y\n"})]
        members_file.write_text(json.dumps([m.to_dict() for m in clash]))
        conflicted = self.run_cli("build", "--base", self.base, "--members", str(members_file), expect=1)
        self.assertEqual(conflicted["status"], c.CONFLICT)
        self.assertEqual(conflicted["conflict"]["source_pr"], f"{PROJECT}#802")
        self.assertEqual(conflicted["conflict"]["conflict_paths"], ["Shared.lean"])
        # Usage errors exit 2.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tauceti_queue.chain",
                "replay",
                "--repo",
                str(self.repo),
                "--base",
                "0" * 40,
                "--tip",
                built["tip"],
                "--onto",
                advanced,
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(REPO)),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()

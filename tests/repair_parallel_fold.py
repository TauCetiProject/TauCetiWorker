"""Fold real Git group commits without losing empty members or original messages."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import chain, repair, repair_parallel


class ParallelFold(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parallel-fold-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "commit.cleanup", "strip")
        self.git("config", "core.commentChar", "#")
        self.base = self.commit({"base": "base\n"}, "base")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def commit(self, files, message):
        for name, content in files.items():
            path = self.repo / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        self.git("add", "-A")
        tree = self.git("write-tree")
        parent = self.git("rev-parse", "HEAD") if (self.repo / ".git/refs/heads/master").exists() else None
        if parent is None:
            # The fixture's initial commit has no parent.
            result = subprocess.run(
                ["git", "-C", str(self.repo), "commit-tree", tree, "-F", "-"],
                input=message,
                capture_output=True,
                text=True,
                check=True,
            )
            sha = result.stdout.strip()
        else:
            sha = chain.commit_tree(
                self.repo, tree, parent, message, ("Original author", "original@example.test"), "1700000000 +0230"
            )
        self.git("update-ref", "refs/heads/master", sha)
        self.git("checkout", "-q", "-f", "master")
        return sha

    def member(self, number, files, subject=None):
        subject = subject or f"member {number} (#{number})"
        message = (
            f"{subject}\n\n# original heading\n; preserve this too\n\n"
            f"Source-PR: o/r#{number}\nAccepted-Head: {number:040x}\n"
        )
        return self.commit(files, message)

    def group(self, number, members, files, fixups):
        return repair_parallel.Group(f"group-{number}", members, files, files, {}, status="repaired", fixups=fixups)

    def test_two_empty_members_keep_authorship_messages_explanations_and_chain_validation(self):
        first = self.member(1, {"one": "duplicate one\n"})
        second = self.member(2, {"two": "duplicate two\n"})
        tip = self.member(3, {"three": "keep\n"})
        originals = chain.chain_commits(self.repo, self.base, tip)
        groups = []
        for number, owner, filename in ((1, first, "one"), (2, second, "two")):
            subject = chain.read_commit(self.repo, owner).message.partition("\n")[0]
            fixup = self.commit({filename: None}, "fixup! " + subject + "\n")
            explanation = f"Repair: duplicate {number} already exists on main.\n\n# explanation heading\n"
            squash = self.commit({}, "squash! " + subject + "\n\n" + explanation)
            groups.append(self.group(number, [number], [filename], [fixup, squash]))
        result = repair_parallel.fold_fixups(self.repo, self.base, tip, groups)
        self.assertEqual(result["applied"], ["group-1", "group-2"])
        self.assertEqual(result["cherry_picked"], 4)
        folded = chain.chain_commits(self.repo, self.base, result["tip"])
        self.assertEqual(len(folded), 3)
        for number, (before, after) in enumerate(zip(originals, folded, strict=True), 1):
            self.assertEqual(
                (after.author_name, after.author_email, after.author_date),
                (before.author_name, before.author_email, before.author_date),
            )
            if number < 3:
                expected = (
                    before.message + f"\nRepair: duplicate {number} already exists on main.\n\n# explanation heading\n"
                )
                self.assertEqual(after.message, expected)
                self.assertEqual(after.tree, chain.tree_of(self.repo, after.parents[0]))
            else:
                self.assertEqual(after.message, before.message)
        self.assertEqual(self.git("ls-tree", "--name-only", "HEAD").splitlines(), ["base", "three"])
        report = repair.chain_report(self.repo, self.base, tip, result["tip"], [f"o/r#{n}" for n in (1, 2, 3)])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unexplained"], [])
        self.assertEqual([group.status for group in groups], ["folded", "folded"])

    def test_content_bearing_squash_and_sha_targets_apply_their_deltas(self):
        owner = self.member(1, {"one": "original\n"})
        tip = self.member(2, {"two": "unaffected\n"})
        fixup = self.commit({"one": "fixed\n"}, "fixup! " + owner[:12] + "\n")
        squash = self.commit({"one": "fixed\nwith squash content\n"}, "squash! " + owner + "\n\nRepair: both deltas.\n")
        reference = chain.tree_of(self.repo, squash)
        group = self.group(1, [1], ["one"], [fixup, squash])
        result = repair_parallel.fold_fixups(self.repo, self.base, tip, [group])
        self.assertEqual(group.status, "folded", result)
        self.assertEqual(chain.tree_of(self.repo, result["tip"]), reference)
        self.assertEqual(self.git("show", "HEAD:one"), "fixed\nwith squash content")
        self.assertIn("Repair: both deltas.", chain.chain_commits(self.repo, self.base, result["tip"])[0].message)

    def test_originally_empty_member_and_multiple_fixups_per_owner_are_preserved(self):
        empty = self.member(1, {})
        owner = self.member(2, {"two": "original\n"})
        original_message = chain.read_commit(self.repo, owner).message
        fixups = []
        for revision in ("first", "second"):
            fixups.append(self.commit({"two": revision + "\n"}, "fixup! " + owner[:12] + "\n"))
            fixups.append(self.commit({}, "squash! " + owner[:12] + f"\n\nRepair: {revision} adjustment.\n"))
        reference = chain.tree_of(self.repo, fixups[-1])
        group = self.group(1, [2], ["two"], fixups)
        result = repair_parallel.fold_fixups(self.repo, self.base, owner, [group])
        self.assertEqual(group.status, "folded", result)
        folded = chain.chain_commits(self.repo, self.base, result["tip"])
        self.assertEqual(len(folded), 2)
        self.assertEqual(folded[0].message, chain.read_commit(self.repo, empty).message)
        self.assertEqual(folded[0].tree, chain.tree_of(self.repo, self.base))
        self.assertEqual(
            folded[1].message,
            original_message + "\nRepair: first adjustment.\n\nRepair: second adjustment.\n",
        )
        self.assertEqual(chain.tree_of(self.repo, result["tip"]), reference)

    def test_targets_must_uniquely_name_original_permitted_members(self):
        owner = self.member(1, {"one": "one\n"}, subject="same subject")
        other = self.member(2, {"two": "two\n"}, subject="same subject")
        for target in ("same subject", "missing member", other[:12], "fixup! " + owner[:12]):
            with self.subTest(target=target):
                self.git("checkout", "-q", "-f", "master")
                self.git("reset", "--hard", "-q", other)
                fixup = self.commit({"one": "changed\n"}, "fixup! " + target + "\n")
                group = self.group(1, [1], ["one"], [fixup])
                result = repair_parallel.fold_fixups(self.repo, self.base, other, [group])
                self.assertEqual(group.status, "fixups_not_applied", result)
                self.assertEqual(self.git("rev-parse", "HEAD"), other)
                self.assertEqual(result["applied"], [])

    def test_squash_changes_outside_the_group_are_rejected(self):
        owner = self.member(1, {"one": "one\n", "outside": "outside\n"})
        squash = self.commit({"outside": "changed\n"}, "squash! " + owner[:12] + "\n\nRepair: forbidden.\n")
        group = self.group(1, [1], ["one"], [squash])
        result = repair_parallel.fold_fixups(self.repo, self.base, owner, [group])
        self.assertEqual(group.status, "fixups_not_applied", result)
        self.assertIn("outside the group", group.detail)
        self.assertEqual(self.git("rev-parse", "HEAD"), owner)

    def test_real_fold_conflict_returns_to_original_tip(self):
        owner = self.member(1, {"shared": "original\n"})
        tip = self.member(2, {"shared": "later member\n"})
        fixup = self.commit({"shared": "fixed later member\n"}, "fixup! " + owner[:12] + "\n")
        group = self.group(1, [1], ["shared"], [fixup])
        result = repair_parallel.fold_fixups(self.repo, self.base, tip, [group])
        self.assertEqual(group.status, "fixups_not_applied", result)
        self.assertEqual(result["cherry_picked"], 1)
        self.assertIn("conflict", result["fold_error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertFalse((self.repo / ".git/rebase-merge").exists())

    def test_clean_reordering_must_match_the_cherry_picked_reference_tree(self):
        owner = self.member(1, {"one": "same content\n"})
        tip = self.member(2, {"one": None})
        # Reintroducing the owner's file is clean both at the tip and inside the owner, but
        # the later member would delete it again. A clean fold alone cannot prove equivalence.
        fixup = self.commit({"one": "same content\n"}, "fixup! " + owner[:12] + "\n")
        group = self.group(1, [1], ["one"], [fixup])
        result = repair_parallel.fold_fixups(self.repo, self.base, tip, [group])
        self.assertEqual(group.status, "fixups_not_applied", result)
        self.assertIn("differs from the cherry-picked reference tree", result["fold_error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)


if __name__ == "__main__":
    unittest.main()

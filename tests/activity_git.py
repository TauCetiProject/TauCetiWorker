#!/usr/bin/env python3
"""Real local Git comparisons, cold/warm object caches and ordinary retries."""

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import activity as a
from tauceti_worker import activity_git as g


class GitLOC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Tests")
        self.git("config", "user.email", "tests@example.invalid")
        for name, text in {"Edit.lean": "one\n", "Delete.lean": "one\ntwo\n", "README.md": "one\n"}.items():
            (self.repo / name).write_text(text)
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "Edit.lean").write_text("one\ntwo\n")
        (self.repo / "Delete.lean").unlink()
        (self.repo / "New.lean").write_text("new_one\nnew_two\nnew_three\n")
        (self.repo / "README.md").write_text("one\ntwo\nthree\n")
        self.commit()
        self.first = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/7/head", self.first)
        self.git("checkout", "-q", "-b", "second", self.base)
        (self.repo / "Other.lean").write_text("line\n" * 7)
        self.commit()
        self.second = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/8/head", self.second)
        self.prs = [
            dict(number=7, head_sha=self.first, base_sha=self.base, diff_totals=dict(files=4, added=6, deleted=2)),
            dict(number=8, head_sha=self.second, base_sha=self.base, diff_totals=dict(files=1, added=7, deleted=0)),
        ]
        self.original = g._git
        self.fetches = []

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")

    def transport(self, directory, *args, input=None):
        if args[0] == "fetch":
            self.fetches.append(args)
            args = tuple(str(self.repo) if a == "https://github.com/org/repo.git" else a for a in args)
        return self.original(directory, *args, input=input)

    def measure(self, prs=None):
        with patch.object(g, "_git", side_effect=self.transport):
            return g.measure(self.root / "activity", "org/repo", self.prs if prs is None else prs)

    def test_cold_batch_and_warm_cache_match_exact_diffs(self):
        result = self.measure()
        self.assertEqual(result[7], dict(added=4, deleted=2, head_sha=self.first))
        self.assertEqual(result[8], dict(added=7, deleted=0, head_sha=self.second))
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.measure(), result)
        self.assertEqual(len(self.fetches), 1, "warm immutable objects need no network")

    def test_new_head_and_deletion_only_pr_are_measured(self):
        self.measure()
        self.git("checkout", "-q", "-b", "third", self.base)
        (self.repo / "Delete.lean").unlink()
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/7/head", head)
        p = dict(number=7, head_sha=head, base_sha=self.base, diff_totals=dict(files=1, added=0, deleted=2))
        self.assertEqual(self.measure([p])[7], dict(added=0, deleted=2, head_sha=head))
        self.assertEqual(len(self.fetches), 2)

    def test_incremental_fetch_negotiates_existing_history(self):
        self.measure()
        self.git("checkout", "-q", "second")
        (self.repo / "Other.lean").write_text("line\n" * 8)
        self.commit()
        head = self.git("rev-parse", "HEAD")
        trace = self.root / "packets.log"
        with patch.dict(os.environ, {"GIT_TRACE_PACKET": str(trace)}):
            result = self.measure([dict(number=8, head_sha=head, base_sha=self.base)])
        self.assertEqual(result[8]["added"], 8)
        self.assertIn("have " + self.second, trace.read_text())

    def test_base_on_another_branch_is_fetched_exactly(self):
        self.git("checkout", "-q", "-b", "target", self.base)
        (self.repo / "Target.lean").write_text("target\n")
        self.commit()
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "second")
        result = self.measure([{**self.prs[0], "base_sha": base}])
        self.assertEqual(result[7], dict(added=4, deleted=2, head_sha=self.first))

    def test_failed_fetch_keeps_warm_comparisons_available(self):
        self.measure([self.prs[0]])

        def unavailable(directory, *args, input=None):
            if args[0] == "fetch":
                raise subprocess.TimeoutExpired("git", 120)
            return self.original(directory, *args, input=input)

        with patch.object(g, "_git", side_effect=unavailable):
            result = g.measure(self.root / "activity", "org/repo", self.prs)
        self.assertEqual(result[7]["added"], 4)
        self.assertNotIn(8, result)

    def test_rename_paths_and_binary_files(self):
        totals, lean = g._counts(b"0\t0\t\x00Old.lean\x00New.md\x003\t0\t\x00Old.md\x00New.lean\x00-\t-\timage.png\x00")
        self.assertEqual(totals, dict(files=3, added=3, deleted=0))
        self.assertEqual(lean, dict(added=3, deleted=0))
        with self.assertRaises(ValueError):
            g._counts(b"-\t-\tBinary.lean\x00")

    def test_inconsistent_metadata_and_invalid_comparisons_remain_unknown(self):
        self.assertEqual(self.measure([{**self.prs[0], "diff_totals": dict(added=999)}]), {})
        with patch.object(g, "_git", side_effect=AssertionError("invalid comparison ran Git")):
            self.assertEqual(g.measure(self.root, "org/repo", [{**self.prs[0], "base_sha": "main"}]), {})
            self.assertEqual(g.measure(self.root, "https://elsewhere.invalid/repo", self.prs), {})

    def test_failed_fetch_retries_normally_without_manual_state_repair(self):
        def unavailable(directory, *args, input=None):
            if args[0] == "fetch":
                raise subprocess.TimeoutExpired("git", 120)
            return self.original(directory, *args, input=input)

        with patch.object(g, "_git", side_effect=unavailable):
            self.assertEqual(g.measure(self.root / "activity", "org/repo", self.prs), {})
        self.assertEqual(len(self.measure()), 2)

    def test_interrupted_ref_update_recovers_without_operator_cleanup(self):
        hook = None
        original_run = g.processes.run

        def interrupted(directory, *args, input=None):
            nonlocal hook
            if args[0] != "fetch":
                return self.original(directory, *args, input=input)
            hook = directory / "hooks" / "reference-transaction"
            hook.write_text("#!/bin/sh\nsleep 60\n")
            hook.chmod(0o700)

            def short_run(argv, **kwargs):
                return original_run(argv, **{**kwargs, "timeout": 0.5})

            with patch.object(g.processes, "run", side_effect=short_run):
                return self.transport(directory, *args, input=input)

        with patch.object(g, "_git", side_effect=interrupted):
            # Objects received before interruption can already be counted.
            self.assertEqual(len(g.measure(self.root / "activity", "org/repo", self.prs)), 2)
        self.assertIsNotNone(hook)
        hook.unlink()
        self.assertEqual(len(self.measure()), 2)

    def test_normal_refresh_stores_real_git_counts_and_retries_transport_failure(self):
        env = dict(
            TAUCETI_ACTIVITY_DIR=str(self.root / "activity"),
            TAUCETI_GITHUB_STATE_URL="https://state.example",
            TAUCETI_GITHUB_STATE_TOKEN_FILE="/not-read-by-mock",
        )
        response = dict(prs=[{**self.prs[0], "state": "OPEN", "observed_at": 1000000, "lean_loc": None}], missing=[])
        with (
            patch.dict(os.environ, env),
            contextlib.closing(a._connect()) as db,
            patch.object(a, "observe", return_value=response),
        ):
            db.execute("INSERT INTO prs(repo,number,worker,created) VALUES ('org/repo',7,'worker',0)")
            with patch.object(g, "_git", side_effect=OSError("offline")):
                a._refresh_prs(db, None, 1000, [])
            self.assertIsNone(db.execute("SELECT added FROM pr_loc").fetchone())
            with patch.object(g, "_git", side_effect=self.transport):
                a._refresh_prs(db, None, 1301, [])
            self.assertEqual(tuple(db.execute("SELECT head,added,deleted FROM pr_loc").fetchone()), (self.first, 4, 2))


if __name__ == "__main__":
    unittest.main()

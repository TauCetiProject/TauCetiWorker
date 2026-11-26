#!/usr/bin/env python3
"""A GitHub PR whose refs/pull/<n>/head is missing is checked out from its head branch instead."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import work_units as work
from tauceti_worker.survey import PRInfo

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(directory, *args):
    import os

    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True, env={**os.environ, **ENV}
    ).stdout.strip()


def pr(number, head):
    return PRInfo(
        number,
        head,
        "roadmap/work",
        "fork-owner",
        "TauCeti",
        False,
        "CONFLICTING",
        "science",
        True,
        False,
        False,
    )


class CheckoutWithoutPullRef(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fork = root / "fork"
        self.fork.mkdir()
        git(self.fork, "init", "-q", "-b", "main")
        (self.fork / "A.lean").write_text("theorem a : True := trivial\n")
        git(self.fork, "add", "-A")
        git(self.fork, "commit", "-qm", "base")
        git(self.fork, "checkout", "-q", "-b", "roadmap/work")
        (self.fork / "B.lean").write_text("theorem b : True := trivial\n")
        git(self.fork, "add", "-A")
        git(self.fork, "commit", "-qm", "work")
        self.head = git(self.fork, "rev-parse", "HEAD")
        self.checkout = root / "checkout"
        git(root, "clone", "-q", "-b", "main", str(self.fork), str(self.checkout))
        url = patch.object(work, "_github_clone_url", return_value=str(self.fork))
        url.start()
        self.addCleanup(url.stop)
        env = patch.dict("os.environ", ENV)
        env.start()
        self.addCleanup(env.stop)

    def test_checks_out_the_selected_head_tracking_the_head_branch(self):
        result = work._checkout_head_branch(self.checkout, pr(7, self.head), self.head, "pr-7-worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "pr-7-worker")
        self.assertEqual(git(self.checkout, "config", "branch.pr-7-worker.remote"), str(self.fork))
        self.assertEqual(git(self.checkout, "config", "branch.pr-7-worker.merge"), "refs/heads/roadmap/work")

    def test_a_moved_head_branch_is_not_checked_out(self):
        result = work._checkout_head_branch(self.checkout, pr(7, "f" * 40), "f" * 40, "pr-7-worker")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_only_a_missing_pull_ref_on_github_takes_the_fallback(self):
        missing = subprocess.CompletedProcess([], 1, "", "fatal: couldn't find remote ref refs/pull/7/head\n")
        self.assertTrue(work._pull_ref_missing(missing, 7))
        self.assertFalse(work._pull_ref_missing(missing, 8))
        other = subprocess.CompletedProcess([], 1, "", "fatal: could not read Username\n")
        self.assertFalse(work._pull_ref_missing(other, 7))
        with patch("tauceti_worker.worker_sources.current", return_value=object()):
            self.assertFalse(work._pull_ref_missing(missing, 7), "Forgejo checkouts never use pull refs")


if __name__ == "__main__":
    unittest.main()

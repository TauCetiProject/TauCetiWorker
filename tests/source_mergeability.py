#!/usr/bin/env python3
"""Exact native snapshots, unavailable proof and concurrent head/base changes."""

import copy
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import source_mergeability as probe

HEAD, BASE, OTHER = "a" * 40, "b" * 40, "c" * 40


class Mergeability(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, TAUCETI_BASE_BRANCH="main")
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.row = {
            "state": "open",
            "draft": False,
            "mergeable": False,
            "head": {"sha": HEAD},
            "base": {"sha": BASE, "ref": "main"},
        }
        self.source = SimpleNamespace(
            validate_binding=Mock(),
            live_pull=Mock(side_effect=lambda _: copy.deepcopy(self.row)),
            identity=object(),
            api=SimpleNamespace(_token="fixture-token"),
            repo="owner/project",
            _mergeability_cache={},
        )
        self.refs = {"refs/mergeability/head": HEAD, "refs/mergeability/base": BASE}
        self.git = Mock()
        self.git.local_ref_sha.side_effect = lambda ref: self.refs[ref]
        self.git.mergeability.return_value = "CONFLICTING"
        factory = patch.object(probe, "ForgejoGitSource", return_value=self.git)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def resolve(self):
        return probe.resolve(self.source, self.state, 7, HEAD)

    def test_exact_proof_uses_retained_pull_refs_and_caches_the_pair(self):
        self.assertEqual(self.resolve(), "CONFLICTING")
        self.git.fetch_refs.assert_called_once_with(
            "owner/project", ["+refs/pull/7/head:refs/mergeability/head", "+refs/heads/main:refs/mergeability/base"]
        )
        self.assertEqual(self.resolve(), "CONFLICTING")
        self.git.fetch_refs.assert_called_once()
        self.row["base"]["sha"] = OTHER
        self.refs["refs/mergeability/base"] = OTHER
        self.git.mergeability.return_value = "MERGEABLE"
        self.assertEqual(self.resolve(), "MERGEABLE")
        self.assertEqual(self.git.fetch_refs.call_count, 2)

    def test_ineligible_or_moved_head_never_fetches(self):
        for change in (
            {"state": "closed"},
            {"draft": True},
            {"head": {"sha": OTHER}},
            {"base": {"sha": BASE, "ref": "another-base"}},
        ):
            with self.subTest(change=change):
                row = copy.deepcopy(self.row)
                row.update(change)
                self.source.live_pull.side_effect = None
                self.source.live_pull.return_value = row
                self.assertEqual(self.resolve(), "UNKNOWN")
        self.factory.assert_not_called()

    def test_both_head_and_base_races_after_git_leave_no_authority(self):
        for side in ("head", "base"):
            with self.subTest(side=side):
                changed = copy.deepcopy(self.row)
                changed[side]["sha"] = OTHER
                self.source.live_pull.side_effect = [copy.deepcopy(self.row), changed]
                self.assertEqual(self.resolve(), "UNKNOWN")
                self.assertEqual(self.source._mergeability_cache, {})

    def test_fetched_ref_mismatch_never_runs_merge_tree(self):
        self.refs["refs/mergeability/head"] = OTHER
        self.assertEqual(self.resolve(), "UNKNOWN")
        self.git.mergeability.assert_not_called()
        self.assertEqual(self.source._mergeability_cache, {})

    def test_unavailable_credentials_or_git_leave_no_cached_conflict(self):
        self.factory.side_effect = ValueError("credential unavailable")
        self.assertEqual(self.resolve(), "UNKNOWN")
        self.assertEqual(self.source._mergeability_cache, {})
        self.factory.side_effect = None
        self.git.fetch_refs.side_effect = OSError("transport unavailable")
        self.assertEqual(self.resolve(), "UNKNOWN")
        self.assertEqual(self.source._mergeability_cache, {})

    def test_busy_probe_store_does_not_block_a_survey(self):
        root = self.state / "mergeability"
        root.mkdir(mode=0o755)
        with (root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.resolve(), "UNKNOWN")
        self.factory.assert_not_called()
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_inherited_git_directory_does_not_redirect_store_initialization(self):
        escape = self.state / "unexpected.git"
        with patch.dict(os.environ, GIT_DIR=str(escape)):
            self.assertEqual(self.resolve(), "CONFLICTING")
        self.assertFalse(escape.exists())
        self.assertTrue((self.state / "mergeability/objects.git/HEAD").is_file())


if __name__ == "__main__":
    unittest.main()

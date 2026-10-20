#!/usr/bin/env python3
"""Private Git/fake-tool storage pressure, cache expiry and automatic continuation fixtures."""

import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verification import Receipts, git, write

from tauceti_worker import verification as v
from tauceti_worker import verification_storage as storage
from tauceti_worker.retention import locked
from tauceti_worker.verification_jobs import resume_pending


class Storage(Receipts):
    def setUp(self):
        super().setUp()
        os.environ.update(
            TAUCETI_VERIFY_CANDIDATE_BYTES=str(128 * 1024**2), TAUCETI_VERIFY_STORAGE_BYTES=str(256 * 1024**2)
        )

    def pending(self):
        result = self.verify(stage="build", deadline=time.time() - 1)
        self.assertEqual(result["result_type"], "pending", result)
        path = Path(result["continuation_job"])
        job = v.read_json(path)
        self.assertTrue(job["isolated"])
        return path, job, Path(job["root"])

    def collect(self, **kwargs):
        return storage.housekeep((self.state,), seconds=10, entries=100000, now=time.time() + 90000, **kwargs)

    def test_expiry_reclaims_builds_preserves_exact_source_and_all_git_state(self):
        write(self.root, ".lake/build/large.olean", "rebuildable")
        dep = self.root / ".lake/packages/mathlib"
        dep.mkdir(parents=True)
        git(dep, "init", "-q")
        git(dep, "config", "user.name", "Fixture")
        git(dep, "config", "user.email", "fixture@example.org")
        write(dep, ".gitignore", ".lake/\n")
        write(dep, "Dependency.lean", "dependency source\n")
        git(dep, "add", ".")
        git(dep, "commit", "-qm", "dependency")
        write(dep, "Dependency.lean", "unpublished dependency edits\n")
        write(dep, ".lake/build/Dependency.olean", "dependency build output")
        write(self.root, "TauCeti/A.lean", "staged edit\n")
        git(self.root, "add", "TauCeti/A.lean")
        write(self.root, "TauCeti/A.lean", "unstaged edit\n")
        write(self.root, "Untracked.lean", "unique untracked source\n")
        git(self.root, "tag", "keep-tag")
        _, job, tree = self.pending()
        before = v.inventory(tree, self.base)
        index = git(tree, "diff", "--cached", "--binary")
        head = git(tree, "rev-parse", "HEAD")
        report = self.collect()
        self.assertFalse(report["errors"], report)
        self.assertFalse((tree / ".lake/build").exists())
        self.assertFalse((tree / ".lake/packages/mathlib/.lake/build").exists())
        self.assertEqual(
            (tree / ".lake/packages/mathlib/Dependency.lean").read_text(), "unpublished dependency edits\n"
        )
        self.assertEqual(v.inventory(tree, self.base), before)
        self.assertEqual(git(tree, "diff", "--cached", "--binary"), index)
        self.assertEqual(git(tree, "rev-parse", "keep-tag"), head)
        self.assertEqual((tree / "Untracked.lean").read_text(), "unique untracked source\n")
        self.assertEqual(
            v.read_json(self.state / "verification-pending" / f"{job['id']}.json")["source_digest"],
            job["source_digest"],
        )

    def test_active_job_and_receipt_leases_prevent_removal(self):
        write(self.root, ".lake/build/keep.olean", "active")
        _, job, tree = self.pending()
        for lease in (self.state / "verification-jobs" / f"{job['id']}.lock", Path(job["lease"])):
            with locked(lease):
                self.collect()
                self.assertTrue((tree / ".lake/build/keep.olean").exists())
        self.collect()
        self.assertFalse((tree / ".lake/build").exists())

    def test_completed_candidate_uses_short_grace_and_keeps_receipt(self):
        pending, job, tree = self.pending()
        v.atomic(self.state / "verification-jobs" / pending.name, {**job, "status": "success"})
        pending.unlink()
        cache = write(tree, ".lake/build/cache", "generated")
        receipt = Path(job["receipt"])
        v.atomic(receipt, {**v.read_json(receipt), "fingerprint": "a" * 64})
        index = self.state / "verification-index" / ("a" * 64 + ".json")
        v.atomic(index, dict(build=str(receipt), lint=str(receipt)))
        storage.housekeep((self.state,), seconds=10, now=time.time() + 1800)
        self.assertTrue(cache.exists())
        storage.housekeep((self.state,), seconds=10, entries=100000, now=time.time() + 3700)
        self.assertFalse(cache.exists())
        self.assertTrue(receipt.exists())
        self.assertNotIn("build", v.read_json(index))
        self.assertIn("lint", v.read_json(index))

    def test_disk_pressure_reclaims_before_ttl_and_recovery_makes_useful_progress(self):
        write(self.root, ".lake/build/cache", "generated")
        pending, job, tree = self.pending()
        os.environ["TAUCETI_VERIFY_STORAGE_BYTES"] = str(128 * 1024**2)
        self.assertRaises(storage.Pressure, storage.reserve, self.state, "b" * 32)
        # A full reservation triggers early eviction; no TTL or new model conversation needed.
        report = storage.housekeep((self.state,), seconds=10, entries=100000)
        self.assertFalse(report["errors"], report)
        self.assertFalse((tree / ".lake/build").exists())
        job["next_at"] = 0
        v.atomic(pending, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        complete = v.read_json(self.state / "verification-jobs" / pending.name)
        self.assertEqual(complete["status"], "success", complete)
        self.assertTrue((tree / ".lake/calls").is_file(), "actual next build completed in the retained candidate")

    def test_reservation_contention_never_overcommits_or_waits_for_heavy_tools(self):
        self.state.mkdir()

        def allocate(index):
            try:
                storage.reserve(self.state, f"{index:032x}")
                return True
            except storage.Pressure:
                return False

        with ThreadPoolExecutor(max_workers=20) as pool:
            admitted = list(pool.map(allocate, range(20)))
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertGreater(sum(admitted), 0)
        self.assertLessEqual(sum(row["reserved"] for row in rows.values()), 256 * 1024**2)

    def test_budget_defer_keeps_job_and_does_not_consume_an_attempt(self):
        pending, job, _ = self.pending()
        os.environ["TAUCETI_VERIFY_STORAGE_BYTES"] = "1"
        job["next_at"] = 0
        v.atomic(pending, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(resume_pending(SimpleNamespace(state=self.state)))
        retained = v.read_json(pending)
        self.assertEqual(retained["attempts"], 0)
        self.assertEqual(retained["status"], "pending")
        self.assertIn("budget", retained["error"])

    def test_large_file_removal_is_resumable_and_source_remains(self):
        write(self.root, ".lake/build/large", "x" * 200)
        _, job, tree = self.pending()
        for _ in range(20):
            self.collect(removal_bytes=40)
            if not (tree / ".lake/build").exists():
                break
        self.assertFalse((tree / ".lake/build").exists())
        self.assertTrue((tree / "TauCeti/A.lean").exists())
        self.assertTrue(
            v.read_json(storage.root() / "candidates.json")["candidates"][storage.key(self.state, job["id"])]["compact"]
        )

    def test_symlinked_packages_tracked_builds_and_live_cwd_are_protected(self):
        write(self.root, "build/Tracked.lean", "tracked source")
        git(self.root, "add", "build/Tracked.lean")
        _, _, tree = self.pending()
        outside = self.home / "external"
        write(outside, "dep/.lake/build/keep", "external")
        (tree / ".lake").mkdir(exist_ok=True)
        (tree / ".lake/packages").symlink_to(outside, target_is_directory=True)
        cache = write(tree, ".lake/build/keep", "active")
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tree)
        try:
            self.collect()
            self.assertTrue(cache.exists())
        finally:
            process.terminate()
            process.wait()
        self.collect()
        self.assertTrue((outside / "dep/.lake/build/keep").exists())
        self.assertEqual((tree / "build/Tracked.lean").read_text(), "tracked source")

    def test_mount_or_missing_mount_evidence_withholds_only_affected_candidate(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/keep", "mounted")
        for mounts in (None, [tree / ".lake/build"]):
            with patch("tauceti_worker.retention.mount_points", return_value=mounts):
                self.collect()
                self.assertTrue(cache.exists())
        self.collect()
        self.assertFalse(cache.exists())

    def test_monitor_reports_pressure_without_claiming_success(self):
        _, job, tree = self.pending()
        os.environ["TAUCETI_VERIFY_CANDIDATE_BYTES"] = "10"
        monitor = storage.Monitor(self.state, job["id"])
        self.addCleanup(monitor.close)
        self.assertRaises(storage.Pressure, monitor.check)
        self.assertTrue((tree / "TauCeti/A.lean").exists())

    def test_legacy_candidates_are_counted_across_sibling_workers(self):
        for name, identifier in (("first", "a" * 32), ("second", "b" * 32)):
            write(self.home / name, f"verification-candidates/{identifier}/unique.lean", "source")
        self.state.mkdir(exist_ok=True)
        self.assertRaises(storage.Pressure, storage.reserve, self.state, "c" * 32)
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(
            all(
                (Path(row["state"]) / "verification-candidates" / row["id"] / "unique.lean").exists()
                for row in rows.values()
            )
        )

    def test_corrupt_ledger_refuses_new_allocation_without_deleting_anything(self):
        _, _, tree = self.pending()
        path = storage.root() / "candidates.json"
        path.write_text("interrupted/corrupt")
        self.assertRaises(storage.Pressure, storage.reserve, self.state, "a" * 32)
        self.collect()
        self.assertTrue((tree / "TauCeti/A.lean").exists())

    def test_oversized_warm_snapshot_falls_back_to_exact_source_without_builds(self):
        cache = self.root / ".lake/build/oversized"
        cache.parent.mkdir(parents=True)
        with cache.open("wb") as handle:
            handle.truncate(129 * 1024**2)
        _, _, tree = self.pending()
        self.assertFalse((tree / ".lake/build").exists())
        self.assertEqual(v.inventory(tree, self.base), v.inventory(self.root, self.base))
        self.assertTrue(cache.exists(), "fallback never edits the original build cache")

    def test_oversized_owned_tool_is_interrupted_then_resumes_after_cleanup(self):
        pending, job, tree = self.pending()
        job["next_at"] = 0
        v.atomic(pending, job)
        os.environ.update(
            TAUCETI_VERIFY_CANDIDATE_BYTES="65536", FIXTURE_MODE="storage-growth", FIXTURE_GROW_BYTES="1048576"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        retained = v.read_json(pending)
        receipt = v.read_json(Path(retained["receipt"]))
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["incomplete_reason"], "disk-pressure")
        self.assertEqual(receipt["result_type"], "pending")
        self.assertLess(receipt["commands"][-1]["exit"], 0)
        self.assertTrue((tree / "TauCeti/A.lean").exists())
        os.environ["TAUCETI_VERIFY_CANDIDATE_BYTES"] = str(128 * 1024**2)
        os.environ.pop("FIXTURE_MODE")
        self.collect()
        retained["next_at"] = 0
        v.atomic(pending, retained)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertEqual(v.read_json(self.state / "verification-jobs" / pending.name)["status"], "success")

    def test_source_checkpoints_share_host_budget_and_never_expire(self):
        os.environ["TAUCETI_VERIFY_CHECKPOINT_BYTES"] = str(128 * 1024**2)
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            first = self.verify(stage="build")
            self.assertEqual(first["status"], "success")
            checkpoint = Path(first["source_checkpoint"])
            self.assertTrue(checkpoint.is_file())
            write(self.root, "TauCeti/A.lean", "next exact candidate\n")
            blocked = self.verify(stage="build")
        self.assertEqual(blocked["status"], "incomplete", blocked)
        self.assertEqual(blocked["incomplete_reason"], "disk-pressure")
        self.collect()
        self.assertTrue(checkpoint.exists())


def load_tests(loader, suite, pattern):
    return unittest.TestSuite(
        loader.loadTestsFromName("Storage." + name, sys.modules[__name__])
        for name in Storage.__dict__
        if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()

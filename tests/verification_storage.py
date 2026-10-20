#!/usr/bin/env python3
"""Private Git/fake-tool storage pressure, cache expiry and automatic continuation fixtures."""

import contextlib
import io
import os
import shutil
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
        pending, job, tree = self.pending()
        v.atomic(self.state / "verification-jobs" / pending.name, {**job, "status": "success"})
        pending.unlink()
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
            v.read_json(self.state / "verification-jobs" / f"{job['id']}.json")["source_digest"],
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
        write(self.root, ".lake/build/cache", "x" * (2 * 1024**2))
        pending, job, tree = self.pending()
        os.environ["TAUCETI_VERIFY_STORAGE_BYTES"] = str(130 * 1024**2)
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
        job["next_at"] = 0
        v.atomic(pending, job)
        with (
            patch.object(storage, "reserve", side_effect=storage.Pressure("candidate byte budget exhausted")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
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
        shutil.rmtree(tree / ".lake/packages")
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

    def test_corrupt_ledger_rebuilds_mechanically_without_deleting_source(self):
        _, _, tree = self.pending()
        path = storage.root() / "candidates.json"
        path.write_text("interrupted/corrupt")
        try:
            storage.reserve(self.state, "a" * 32)
        except storage.Pressure:
            self.collect()
            storage.reserve(self.state, "a" * 32)
        self.assertEqual(len(list(storage.root().glob("candidates.corrupt-*.json"))), 1)
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
            TAUCETI_VERIFY_CANDIDATE_BYTES="262144", FIXTURE_MODE="storage-growth", FIXTURE_GROW_BYTES="1048576"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        retained = v.read_json(pending)
        receipt = v.read_json(Path(retained["receipt"]))
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["incomplete_reason"], "storage-limit")
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
            second = self.verify(stage="build")
            self.assertEqual(second["status"], "success", second)
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        checkpoints = [row for row in rows.values() if row.get("kind") == "checkpoint"]
        self.assertEqual(len(checkpoints), 2)
        self.assertTrue(all(row["reserved"] < 100000 for row in checkpoints))
        self.collect()
        self.assertTrue(checkpoint.exists())

    def test_default_budget_progresses_on_large_inventory_without_one_large_sweep(self):
        _, _, tree = self.pending()
        cache = tree / ".lake/build"
        cache.mkdir(parents=True)
        for index in range(8000):
            (cache / f"file-{index}.olean").write_text("generated")
        passes = 0
        while cache.exists() and passes < 100:
            report = storage.housekeep((self.state,), now=time.time() + 90000)
            self.assertLessEqual(report["work"]["metadata_entries"], 2048)
            self.assertLessEqual(report["work"]["removed_bytes"], 256 * 1024**2)
            passes += 1
        self.assertFalse(cache.exists(), f"no useful default-budget cleanup after {passes} passes: {report}")
        self.assertGreater(passes, 1)
        self.assertTrue((tree / "TauCeti/A.lean").exists())

    def test_owner_death_releases_reclamation_without_operator_action(self):
        _, job, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "rebuildable")
        lease = self.state / "verification-jobs" / f"{job['id']}.lock"
        lease.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)",
                str(lease),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.collect()
            self.assertTrue(cache.exists())
        finally:
            process.kill()
            process.wait()
            process.stdout.close()
        self.collect()
        self.assertFalse(cache.exists())

    def test_active_oldest_candidate_does_not_starve_small_or_large_neighbors(self):
        os.environ["TAUCETI_VERIFY_STORAGE_BYTES"] = str(512 * 1024**2)
        jobs = []
        for index in range(3):
            write(self.root, "TauCeti/A.lean", f"unique candidate {index}\n")
            jobs.append(self.pending())
        first, large, small = [row[2] for row in jobs]
        write(first, ".lake/build/keep", "active")
        write(large, ".lake/build/large", "x" * 1000)
        write(small, ".lake/build/small", "x")
        lease = self.state / "verification-jobs" / f"{jobs[0][1]['id']}.lock"
        with locked(lease):
            for _ in range(8):
                self.collect(removal_bytes=50)
                if not (small / ".lake/build").exists() and (large / ".lake/build/large").stat().st_size < 1000:
                    break
            self.assertFalse((small / ".lake/build").exists())
            self.assertTrue((first / ".lake/build/keep").exists())
            self.assertLess((large / ".lake/build/large").stat().st_size, 1000)

    def test_fast_writer_cannot_finish_successfully_between_monitor_ticks(self):
        _, job, tree = self.pending()
        os.environ.update(
            TAUCETI_VERIFY_CANDIDATE_BYTES="262144",
            FIXTURE_MODE="storage-growth",
            FIXTURE_GROW_BYTES="1048576",
            FIXTURE_SLEEP="0",
        )
        with contextlib.redirect_stderr(io.StringIO()), patch.object(storage.Monitor, "check"):
            receipt = v.Verification(tree, self.state, base=self.base, stage="build", job=job).run()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["incomplete_reason"], "storage-limit")
        self.assertTrue(all(command["exit"] == 0 for command in receipt["commands"]), receipt)

    def test_untracked_source_under_build_survives_cleanup(self):
        write(self.root, "build/New.lean", "untracked source must survive\n")
        _, _, tree = self.pending()
        before = v.inventory(tree, self.base)
        report = self.collect()
        self.assertFalse(report["errors"], report)
        self.assertEqual((tree / "build/New.lean").read_text(), "untracked source must survive\n")
        self.assertEqual(v.inventory(tree, self.base), before)

    def test_pending_dependencies_keep_warm_builds_when_own_cache_expires(self):
        write(self.root, ".lake/build/Own.olean", "own build")
        write(self.root, ".lake/packages/mathlib/.lake/build/Mathlib.olean", "warm dependency")
        _, _, tree = self.pending()
        self.collect()
        self.assertFalse((tree / ".lake/build").exists())
        self.assertTrue((tree / ".lake/packages/mathlib/.lake/build/Mathlib.olean").exists())

    def test_nonisolated_pressure_is_retryable_without_an_unusable_pending_job(self):
        with patch.object(storage, "reserve", side_effect=storage.Pressure("candidate byte budget exhausted")):
            refused = self.verify(stage="build", deadline=time.time() - 1)
        self.assertEqual(refused["incomplete_reason"], "disk-pressure")
        self.assertEqual(refused["result_type"], "incomplete")
        self.assertNotIn("continuation_job", refused)
        self.assertFalse(list((self.state / "verification-pending").glob("*.json")))
        self.assertEqual(self.verify(stage="build")["status"], "success")

    def test_partial_legacy_candidates_and_checkpoints_are_measured_without_deletion(self):
        source = write(self.state, "verification-candidates/" + "a" * 32 + "/unique.lean", "unpublished")
        checkpoint = write(self.state, "verification-checkpoints/" + "b" * 64 + "/source.patch", "partial source")
        for _ in range(3):
            self.collect()
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["reserved"] < 100000 for row in rows.values()), rows)
        self.assertEqual(source.read_text(), "unpublished")
        self.assertEqual(checkpoint.read_text(), "partial source")

    def test_bubble_discovery_never_enumerates_root_and_uses_host_identity(self):
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_HOST_STATE=str(self.state)),
            patch.object(storage, "discover") as discover,
        ):
            with patch.object(Path, "iterdir", side_effect=AssertionError("bubble walked outside mounted state")):
                storage.discover_family(Path("/opt/verification-state"))
            discover.assert_called_once_with(Path("/opt/verification-state"))
            self.assertEqual(storage.key(Path("/opt/verification-state"), "a" * 32), storage.key(self.state, "a" * 32))

    def test_full_disk_housekeeping_error_does_not_escape_supervisor_hook(self):
        self.pending()
        with patch.object(storage, "_scheduled", side_effect=OSError(28, "disk full")):
            result = storage.housekeep((self.state,))
        self.assertIn("disk full", result["errors"][0])
        self.assertEqual(result["next_action"], "retry-verification-storage-housekeeping")

    def test_checkpoint_failure_releases_empty_reservation_and_retries(self):
        os.environ["TAUCETI_VERIFY_CHECKPOINT_BYTES"] = "1"
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            refused = self.verify(stage="build")
            self.assertEqual(refused["incomplete_reason"], "storage-limit")
            rows = v.read_json(storage.root() / "candidates.json")["candidates"]
            self.assertFalse(any(row.get("kind") == "checkpoint" for row in rows.values()))
            os.environ["TAUCETI_VERIFY_CHECKPOINT_BYTES"] = str(128 * 1024**2)
            self.assertEqual(self.verify(stage="build")["status"], "success")

    def test_candidate_lease_stays_held_between_all_phases(self):
        _, job, tree = self.pending()
        cache = write(tree, ".lake/build/keep", "active")
        original = v.Verification.phase_commands

        def commands(verifier, phase, modules):
            if phase == "axioms":
                self.collect()
                self.assertTrue(cache.exists(), "workspace was evicted between build and audit")
            return original(verifier, phase, modules)

        with contextlib.redirect_stderr(io.StringIO()), patch.object(v.Verification, "phase_commands", commands):
            receipt = v.Verification(tree, self.state, base=self.base, job=job).run()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.collect()
        self.assertFalse(cache.exists())

    def test_twenty_contenders_with_space_all_obtain_reservations(self):
        os.environ.update(
            TAUCETI_VERIFY_CANDIDATE_BYTES=str(2 * 1024**2), TAUCETI_VERIFY_STORAGE_BYTES=str(64 * 1024**2)
        )
        states = [self.home / "workers" / str(index) for index in range(20)]
        for state in states:
            state.mkdir(parents=True)
        with ThreadPoolExecutor(max_workers=20) as pool:
            reservations = list(pool.map(lambda state: storage.reserve(state, "a" * 32), states))
        self.assertEqual(reservations, [2 * 1024**2] * 20)

    def test_checkpoints_book_actual_bytes_across_many_source_states(self):
        for index in range(30):
            write(self.root, "TauCeti/A.lean", f"unique preserved source {index}\n")
            verifier = v.Verification(self.root, self.state)
            verifier.base = self.base
            verifier.checkpoint_sources(dict(sources=v.inventory(self.root, self.base)))
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertEqual(len(rows), 30)
        self.assertLess(sum(row["reserved"] for row in rows.values()), 1024**2)

    def test_last_resort_cache_reclamation_keeps_oldest_pending_job_warm_and_progressing(self):
        dep = self.root / ".lake/packages/mathlib"
        dep.mkdir(parents=True)
        git(dep, "init", "-q")
        git(dep, "config", "user.name", "Fixture")
        git(dep, "config", "user.email", "fixture@example.org")
        write(dep, ".gitignore", ".lake/\n")
        write(dep, "Dependency.lean", "dependency source")
        git(dep, "add", ".")
        git(dep, "commit", "-qm", "dependency")
        output = dep / ".lake/build/Dependency.olean"
        output.parent.mkdir(parents=True)
        with output.open("wb") as stream:
            stream.truncate(1024**2)
        jobs = []
        for index in range(3):
            write(self.root, "TauCeti/A.lean", f"pending source {index}")
            jobs.append(self.pending())
        os.environ["TAUCETI_VERIFY_STORAGE_BYTES"] = str(130 * 1024**2)
        self.assertRaises(storage.Pressure, storage.reserve, self.state, jobs[0][1]["id"])
        for _ in range(12):
            self.collect()
            try:
                storage.reserve(self.state, jobs[0][1]["id"])
                break
            except storage.Pressure:
                pass
        self.assertFalse((jobs[0][2] / ".lake/build").exists())
        self.assertTrue((jobs[0][2] / ".lake/packages/mathlib/.lake/build/Dependency.olean").exists())
        self.assertFalse((jobs[1][2] / ".lake/packages/mathlib/.lake/build").exists())
        pending, job, _ = jobs[0]
        job["next_at"] = 0
        v.atomic(pending, job)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertEqual(v.read_json(self.state / "verification-jobs" / pending.name)["status"], "success")

    def test_written_bytes_are_not_double_counted_as_unwritten_growth(self):
        cache = self.root / ".lake/build/sparse"
        cache.parent.mkdir(parents=True)
        with cache.open("wb") as stream:
            stream.truncate(64 * 1024**2)
        _, first, _ = self.pending()
        write(self.root, "TauCeti/A.lean", "another source")
        self.pending()
        usage = SimpleNamespace(total=1024**3, used=924 * 1024**2, free=100 * 1024**2)
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_MIN_FREE_PERCENT="0"),
            patch.object(storage.shutil, "disk_usage", return_value=usage),
        ):
            self.assertEqual(storage.reserve(self.state, first["id"]), 128 * 1024**2)

    def test_far_future_housekeeping_timestamp_cannot_disable_reclamation(self):
        _, job, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "generated")
        with storage.ledger() as data:
            data["candidates"][storage.key(self.state, job["id"])]["last_used"] = 0
        v.atomic(storage.root() / "housekeeping.json", dict(next_at=10**20))
        storage.housekeep((self.state,), seconds=10, entries=100000)
        self.assertFalse(cache.exists())

    def test_slow_ownership_inspection_leaves_budget_for_real_deletion(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "generated")

        def slow_users(path):
            time.sleep(0.3)
            return []

        with patch("tauceti_worker.work_recovery.live_users", side_effect=slow_users):
            report = storage.housekeep((self.state,), now=time.time() + 90000)
        self.assertFalse(cache.exists(), report)
        self.assertGreaterEqual(report["work"]["ownership_seconds"], 0.3)
        self.assertLess(report["work"]["seconds"], 0.25)

    def test_inventory_observer_does_not_hold_a_false_live_workspace_reference(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "generated")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; from pathlib import Path; from tauceti_worker.verification_storage import _sizes; scan=_sizes(Path(sys.argv[1])); print(next(scan),flush=True); time.sleep(30)",
                str(tree),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(process.stdout.readline().strip())
            self.collect()
            self.assertFalse(cache.exists(), "read-only inventory was mistaken for an active writer")
        finally:
            process.kill()
            process.wait()
            process.stdout.close()

    def test_deterministic_limit_stays_dormant_until_limits_change(self):
        pending, job, _ = self.pending()
        job["next_at"] = 0
        v.atomic(pending, job)
        os.environ.update(
            TAUCETI_VERIFY_CANDIDATE_BYTES="262144", FIXTURE_MODE="storage-growth", FIXTURE_GROW_BYTES="1048576"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        blocked = v.read_json(pending)
        self.assertEqual(blocked["status"], "blocked-storage-limit")
        blocked["next_at"] = 0
        v.atomic(pending, blocked)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertEqual(v.read_json(pending)["attempts"], 1)
        os.environ["TAUCETI_VERIFY_CANDIDATE_BYTES"] = str(128 * 1024**2)
        os.environ.pop("FIXTURE_MODE")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertEqual(v.read_json(self.state / "verification-jobs" / pending.name)["status"], "success")

    def test_invalid_setting_defers_without_retiring_or_charging_the_job(self):
        pending, job, _ = self.pending()
        job["next_at"] = 0
        v.atomic(pending, job)
        before = pending.read_bytes()
        with patch.dict(os.environ, TAUCETI_VERIFY_CANDIDATE_BYTES="32G"), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(resume_pending(SimpleNamespace(state=self.state)))
        self.assertEqual(pending.read_bytes(), before)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(resume_pending(SimpleNamespace(state=self.state), seconds=10))
        self.assertEqual(v.read_json(self.state / "verification-jobs" / pending.name)["status"], "success")

    def test_removed_compact_artifacts_release_their_reservations(self):
        import shutil

        _, job, tree = self.pending()
        self.collect()
        shutil.rmtree(tree)
        self.collect()
        rows = v.read_json(storage.root() / "candidates.json")["candidates"]
        self.assertNotIn(storage.key(self.state, job["id"]), rows)

    def test_failed_source_only_copy_removes_partial_files_and_reservation(self):
        original = write(self.root, "TauCeti/Large.lean", "unpublished source\n" * 10000)
        before = original.read_bytes()
        os.environ.update(TAUCETI_VERIFY_CANDIDATE_BYTES="131072", TAUCETI_VERIFY_SOURCE_CANDIDATE_BYTES="65536")
        refused = self.verify(stage="build", deadline=time.time() - 1)
        self.assertEqual(refused["incomplete_reason"], "storage-limit")
        self.assertEqual(refused["result_type"], "incomplete")
        self.assertFalse(list((self.state / "verification-candidates").iterdir()))
        self.assertFalse(v.read_json(storage.root() / "candidates.json")["candidates"])
        self.assertEqual(original.read_bytes(), before)

    def test_interrupted_copy_construction_is_reclaimed_with_bounded_readonly_file_removal(self):
        pending, job, tree = self.pending()
        job.update(isolated=False, root=str(self.root))
        v.atomic(pending, job)
        v.atomic(
            self.state / "verification-copy-incomplete" / f"{job['id']}.json",
            dict(version=1, source=str(self.root), source_digest=job["source_digest"]),
        )
        large = tree / ".git/objects/pack/interrupted.pack"
        large.parent.mkdir(parents=True, exist_ok=True)
        with large.open("wb") as out:
            out.truncate(300 * 1024**2)
        large.chmod(0o444)
        for _ in range(4):
            self.collect()
            if not tree.exists():
                break
        self.assertFalse(tree.exists())
        self.assertTrue((self.root / "TauCeti/A.lean").exists())
        self.assertFalse(v.read_json(storage.root() / "candidates.json")["candidates"])

    def test_legacy_candidate_backlog_cannot_block_tiny_source_checkpoints(self):
        for index in range(4):
            write(self.state, f"verification-candidates/{index:032x}/unique.lean", "unpublished")
        verifier = v.Verification(self.root, self.state)
        verifier.base = self.base
        verifier.checkpoint_sources(dict(sources=v.inventory(self.root, self.base)))
        self.assertTrue(Path(verifier.receipt["source_checkpoint"]).exists())
        for _ in range(5):
            self.collect()
        with patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"):
            self.assertEqual(self.verify(stage="build")["status"], "success")

    def test_accounting_contention_cannot_turn_a_fixed_limit_into_automatic_retries(self):
        _, job, _ = self.pending()
        os.environ["TAUCETI_VERIFY_CANDIDATE_BYTES"] = "10"
        monitor = storage.Monitor(self.state, job["id"])
        self.addCleanup(monitor.close)
        with patch.object(storage, "overflow", side_effect=storage.Pressure("accounting busy")):
            self.assertRaises(storage.Limit, monitor.check)
            self.assertRaises(storage.Limit, monitor.finish)

    def test_invalid_budget_combinations_cannot_strand_every_continuation(self):
        for overrides in (
            dict(TAUCETI_VERIFY_STORAGE_BYTES="1"),
            dict(TAUCETI_VERIFY_CHECKPOINT_POOL_BYTES=str(256 * 1024**2)),
        ):
            with patch.dict(os.environ, overrides):
                self.assertRaises(storage.Configuration, storage.validate_settings)
        storage.validate_settings()

    def test_idle_failure_releases_growth_and_does_not_force_unrelated_cache_eviction(self):
        _, job, _ = self.pending()
        storage.reserve(self.state, job["id"])
        monitor = storage.Monitor(self.state, job["id"])
        monitor.settle_idle()
        with storage.ledger(write=False) as data:
            row = data["candidates"][storage.key(self.state, job["id"])]
            self.assertFalse(row["growing"])
            self.assertLess(row["reserved"], 1024**2)
            self.assertEqual(storage.future_growth(data["candidates"], row["device"]), 0)

    def test_live_terminal_owner_does_not_starve_other_idle_candidates(self):
        _, first, first_tree = self.pending()
        v.atomic(self.state / "verification-jobs" / f"{first['id']}.json", {**first, "status": "success"})
        write(self.root, "TauCeti/A.lean", "second source")
        _, _, second_tree = self.pending()
        cache = write(second_tree, ".lake/build/cache", "generated")
        with patch(
            "tauceti_worker.work_recovery.live_users", side_effect=lambda tree: ["live"] if tree == first_tree else []
        ):
            for _ in range(5):
                self.collect()
        self.assertFalse(cache.exists())

    def test_full_filesystem_reclaims_optional_hash_metadata_then_completes_cleanup(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "generated")
        hashes = write(self.state, "verification-hashes-v2.json", "optional derived cache")
        hashes.with_suffix(".lock").touch()
        original = storage.atomic_json

        def disk_full(path, value):
            if hashes.exists() and path.parent == self.state / "verification-storage":
                raise OSError(28, "filesystem full before deletion journal")
            return original(path, value)

        with patch.object(storage, "atomic_json", side_effect=disk_full):
            report = self.collect()
        self.assertIn("emergency_hash_cache_reclaimed", report)
        self.assertFalse(cache.exists(), report)
        self.assertTrue((tree / "TauCeti/A.lean").exists())
        self.assertTrue(list((self.state / "verification").glob("*/receipt.json")))

    def test_readonly_private_cache_directories_and_exact_byte_slice_make_progress(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/readonly/data", "abcd")
        cache.parent.chmod(0o555)
        for _ in range(4):
            report = self.collect(removal_bytes=4)
            self.assertFalse(report["errors"], report)
            if not (tree / ".lake/build").exists():
                break
        self.assertFalse((tree / ".lake/build").exists())
        self.assertTrue((tree / "TauCeti/A.lean").exists())

    def test_unsupported_sibling_alias_does_not_retire_a_healthy_workers_job(self):
        sibling = self.state.parent / "sibling"
        sibling.mkdir()
        (sibling / "verification-candidates").symlink_to(self.root, target_is_directory=True)
        pending, _, tree = self.pending()
        self.assertTrue(pending.exists())
        self.assertTrue((tree / "TauCeti/A.lean").exists())

    def test_shared_host_pressure_reclaims_caller_cache_before_its_idle_expiry(self):
        _, _, tree = self.pending()
        cache = write(tree, ".lake/build/cache", "generated")
        sibling = self.state.parent / "worker-two"
        sibling.mkdir()
        # Another worker's admitted growth leaves insufficient room for this caller's next
        # build. Its local collector must observe the shared total, not just its own rows.
        storage.reserve(sibling, "b" * 32)
        report = storage.housekeep((self.state,), seconds=10, entries=100000, now=time.time())
        self.assertFalse(report["errors"], report)
        self.assertFalse(cache.exists(), report)


def load_tests(loader, suite, pattern):
    return unittest.TestSuite(
        loader.loadTestsFromName("Storage." + name, sys.modules[__name__])
        for name in Storage.__dict__
        if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()

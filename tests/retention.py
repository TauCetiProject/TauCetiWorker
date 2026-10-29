"""Local storage-reclamation fixtures; never inspect or mutate the real worker fleet."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import activity, config, processes, retention, shared_mathlib, work_recovery
from tauceti_worker import round as round_
from tauceti_worker.runtime_status import atomic_json, read_json

REAL_WORKER_CONFIGS = retention.worker_configs


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tauceti-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state" / "retired"
        self.state.mkdir(parents=True)
        self.checkout = self.root / "checkouts" / "retired" / "TauCeti"
        self.checkout.mkdir(parents=True)
        self.cfg = config.Config(
            wid="retired",
            home=self.root / "login",
            data_home=self.state / "home",
            state=self.state,
            checkout=self.checkout,
            store_dir=self.state / "review",
            sbcache=self.state / "cache",
            logdir=self.root / "logs" / "retired",
            quota_cache=self.state / "cache",
        )
        self.clock = 1_000_000.0
        actual = shutil.disk_usage(self.root)
        balanced = shutil._ntuple_diskusage(actual.total, actual.total // 2, actual.total // 2)
        for item in (
            patch.object(retention.shutil, "disk_usage", return_value=balanced),
            patch.dict(
                os.environ,
                {
                    "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                    "TAUCETI_BUILD_CACHE_ROOT": str(self.root / "build-cache"),
                    "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "pool"),
                    "TAUCETI_WORKERS_STATE_DIR": str(self.root / "managed"),
                },
            ),
            patch.object(retention, "worker_configs", return_value=[self.cfg]),
            patch.object(retention, "worker_state_root", return_value=self.state.parent),
            patch.object(retention, "legacy_project", return_value=True),
            patch("tauceti_worker.build_caches.mathlib_pool", return_value=self.root / "archives"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.checkout / "Source.lean").write_text("original\n")
        (self.checkout / ".gitignore").write_text(".lake/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "seed")
        work_recovery.mark_published(self.checkout, "seed", self.git("rev-parse", "HEAD").strip())

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.checkout), *args], check=True, capture_output=True, text=True
        ).stdout

    def cache(self):
        target = self.checkout / ".lake" / "build" / "Main.olean"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated")
        return target

    def old(self):
        atomic_json(self.state / "cache-generation.json", dict(started_at=self.clock - retention.LIFETIME))

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file() and not p.is_symlink()
        }

    def test_three_days_and_source_recovery(self):
        self.old()
        cache = self.cache()
        (self.checkout / "Source.lean").write_text("unfinished\n")
        journal = self.state / "usage.jsonl"
        journal.write_text("authoritative\n")
        preserved = [self.state / name for name in ("budget/accounting.json", "review-outbox/pending.json")]
        for path in preserved:
            path.parent.mkdir(parents=True)
            path.write_text("must survive")
        result = retention.sweep(now=self.clock)
        self.assertFalse(cache.exists())
        self.assertFalse(self.checkout.exists())
        self.assertEqual(journal.read_text(), "authoritative\n")
        for path in preserved:
            self.assertEqual(path.read_text(), "must survive")
        records = list(work_recovery.records(self.state.parent))
        self.assertEqual(len(records), 1)
        self.assertIn(b"unfinished", (records[0][1] / "unstaged.patch").read_bytes())
        self.assertFalse(result["errors"])

    def test_pressure_request_recycles_each_old_generation_once(self):
        build = self.cache()
        private = self.cfg.state / "home/.cache/lake/payload"
        private.parent.mkdir(parents=True)
        private.write_text("old artifact")
        atomic_json(
            activity.root() / "cache-pressure.json",
            dict(requested_at=self.clock + 10, until=self.clock + 200, workers=[self.cfg.wid]),
        )
        retention.recycle(self.cfg, now=self.clock + 20)
        self.assertFalse(private.exists())
        self.assertTrue(build.exists())  # Pressure does not make the current build cold.
        private.parent.mkdir(parents=True)
        private.write_text("new artifact")
        retention.recycle(self.cfg, now=self.clock + 30)
        self.assertTrue(private.exists())

    def test_critical_space_reclaims_only_the_selected_worker_through_recovery(self):
        self.clock = time.time()
        cache = self.cache()
        self.cfg.state.mkdir(parents=True, exist_ok=True)
        atomic_json(self.cfg.state / "cache-generation.json", dict(started_at=self.clock))
        private = self.cfg.state / "home/.cache/lake/payload"
        private.parent.mkdir(parents=True)
        private.write_text("artifact")
        usage = shutil._ntuple_diskusage(1000, 960, 40)
        with (
            patch.object(retention.shutil, "disk_usage", return_value=usage),
            patch("tauceti_worker.retention.request_cleanup"),
        ):
            with self.assertRaises(config.AdmissionUnavailable):
                with round_.RoundContext(self.cfg):
                    self.fail("critical storage must defer new work")
        self.assertTrue(cache.exists())
        self.assertTrue(private.exists())
        atomic_json(
            activity.root() / "cache-pressure.json",
            dict(requested_at=self.clock + 1, until=self.clock + 200, workers=[self.cfg.wid]),
        )
        with patch.object(retention.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(1000, 900, 100)):
            retention.recycle(self.cfg, now=self.clock + 20)
        self.assertFalse(private.exists())
        self.assertFalse(cache.exists())
        # Deferral released the round lock, and does not need an activity/paid round.
        with retention.locked(self.cfg.state / "round.lock"):
            pass

    def test_short_rounds_do_not_renew_generation(self):
        cache = self.cache()
        retention.recycle(self.cfg, now=self.clock)
        retention.recycle(self.cfg, now=self.clock + retention.LIFETIME - 1)
        self.assertTrue(cache.exists())
        self.assertEqual(read_json(self.state / "cache-generation.json")["started_at"], self.clock)
        retention.recycle(self.cfg, now=self.clock + retention.LIFETIME)
        self.assertFalse(cache.exists())

    def test_legacy_generation_gets_one_grace_period(self):
        cache = self.cache()
        os.utime(cache, (0, 0))
        retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())
        retention.sweep(now=self.clock + retention.LIFETIME)
        self.assertFalse(cache.exists())

    def test_dry_run_does_not_write_any_files(self):
        self.old()
        self.cache()
        before = self.snapshot()
        report = retention.run(dry=True)
        self.assertTrue(report["removed"])
        self.assertEqual(before, self.snapshot())

    def test_round_lock_protects_replacement_workspace(self):
        self.old()
        cache = self.cache()
        with retention.locked(self.state / "round.lock"):
            report = retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())
        self.assertIn(str(self.state), report["protected"])

    def test_retention_never_signals_an_aged_active_round(self):
        self.old()
        cache = self.cache()
        atomic_json(self.state / "round-owner.json", dict(pid=os.getpid(), started_at=0))
        with (
            retention.locked(self.state / "round.lock"),
            patch.object(os, "kill", side_effect=AssertionError("retention must not terminate work")),
            patch.object(os, "killpg", side_effect=AssertionError("retention must not terminate work")),
        ):
            retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())

    def test_busy_round_does_not_block_closed_logs_or_unrelated_attempts(self):
        self.old()
        cache = self.cache()
        self.cfg.logdir.mkdir(parents=True)
        log = self.cfg.logdir / "old.log"
        log.write_text("closed diagnostic")
        attempt = self.state / "verification/old"
        attempt.mkdir(parents=True)
        os.utime(log, (0, 0))
        os.utime(attempt, (0, 0))
        with retention.locked(self.state / "round.lock"):
            retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())
        self.assertFalse(log.exists())
        self.assertFalse(attempt.exists())

    def test_worker_progress_without_namespaces_or_valid_containment_markers(self):
        self.old()
        cache = self.cache()
        (self.checkout / "Source.lean").write_text("unpublished work")
        markers = [
            self.state / "round-boundaries/stale.json",
            self.state / "quiescence.json",
            self.root / "activity/containment-unconfirmed/stale.json",
            self.state / "retention-expired.json",
        ]
        for marker in markers:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("malformed historical evidence")
        original = subprocess.Popen

        def without_namespaces(argv, *args, **kwargs):
            self.assertNotIn(Path(argv[0]).name, ("bwrap", "unshare"))
            return original(argv, *args, **kwargs)

        signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            with (
                patch.dict(os.environ, {"TAUCETI_PROCESS_CONTAINMENT": "required"}),
                patch.object(subprocess, "Popen", side_effect=without_namespaces),
                patch("tauceti_worker.resources.RoundResources.start"),
            ):
                with round_.RoundContext(self.cfg):
                    self.assertFalse(cache.exists())
                    self.assertEqual((self.checkout / "Source.lean").read_text(), "unpublished work")
                    result = processes.run(
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('next-action').write_text('useful progress')",
                        ],
                        cwd=self.checkout,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                with round_.RoundContext(self.cfg):
                    self.assertEqual((self.checkout / "next-action").read_text(), "useful progress")
        finally:
            for sig, handler in signals.items():
                signal.signal(sig, handler)
        for marker in markers:
            self.assertEqual(marker.read_text(), "malformed historical evidence")

    def test_live_supervisor_recycles_only_at_its_own_round_boundary(self):
        self.old()
        cache = self.cache()
        with retention.locked(self.state / "instance.lock"):
            retention.sweep(now=self.clock)
        self.assertTrue(self.checkout.exists())
        self.assertTrue(cache.exists())
        self.assertFalse((self.state / "round.lock").exists())
        with retention.locked(self.state / "round.lock"):
            retention.recycle(self.cfg, now=self.clock)
        self.assertFalse(cache.exists())

    def test_open_instance_lock_protects_workspace_without_proc_lock_device_matching(self):
        self.old()
        cache = self.cache()
        with (self.state / "instance.lock").open("w"):
            report = retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())
        self.assertIn(str(self.state), report["protected"])
        self.assertFalse((self.state / "round.lock").exists())

    def test_first_sweep_gives_old_round_locks_the_generation_grace_period(self):
        cache = self.cache()
        lock = self.state / "round.lock"
        lock.touch()
        os.utime(lock, (0, 0))
        retention.sweep(now=self.clock)
        self.assertTrue(self.checkout.exists())
        self.assertTrue(cache.exists())
        retention.sweep(now=self.clock + retention.LIFETIME - 1)
        self.assertTrue(self.checkout.exists())
        retention.sweep(now=self.clock + retention.LIFETIME)
        self.assertFalse(self.checkout.exists())

    def test_missing_open_file_observations_protect_diagnostics_locally(self):
        self.old()
        cache = self.cache()
        self.cfg.logdir.mkdir(parents=True)
        diagnostic = self.cfg.logdir / "old.log"
        diagnostic.write_text("possibly still open")
        os.utime(diagnostic, (0, 0))
        with patch.object(retention, "open_files", return_value=None):
            report = retention.sweep(now=self.clock)
        self.assertTrue(diagnostic.exists())
        self.assertTrue(cache.exists())
        self.assertTrue(report["protected"])

    def test_vanishing_diagnostic_does_not_skip_other_logs_or_attempts(self):
        self.cfg.logdir.mkdir(parents=True)
        vanished = self.cfg.logdir / "vanishing.log"
        old = self.cfg.logdir / "old.log"
        old.write_text("remove me")
        os.utime(old, (0, 0))
        attempt = self.state / "verification/old"
        attempt.mkdir(parents=True)
        os.utime(attempt, (0, 0))
        original = Path.iterdir

        def entries(path):
            if path == self.cfg.logdir:
                return iter((vanished, old))
            return original(path)

        with patch.object(Path, "iterdir", entries):
            report = retention.sweep(now=self.clock)
        self.assertFalse(old.exists())
        self.assertFalse(attempt.exists())
        self.assertFalse(report["errors"])

    def test_unreadable_managed_logs_does_not_prevent_cleanup_receipt(self):
        root = self.root / "managed/logs"
        original = Path.iterdir

        def entries(path):
            if path == root:
                raise PermissionError("managed logs unavailable")
            return original(path)

        with patch.object(Path, "iterdir", entries):
            report = retention.sweep(now=self.clock)
        self.assertTrue(report["errors"])
        self.assertEqual(read_json(activity.root() / "cleanup.json")["at"], self.clock)

    def test_disabled_absent_shared_pool_is_not_created_by_cleanup(self):
        retention.sweep(now=self.clock)
        self.assertFalse(shared_mathlib.root().exists())

    def test_retired_worker_private_archives_are_reclaimed_without_touching_credentials(self):
        self.old()
        private = self.state / "home/.cache/lake" / "artifact"
        private.parent.mkdir(parents=True)
        private.write_text("generated")
        credentials = self.state / "home/.codex/auth.json"
        credentials.parent.mkdir(parents=True)
        credentials.write_text("protected")
        retention.sweep(now=self.clock)
        self.assertFalse(private.exists())
        self.assertEqual(credentials.read_text(), "protected")

    def test_operator_archive_pool_and_private_hardlink_are_untouched(self):
        pool = self.root / "archives"
        pool.mkdir()
        archive = pool / "old.ltar"
        archive.write_text("immutable")
        os.utime(archive, (0, 0))
        reader = self.root / "reader.ltar"
        os.link(archive, reader)
        retention.sweep(now=self.clock)
        self.assertTrue(archive.exists())
        self.assertEqual(reader.read_text(), "immutable")

    def test_active_verification_job_protects_private_caches(self):
        self.old()
        cache = self.cache()
        lease = self.state / "verification-jobs" / "job.lock"
        with retention.locked(lease):
            report = retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())
        self.assertIn(str(lease), report["protected"])
        retention.sweep(now=self.clock)
        self.assertFalse(cache.exists())

    def test_symlinked_worker_home_cannot_delete_external_cache(self):
        self.old()
        outside = self.root / "outside"
        target = outside / ".cache" / "lake" / "keep"
        target.parent.mkdir(parents=True)
        target.write_text("outside")
        (self.state / "home").symlink_to(outside, target_is_directory=True)
        retention.sweep(now=self.clock)
        self.assertTrue(target.exists())

    def test_symlinked_checkout_parent_is_skipped(self):
        self.old()
        outside = self.root / "outside"
        self.checkout.parent.rename(outside)
        self.checkout.parent.symlink_to(outside, target_is_directory=True)
        cache = outside / "TauCeti" / ".lake"
        cache.mkdir()
        retention.sweep(now=self.clock)
        self.assertTrue(cache.exists())

    def test_hardlinks_preserve_other_workers_artifacts(self):
        self.old()
        cache = self.cache()
        other = self.root / "other-worker.olean"
        os.link(cache, other)
        retention.sweep(now=self.clock)
        self.assertFalse(cache.exists())
        self.assertEqual(other.read_text(), "generated")

    def test_interrupted_delete_is_reclaimed(self):
        self.old()
        trash = self.checkout / ".lake.retention-deleting"
        trash.mkdir()
        (trash / "partial").write_text("leftover")
        retention.sweep(now=self.clock)
        self.assertFalse(trash.exists())

    def test_existing_recovery_deletion_is_left_to_descriptor_housekeeping(self):
        trash = self.checkout.parent / work_recovery.RETAINED_DIR / "TauCeti-old.deleting"
        trash.mkdir(parents=True)
        (trash / "partial").write_text("source already saved by recovery")
        retention.sweep(now=self.clock)
        self.assertTrue(trash.exists())
        self.assertEqual((trash / "partial").read_text(), "source already saved by recovery")

    def test_inaccessible_generated_directory_can_be_removed(self):
        target = self.checkout / ".lake"
        target.mkdir()
        child = target / "overlay-work"
        child.mkdir()
        child.chmod(0)
        report = retention.fresh_report()
        retention.remove(target, self.checkout.parent.parent, report)
        self.assertFalse(target.exists())
        self.assertFalse(report["errors"])

    def test_interrupted_full_checkout_delete_preserves_young_replacement(self):
        self.old()
        (self.checkout / "Source.lean").write_text("saved before removal")
        original = shutil.rmtree

        def interrupted(path, *args, **kwargs):
            if Path(path).name == "TauCeti.retention-deleting":
                raise OSError("interrupted deletion")
            return original(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", side_effect=interrupted):
            result = retention.sweep(now=self.clock)
        self.assertTrue(result["errors"])
        trash = self.checkout.with_name("TauCeti.retention-deleting")
        self.assertTrue(trash.exists())
        self.assertEqual(len(list(work_recovery.records(self.state.parent))), 1)
        self.checkout.mkdir()
        young = self.cache()
        atomic_json(self.state / "cache-generation.json", dict(started_at=self.clock))
        retention.recycle(self.cfg, now=self.clock + 1)
        self.assertFalse(trash.exists())
        self.assertTrue(young.exists())

    def test_failed_source_save_preserves_source_but_evicts_builds(self):
        self.old()
        self.cache()
        (self.checkout / "Source.lean").write_text("unpublished")
        with patch.object(work_recovery, "_record", side_effect=work_recovery.RecordError("disk full")):
            retention.sweep(now=self.clock)
        self.assertEqual((self.checkout / "Source.lean").read_text(), "unpublished")
        self.assertFalse((self.checkout / ".lake").exists())

    def test_logs_age_and_attempt_lease(self):
        self.cfg.logdir.mkdir(parents=True)
        old = self.cfg.logdir / "old.log"
        new = self.cfg.logdir / "new.log"
        old.write_text("old")
        new.write_text("new")
        usage = self.cfg.logdir / "usage.jsonl"
        usage.write_text("accounting")
        os.utime(usage, (0, 0))
        os.utime(old, (self.clock - retention.LIFETIME, self.clock - retention.LIFETIME))
        attempt = self.state / "verification" / "old"
        attempt.mkdir(parents=True)
        with retention.locked(attempt / "live.lock"):
            os.utime(attempt, (0, 0))
            retention.sweep(now=self.clock)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(usage.read_text(), "accounting")
        self.assertTrue(attempt.exists())
        retention.sweep(now=self.clock)
        self.assertFalse(attempt.exists())

    def test_log_sweep_scans_processes_once_and_bounds_its_receipt(self):
        self.cfg.logdir.mkdir(parents=True)
        for n in range(250):
            path = self.cfg.logdir / f"old-{n}.log"
            path.write_text("diagnostic")
            os.utime(path, (0, 0))
        active = self.cfg.logdir / "active.log"
        with active.open("w") as output:
            output.write("still open")
            output.flush()
            os.utime(active, (0, 0))
            with patch.object(retention, "open_files", wraps=retention.open_files) as scan:
                result = retention.sweep(now=self.clock)
        scan.assert_called_once_with()
        self.assertTrue(active.exists())
        self.assertEqual(result["removed_count"], 250)
        self.assertEqual(len(result["removed"]), 100)
        self.assertIn(str(active), result["protected"])

    def test_unrelated_retained_failure_does_not_repeat_active_cache_eviction(self):
        self.old()
        retained = self.checkout.parent / work_recovery.RETAINED_DIR / "TauCeti-old"
        retained.mkdir(parents=True)
        (retained / "source").write_text("not a git checkout")
        os.utime(retained, (0, 0))
        self.cache()
        report = retention.sweep(now=self.clock)
        self.assertTrue(report["protected"])
        self.assertEqual(read_json(self.state / "cache-generation.json")["started_at"], self.clock)
        fresh = self.cache()
        retention.recycle(self.cfg, now=self.clock + 1)
        self.assertTrue(fresh.exists())

    def test_retained_source_uses_its_own_owner_and_removes_sidecar(self):
        retained = self.checkout.parent / work_recovery.RETAINED_DIR / "TauCeti-old"
        retained.parent.mkdir()
        subprocess.run(["git", "clone", "-q", str(self.checkout), str(retained)], check=True)
        (retained / "Source.lean").write_text("retained repair")
        atomic_json(work_recovery.owner_path(self.cfg, "TauCeti"), dict(pr=99, round_id="new-round"))
        sidecar = retained.with_name(retained.name + ".owner.json")
        dead = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json; from tauceti_worker.work_recovery import process_owner; print(json.dumps(process_owner()))",
                ],
                text=True,
            )
        )
        atomic_json(sidecar, dict(pr=42, round_id="old-round", work="fix", process=dead))
        os.utime(retained, (0, 0))
        retention.sweep(now=self.clock)
        self.assertTrue(retained.exists())  # storage timer only registers the retained root
        work_recovery.housekeep(self.state.parent, seconds=5, entries=50000)
        self.assertFalse(retained.exists())
        self.assertFalse(sidecar.exists())
        [record] = list(work_recovery.records(self.state.parent))
        self.assertEqual(record[2]["pr"], 42)
        self.assertEqual(record[2]["source_round"], "old-round")
        self.assertEqual(record[3]["state"], "checkout-reset")

    def test_stash_reflog_and_credentials_prevent_full_checkout_discard(self):
        self.old()
        self.cache()
        (self.checkout / "Source.lean").write_text("stashed")
        self.git("stash", "push", "-qm", "unfinished")
        retention.sweep(now=self.clock)
        self.assertTrue(self.checkout.exists())
        self.assertIn("stashed", self.git("show", "stash:Source.lean"))
        self.assertFalse((self.checkout / ".lake").exists())
        self.git("stash", "clear")
        (self.checkout / "Source.lean").write_text("reflog-only")
        self.git("commit", "-qam", "unpublished")
        commit = self.git("rev-parse", "HEAD").strip()
        self.git("reset", "--hard", "HEAD~1")
        saved = work_recovery.save_for_cleanup(self.cfg, self.checkout)
        self.assertTrue(saved.ok)
        self.assertIsNotNone(saved.record)
        self.assertIn(f"history/{commit}", read_json(saved.record / "manifest.json")["bundle_refs"])
        (self.checkout / ".env").write_text("secret")
        report = retention.fresh_report()
        retention.collect_retired(self.cfg, report, now=self.clock, discard_checkout=True)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.git("show", f"{commit}:Source.lean"), "reflog-only")
        decision = work_recovery.save_for_cleanup(self.cfg, self.checkout)
        self.assertFalse(decision.ok)
        self.assertEqual((self.checkout / ".env").read_text(), "secret")

    def test_cache_permission_error_does_not_abort_unrelated_reclamation(self):
        self.old()
        cache = self.cache()
        private = self.state / "home/.cache/lake/other"
        private.parent.mkdir(parents=True)
        private.write_text("other cache")
        original = shutil.rmtree

        def fail_one(path, *args, **kwargs):
            if Path(path).name.startswith(".lake"):
                raise PermissionError("fixture permission failure")
            return original(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", side_effect=fail_one):
            result = retention.recycle(self.cfg, now=self.clock)
        self.assertTrue(result["errors"])
        self.assertFalse(private.exists())
        self.assertFalse(cache.exists())  # the failed delete was renamed, not lost
        self.assertTrue((self.checkout / ".lake.retention-deleting").exists())

    def test_dry_run_releases_locks_before_inspection(self):
        path = self.state / "round.lock"
        path.touch()
        with retention.locked(path, dry=True):
            with retention.locked(path):
                pass

    def test_pending_job_keeps_its_attempt_even_without_live_process(self):
        attempt = self.state / "verification" / "old"
        attempt.mkdir(parents=True)
        (attempt / "receipt.json").write_text("evidence")
        os.utime(attempt, (0, 0))
        atomic_json(self.state / "verification-pending/job.json", dict(receipt=str(attempt / "receipt.json")))
        result = retention.sweep(now=self.clock)
        self.assertTrue(attempt.exists())
        self.assertIn(str(attempt), result["protected"])

    def test_pending_reference_through_a_root_alias_keeps_its_attempt(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        attempt = self.state / "verification/old"
        attempt.mkdir(parents=True)
        os.utime(attempt, (0, 0))
        atomic_json(self.state / "verification-pending/job.json", dict(receipt=str(alias / "verification/old/receipt")))
        retention.sweep(now=self.clock)
        self.assertTrue(attempt.exists())

    def test_pending_bubble_paths_keep_the_host_attempt(self):
        state = self.state / "verification-state"
        attempt = state / "verification/old"
        attempt.mkdir(parents=True)
        (attempt / "receipt.json").write_text("needed bubble receipt")
        atomic_json(
            state / "verification-pending/job.json",
            dict(
                lease="/opt/verification-state/verification/old/live.lock",
                receipt="/opt/verification-state/verification/old/receipt.json",
            ),
        )
        os.utime(attempt, (0, 0))
        retention.sweep(now=self.clock)
        self.assertTrue((attempt / "receipt.json").exists())

    def test_pending_verification_keeps_its_source_checkout_until_completed(self):
        self.old()
        self.cache()
        source = self.checkout / "Source.lean"
        source.write_text("pending exact candidate")
        pending = self.state / "verification-pending/job.json"
        atomic_json(pending, dict(root=str(self.checkout)))
        report = retention.sweep(now=self.clock)
        self.assertEqual(source.read_text(), "pending exact candidate")
        self.assertIn(str(self.checkout), report["protected"])
        pending.unlink()
        report = retention.fresh_report()
        retention.collect_retired(self.cfg, report, now=self.clock, discard_checkout=True)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(len(list(work_recovery.records(self.state.parent))), 1)

    def test_interrupted_deletion_obeys_live_job_lock_and_pending_reference(self):
        trash = self.checkout / ".lake.retention-deleting"
        trash.mkdir()
        artifact = trash / "artifact"
        artifact.write_text("still needed")
        with retention.locked(self.state / "verification-jobs/job.lock"):
            retention.recycle(self.cfg, now=self.clock)
        self.assertTrue(artifact.exists())
        pending = self.state / "verification-pending/job.json"
        atomic_json(pending, dict(root=str(trash)))
        retention.recycle(self.cfg, now=self.clock)
        self.assertTrue(artifact.exists())
        pending.unlink()
        retention.recycle(self.cfg, now=self.clock)
        self.assertFalse(artifact.exists())

    def test_partial_cache_failure_does_not_recycle_new_caches_next_round(self):
        self.old()
        self.cache()
        original = shutil.rmtree

        def fail_one(path, *args, **kwargs):
            if Path(path).name == ".lake.retention-deleting":
                raise PermissionError("old cache cannot be removed yet")
            return original(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", side_effect=fail_one):
            retention.recycle(self.cfg, now=self.clock)
            fresh = self.state / "home/.cache/lake/new-archive"
            fresh.parent.mkdir(parents=True)
            fresh.write_text("new generation")
            replacement = self.cache()
            retention.recycle(self.cfg, now=self.clock + 1)
        self.assertTrue(fresh.exists())
        self.assertTrue(replacement.exists())
        retention.recycle(self.cfg, now=self.clock + 2)
        self.assertFalse((self.checkout / ".lake.retention-deleting").exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(replacement.exists())

    def test_retained_cleanup_is_bounded_and_uncertain_candidates_do_not_starve_others(self):
        parent = self.checkout.parent / work_recovery.RETAINED_DIR
        for n in range(4):
            tree = parent / f"TauCeti-{n}"
            tree.mkdir(parents=True)
            (tree / "source").write_text("uncertain source")
            cache = tree / ".lake"
            cache.mkdir()
            os.utime(tree, (0, 0))
        with patch.object(retention, "evict_tree", side_effect=AssertionError("second retained-tree collector")):
            retention.collect_retired(self.cfg, retention.fresh_report(), now=self.clock)
            retention.collect_retired(self.cfg, retention.fresh_report(), now=self.clock)
        self.assertEqual(len(list(parent.glob("*/.lake"))), 4)
        self.assertEqual(len(list(parent.glob("*/source"))), 4)
        # Unknown Git/ownership stays recoverable; bounded housekeeping can still process others.
        work_recovery.housekeep(self.state.parent, seconds=5, entries=50000)
        self.assertEqual(len(list(parent.glob("*/source"))), 4)

    def test_dry_run_retains_checkout_with_uncaptured_git_refs(self):
        self.old()
        self.git("tag", "local-meaningful-tag")
        before = self.snapshot()
        report = retention.sweep(now=self.clock, dry=True)
        self.assertNotIn(str(self.checkout), report["removed"])
        self.assertTrue(report["protected"])
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_respects_existing_source_archive_limits(self):
        self.old()
        (self.checkout / "unpublished.lean").write_text("source beyond fixture limit")
        before = self.snapshot()
        with patch.object(work_recovery, "MAX_UNTRACKED_BYTES", 1):
            report = retention.sweep(now=self.clock, dry=True)
        self.assertNotIn(str(self.checkout), report["removed"])
        self.assertTrue(report["protected"])
        self.assertEqual(self.snapshot(), before)

    def test_pressure_cleanup_only_unlinks_closed_diagnostics(self):
        self.old()
        cache = self.cache()
        self.cfg.logdir.mkdir(parents=True)
        closed = self.cfg.logdir / "old.log"
        closed.write_text("diagnostic")
        os.utime(closed, (0, 0))
        with (
            patch.object(retention, "recycle", side_effect=AssertionError("no workspace collection on retry")),
            patch.object(retention, "collect_retired", side_effect=AssertionError("no source archival on retry")),
            patch.object(shared_mathlib, "collect", side_effect=AssertionError("no recursive GC on retry")),
        ):
            report = retention.run(diagnostics_only=True)
        self.assertFalse(closed.exists())
        self.assertTrue(cache.exists())
        self.assertEqual(report.get("protected_count", 0), 0)

    def test_cleanup_pins_activity_before_project_resolution(self):
        expected = self.root / "login/.local/state/tauceti/activity"

        def configs():
            os.environ.setdefault("TAUCETI_ACTIVITY_DIR", str(self.root / "wrong-project-activity"))
            return [self.cfg]

        with patch.dict(os.environ), patch.object(Path, "home", return_value=self.root / "login"):
            del os.environ["TAUCETI_ACTIVITY_DIR"]
            with patch.object(retention, "worker_configs", side_effect=configs):
                retention.run()
            self.assertEqual(activity.root(), expected)
        self.assertTrue((expected / "cleanup.json").exists())

    def test_status_only_reads_prepared_cleanup_data(self):
        retention.sweep(now=self.clock)
        activity.prepare(now=self.clock)
        with (
            patch.object(retention, "sweep", side_effect=AssertionError("status must not collect")),
            patch.object(Path, "iterdir", side_effect=AssertionError("status must not scan storage")),
            patch.object(Path, "rglob", side_effect=AssertionError("status must not scan storage")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("status must not launch GitHub reads")),
        ):
            value = activity.cached()
        self.assertEqual(value["summary"]["storage_cleanup"]["at"], self.clock)

    def test_nonworker_state_directories_are_not_discovered(self):
        foreign = self.state.parent / "github-shared"
        foreign.mkdir()
        with patch.object(retention, "HERE", self.root), patch.object(config.Config, "resolve", return_value=self.cfg):
            self.assertEqual([cfg.wid for cfg in REAL_WORKER_CONFIGS()], [self.cfg.wid])
        self.assertFalse((foreign / "cache-generation.json").exists())

    def test_same_device_bind_mount_and_unavailable_mount_evidence_are_protected(self):
        cache = self.cache()
        root = self.checkout / ".lake"
        for mounts in ([root / "build"], None):
            report = retention.fresh_report()
            with patch.object(retention, "mount_points", return_value=mounts):
                retention.remove(root, self.checkout.parent.parent, report)
            self.assertTrue(cache.exists())
            self.assertTrue(report["protected"])

    def test_replacement_does_not_bypass_protection_on_existing_deletion(self):
        cache = self.cache()
        trash = self.checkout / ".lake.retention-deleting"
        trash.mkdir()
        (trash / "protected").write_text("mounted old generation")
        report = retention.fresh_report()
        with patch.object(retention, "mount_points", return_value=[trash]):
            retention.remove(self.checkout / ".lake", self.checkout.parent.parent, report)
        self.assertTrue(cache.exists())
        self.assertTrue((trash / "protected").exists())

    def test_daily_log_rotation_keeps_a_running_loop_bounded(self):
        with (
            patch.object(config, "_LOG_FH", None),
            patch.object(config, "_LOG_DIR", None),
            patch.object(config, "_LOG_DAY", None),
        ):
            with patch.object(config.time, "strftime", return_value="20261020"):
                config.set_log_file(self.cfg.logdir)
                first = config._LOG_FH
            with patch.object(config.time, "strftime", return_value="20261021"):
                config.log("next day")
                self.assertTrue(first.closed)
                self.assertEqual(Path(config._LOG_FH.name).name, "work-20261021.log")
                config._LOG_FH.close()

    def test_concurrent_log_rollover_does_not_close_another_threads_writer(self):
        errors = []
        with (
            patch.object(config, "_LOG_FH", None),
            patch.object(config, "_LOG_DIR", None),
            patch.object(config, "_LOG_DAY", None),
        ):
            with patch.object(config.time, "strftime", return_value="20261020"):
                config.set_log_file(self.cfg.logdir)
            barrier = threading.Barrier(8)

            def write():
                try:
                    barrier.wait()
                    config.log("concurrent rollover")
                except Exception as error:
                    errors.append(error)

            with patch.object(config.time, "strftime", return_value="20261021"):
                threads = [threading.Thread(target=write) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertFalse(errors)
                config._LOG_FH.close()
            self.assertEqual((self.cfg.logdir / "work-20261021.log").read_text().count("concurrent rollover"), 8)

    def test_daily_timer_is_persistent_and_low_priority(self):
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}),
            patch.object(subprocess, "run") as run,
        ):
            retention.install_timer()
        directory = self.root / "xdg/systemd/user"
        self.assertIn("OnCalendar=hourly", (directory / "tauceti-cleanup.timer").read_text())
        self.assertIn("Persistent=true", (directory / "tauceti-cleanup.timer").read_text())
        self.assertIn("IOSchedulingClass=idle", (directory / "tauceti-cleanup.service").read_text())
        self.assertIn("TAUCETI_ACTIVITY_DIR=", (directory / "tauceti-cleanup.service").read_text())
        self.assertEqual(run.call_count, 2)

    def test_timer_install_reports_unsupported_platform(self):
        with patch.object(sys, "platform", "darwin"), self.assertRaisesRegex(config.Die, "requires Linux"):
            retention.install_timer()

    def test_timer_failure_does_not_fail_an_already_started_worker_service(self):
        from tauceti_worker import worker_manager

        service = self.root / "config/systemd/user/tauceti-workers.service"
        with (
            patch.object(worker_manager, "_service_path", return_value=service),
            patch.object(worker_manager, "_systemd_unit", return_value="fixture unit"),
            patch.object(shutil, "which", return_value="systemctl"),
            patch.object(subprocess, "run"),
            patch.object(retention, "install_timer", side_effect=config.Die("fixture timer unavailable")),
        ):
            self.assertEqual(worker_manager.service_action("install", self.root / "workers.toml"), 0)
        self.assertEqual(service.read_text(), "fixture unit")

    def test_macos_boundary_recycles_only_the_managed_private_home(self):
        from tauceti_worker.agents import _worker_iso_home

        self.old()
        with patch.object(sys, "platform", "darwin"):
            home = _worker_iso_home(self.cfg.wid, _base=self.cfg.home)
        cache = home / ".cache/lake/artifact"
        cache.parent.mkdir(parents=True)
        cache.write_text("private generated cache")
        credentials = home / ".codex/auth.json"
        credentials.parent.mkdir()
        credentials.write_text("credentials")
        login_cache = self.cfg.home / ".cache/lake/operator-artifact"
        login_cache.parent.mkdir(parents=True)
        login_cache.write_text("operator cache")
        with patch.object(sys, "platform", "darwin"):
            retention.recycle(self.cfg, now=self.clock)
        self.assertFalse(cache.exists())
        self.assertEqual(credentials.read_text(), "credentials")
        self.assertTrue(login_cache.exists())

    def test_managed_console_rotates_without_restarting_its_process(self):
        old = self.root / "managed/logs/retired/work-old.log"
        old.parent.mkdir(parents=True)
        env = dict(
            os.environ,
            TAUCETI_CONSOLE_LOG_FILE=str(old),
            TAUCETI_CONSOLE_LOG_DAY="19000101",
            TAUCETI_RUNTIME_STATUS=str(self.root / "managed-status.json"),
        )
        script = """
from tauceti_worker.config import rotate_console_log
print('before rotation', flush=True)
rotate_console_log()
print('after rotation', flush=True)
"""
        with old.open("w") as output:
            result = subprocess.run(
                [sys.executable, "-c", script], env=env, stdout=output, stderr=subprocess.STDOUT, timeout=10
            )
        self.assertEqual(result.returncode, 0)
        current = Path(read_json(self.root / "managed-status.json")["log_file"])
        self.assertEqual(old.read_text(), "before rotation\n")
        self.assertEqual(current.read_text(), "after rotation\n")

    def test_console_rotation_preserves_helper_protocol_stdout(self):
        old = self.root / "console/work-old.log"
        old.parent.mkdir()
        old.touch()
        status = self.root / "helper-status.json"
        env = dict(
            os.environ,
            TAUCETI_CONSOLE_LOG_FILE=str(old),
            TAUCETI_CONSOLE_LOG_DAY="19000101",
            TAUCETI_RUNTIME_STATUS=str(status),
        )
        script = """
from tauceti_worker.config import rotate_console_log
rotate_console_log()
print('{"result":"kept on protocol stdout"}', flush=True)
"""
        # Both streams can be helper-owned, or stderr alone can still be the managed console.
        for console_stderr in (False, True):
            with old.open("a") as output:
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=output if console_stderr else subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), '{"result":"kept on protocol stdout"}')
            self.assertEqual(status.exists(), console_stderr)

    def test_managed_logs_cleanup_keeps_an_open_console_and_accounting(self):
        logs = self.root / "managed/logs/retired"
        logs.mkdir(parents=True)
        old = logs / "work-old.log"
        old.write_text("closed")
        os.utime(old, (0, 0))
        usage = logs / "usage.jsonl"
        usage.write_text("accounting")
        with (logs / "work-active.log").open("w") as active:
            active.write("active")
            active.flush()
            os.utime(active.name, (0, 0))
            retention.sweep(now=self.clock)
            self.assertTrue(Path(active.name).exists())
        self.assertFalse(old.exists())
        self.assertEqual(usage.read_text(), "accounting")

    def test_daily_collection_preserves_shared_reader(self):
        pool = shared_mathlib.root()
        key = "a" * 64
        entry = pool / "entries" / key
        entry.mkdir(parents=True)
        os.utime(entry, (0, 0))
        with shared_mathlib.key_lock(pool, key, exclusive=False):
            retention.sweep(now=self.clock)
        self.assertTrue(entry.exists())
        retention.sweep(now=self.clock)
        self.assertFalse(entry.exists())


if __name__ == "__main__":
    unittest.main()

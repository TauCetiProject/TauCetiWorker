#!/usr/bin/env python3
"""Isolated local K3 fixtures: bounded warm lookup, crash boundaries and resumable cleanup."""

import contextlib
import errno
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

from work_recovery import allow_cleanup, fixture, git, reconstruct, snapshot_tree

from tauceti_worker import recovery_index as index
from tauceti_worker import recovery_maintenance as maintenance
from tauceti_worker import work_recovery as wr


class Crash(BaseException):
    pass


class RecoveryMaintenance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "shared"),
                "TAUCETI_FLEET_EVENTS": str(self.root / "events.jsonl"),
                "TAUCETI_WORK_RECOVERY": "on",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.here = patch.object(wr, "HERE", self.root)
        self.here.start()
        self.addCleanup(self.here.stop)
        self.origin, self.co, self.cfg = fixture(self.root)
        # Every writable recovery/configuration path is inside this disposable fixture.
        for path in (self.cfg.state, self.cfg.checkout, self.cfg.data_home, wr.HERE):
            self.assertTrue(path.is_relative_to(self.root))
        self.addCleanup(maintenance._SWEEPS.clear)

    def dirty(self):
        (self.co / "Draft.lean").write_text("-- exact saved source\n")
        return snapshot_tree(self.co)

    def tick(self, **budgets):
        return wr.housekeep(self.cfg.state.parent, **({"seconds": 5, "entries": 10000} | budgets))

    def restored(self, record, expected):
        dest = self.root / f"restore-{len(list(self.root.glob('restore-*')))}"
        clone = reconstruct(self.origin, record, self.root, dest.name)
        self.assertEqual(snapshot_tree(clone), expected)

    def retained(self):
        tree = Path(wr.retain(self.cfg, self.co, "fixture").details["retained"])
        allow_cleanup(self.cfg, self.co)
        return tree

    def test_warm_lookup_independent_of_history(self):
        self.dirty()
        # First make a valid historical descriptor with a relatively large archive.
        (self.co / "History.lean").write_bytes(b"h" * 1024 * 1024)
        historical = wr.guard(self.cfg, self.co).record
        (self.co / "History.lean").unlink()
        current = wr.guard(self.cfg, self.co).record
        baseline = wr.guard(self.cfg, self.co).details["preparation"]
        for n in range(32):
            copy = self.cfg.state / "recovery" / f"history-{n}"
            shutil.copytree(historical, copy)
        parent = self.co.parent / wr.RETAINED_DIR
        for n in range(32):
            cache = parent / f"old-{n}" / ".lake"
            cache.mkdir(parents=True)
            with (cache / "large").open("wb") as out:
                out.truncate(1024 * 1024 * 1024)  # 32 GiB logical history; sparse fixture only
        digested = []
        real_digest = wr._digest

        def digest(path):
            self.assertTrue(path.is_relative_to(self.co), f"historic payload read: {path}")
            digested.append(path)
            return real_digest(path)

        with (
            patch.object(wr, "_digest", digest),
            patch.object(wr, "_complete", side_effect=AssertionError("warm lookup hashed a descriptor")),
            patch.object(maintenance, "_remove", side_effect=AssertionError("foreground cleanup")),
        ):
            decision = wr.guard(self.cfg, self.co)
        self.assertEqual(decision.record, current)
        after = decision.details["preparation"]
        for metric in ("read_bytes", "hash_bytes", "metadata_entries"):
            self.assertEqual(after[metric], baseline[metric], metric)
        self.assertEqual(digested, [self.co / "Draft.lean", self.co / "Draft.lean"])
        print("warm fixture (32 MiB descriptors + 32 GiB sparse caches):", after)

    def test_committed_history_lookup_survives_head_changes(self):
        git(self.co, "checkout", "-qb", "unfinished")
        (self.co / "A.lean").write_text("local-only committed source")
        git(self.co, "commit", "-qam", "unfinished")
        head = git(self.co, "rev-parse", "HEAD").strip()
        expected = snapshot_tree(self.co)
        saved = wr.guard(self.cfg, self.co).record
        git(self.co, "checkout", "-qB", "main", "origin/main")
        with patch.object(wr, "_complete", side_effect=AssertionError("warm branch lookup rehashed bundle")):
            for _ in range(3):
                self.assertEqual(wr.guard(self.cfg, self.co).record, saved)
        self.assertEqual(len(list((self.cfg.state / "recovery").glob("*/manifest.json"))), 1)
        self.restored(saved, expected)
        # A replaced valid manifest is not the source identity bound to this artifact index.
        changed = wr._read_manifest(saved)
        changed["fingerprint"] = "replacement-source"
        wr.atomic_json(saved / "manifest.json", changed)
        replacement = wr.guard(self.cfg, self.co).record
        self.assertNotEqual(replacement, saved)
        saved = replacement
        # Invalidated artifact evidence never exempts commits from preservation.
        (saved / "commits.bundle").unlink()
        replacement = wr.guard(self.cfg, self.co).record
        self.assertNotEqual(replacement, saved)
        restored = reconstruct(self.origin, replacement, self.root, "restored-branch-history")
        self.assertEqual(git(restored, "show", f"{head}:A.lean"), "local-only committed source")

    def test_detached_committed_source_is_saved_and_reclaimed(self):
        git(self.co, "checkout", "-q", "--detach")
        (self.co / "A.lean").write_text("detached local source")
        git(self.co, "commit", "-qam", "detached")
        expected = snapshot_tree(self.co)
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_parked_candidates_do_not_repeat_source_analysis(self):
        self.dirty()
        (self.co / ".env").write_text("fixture secret")
        tree = self.retained()
        path = maintenance._job_path(self.cfg.state, tree)
        maintenance._write_job(path, dict(tree=str(tree), phase="save", allowance_level=3, stack=[""]))
        self.tick()
        job = maintenance._read_job(path)
        self.assertEqual(job["next_action"], "retain-unarchived-metadata")
        self.assertEqual(job["allowance_level"], 3)
        with patch.object(wr, "inspect", side_effect=AssertionError("parked source reinspection")):
            for _ in range(3):
                result = self.tick()
                self.assertEqual(result["hash_bytes"], 0)
            with patch.object(maintenance.time, "time", return_value=time.time() + 3600):
                self.assertEqual(self.tick()["hash_bytes"], 0)  # no repeated 30-minute source scan
        (tree / ".env").unlink()  # metadata change retries at the last successful allowance
        self.tick()
        self.assertFalse(tree.exists())

    def test_gitless_candidate_never_touches_enclosing_repository(self):
        self.dirty()
        git(self.root, "init", "-q")
        shutil.rmtree(self.co / ".git")  # isolated incomplete-clone fixture
        tree = self.retained()
        refs = git(self.root, "for-each-ref")
        with patch.object(wr, "_git", side_effect=AssertionError("Git discovery escaped candidate")):
            self.tick()
        self.assertTrue((tree / "Draft.lean").exists())
        self.assertEqual(git(self.root, "for-each-ref"), refs)
        self.assertFalse((self.cfg.state / "recovery").exists())

    def test_malformed_history_and_index_contention_do_not_freeze_inventory(self):
        self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        malformed = self.cfg.state / "recovery" / "malformed"
        shutil.copytree(saved, malformed)
        manifest = wr._read_manifest(malformed)
        manifest["checkout"] = None
        wr.atomic_json(malformed / "manifest.json", manifest)
        wr.atomic_json(self.cfg.state / "recovery-prepared.json", dict(records=99))
        with patch.object(index, "remember", side_effect=BlockingIOError("busy index")):
            self.tick()
        summary = wr.summary(self.cfg.state.parent)
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["corrupt"], 1)
        self.assertEqual(summary["errors"], 0)

    def test_one_failed_record_does_not_freeze_same_worker_inventory(self):
        self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        other = self.cfg.state / "recovery" / "other"
        shutil.copytree(saved, other)
        process = maintenance.Sweep.process

        def fail(sweep, state, kind, path, root, work):
            if kind == "record" and path == saved:
                raise OSError(errno.EIO, "one failed historical item")
            return process(sweep, state, kind, path, root, work)

        with patch.object(maintenance.Sweep, "process", fail):
            self.tick()
        summary = wr.summary(self.cfg.state.parent)
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertTrue(any("failed historical item" in r for r in summary["reasons"]))

    def test_short_index_contention_preserves_completed_lookup(self):
        import fcntl

        self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        manifest = wr._read_manifest(saved)
        with (self.cfg.state / "recovery-index.lock").open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            def release():
                time.sleep(0.005)
                fcntl.flock(held, fcntl.LOCK_UN)

            thread = threading.Thread(target=release)
            thread.start()
            try:
                index.remember(self.cfg.state, saved, manifest, verified=True, evidence=index.stamps(saved, manifest))
            finally:
                thread.join()
        self.assertEqual(
            index.lookup(self.cfg.state, manifest["repository"], manifest["fingerprint"])["record"], saved.name
        )

    def test_surviving_ordinary_session_member_protects_retained_source(self):
        self.dirty()
        program = """import json, subprocess, sys
from tauceti_worker.work_recovery import process_owner
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd='/', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps(dict(owner=process_owner(), child=child.pid)))
"""
        result = json.loads(subprocess.check_output([sys.executable, "-c", program], text=True, start_new_session=True))
        child = result["child"]

        def terminate():
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGTERM)  # only this isolated fixture child

        self.addCleanup(terminate)
        tree = self.retained()
        wr.atomic_json(tree.with_name(tree.name + ".owner.json"), dict(process=result["owner"]))
        self.tick()
        self.assertTrue(tree.exists())
        terminate()
        for _ in range(100):
            try:
                if Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.01)
        self.tick()
        self.assertFalse(tree.exists())

    def test_corrupt_index_and_missing_payload_never_authorize_reset(self):
        expected = self.dirty()
        first = wr.guard(self.cfg, self.co).record
        manifest = wr._read_manifest(first)
        cache = index.location(self.cfg.state, manifest["repository"], manifest["fingerprint"])
        for data in ("{interrupted", json.dumps({"version": 999}), "x" * 17000, "[" * 2000 + "]" * 2000):
            cache.write_text(data)
            decision = wr.guard(self.cfg, self.co)
            self.assertTrue(decision.ok)
            self.assertNotEqual(decision.record, first)
            self.restored(decision.record, expected)
            first = decision.record
        # Same-size corruption with mtime restored still changes ctime, invalidating the receipt.
        payload = first / "untracked.tar"
        before = payload.stat()
        data = payload.read_bytes()
        payload.write_bytes(b"!" + data[1:])
        os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
        decision = wr.guard(self.cfg, self.co)
        self.assertNotEqual(decision.record, first)
        self.restored(decision.record, expected)
        (decision.record / "staged.patch").unlink()
        newer = wr.guard(self.cfg, self.co)
        self.assertNotEqual(newer.record, decision.record)
        self.restored(newer.record, expected)
        (newer.record / "manifest.json").unlink()  # interrupted external descriptor deletion
        fresh = wr.guard(self.cfg, self.co).record
        self.restored(fresh, expected)
        (fresh / "manifest.json").write_text("[" * 2000 + "]" * 2000)
        self.restored(wr.guard(self.cfg, self.co).record, expected)

    def test_changed_manifest_metadata_is_not_preservation_evidence(self):
        expected = self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        manifest = wr._read_manifest(saved)
        manifest["local_head"] = "f" * 40  # payloads and fingerprint still superficially match
        wr.atomic_json(saved / "manifest.json", manifest)
        decision = wr.guard(self.cfg, self.co)
        self.assertNotEqual(decision.record, saved)
        self.restored(decision.record, expected)
        # Reconciliation cannot turn structurally valid but wrong source metadata into authority.
        current = decision.record
        manifest = wr._read_manifest(current)
        manifest["local_head"] = "e" * 40
        wr.atomic_json(current / "manifest.json", manifest)
        location = index.location(self.cfg.state, manifest["repository"], manifest["fingerprint"])
        location.unlink()
        index.remember(self.cfg.state, current, manifest, verified=False)
        self.restored(wr.guard(self.cfg, self.co).record, expected)

    def test_interrupted_publication_reconstructs_unverified_index(self):
        expected = self.dirty()
        with patch.object(index, "remember", side_effect=OSError(errno.ENOSPC, "fixture")):
            saved = wr.guard(self.cfg, self.co).record
        self.restored(saved, expected)
        self.tick()  # reconstruction registers metadata, never claims historical bytes verified
        manifest = wr._read_manifest(saved)
        cached = index.lookup(self.cfg.state, manifest["repository"], manifest["fingerprint"])
        self.assertIs(cached["verified"], False)
        with (
            patch.object(wr, "_archive", side_effect=AssertionError("unnecessary rearchive")),
            patch.object(index, "remember", side_effect=OSError(errno.ENOSPC, "disposable index fixture")),
        ):
            decision = wr.guard(self.cfg, self.co)
        self.assertEqual(decision.record, saved)
        self.assertGreater(decision.details["preparation"]["hash_bytes"], 10000)
        self.restored(saved, expected)

    def test_crash_before_descriptor_rename_leaves_exact_source(self):
        expected = self.dirty()
        rename = os.rename

        def crash(source, dest):
            if Path(source).name.startswith(".tmp-"):
                raise Crash()
            return rename(source, dest)

        with patch.object(wr.os, "rename", crash), self.assertRaises(Crash):
            wr.guard(self.cfg, self.co)
        self.assertEqual(snapshot_tree(self.co), expected)
        self.assertTrue(list((self.cfg.state / "recovery").glob(".tmp-*")))
        self.restored(wr.guard(self.cfg, self.co).record, expected)

    def test_large_cache_cleanup_resumes_with_all_budgets_and_restart(self):
        expected = self.dirty()
        cache = self.co / ".lake"
        cache.mkdir()
        for n in range(50):
            (cache / str(n)).write_bytes(b"c" * 8192)
        with (cache / "large").open("wb") as out:
            out.truncate(3 * 1024 * 1024)
        tree = self.retained()
        for _ in range(100):
            maintenance._SWEEPS.clear()  # model preparer death; durable destructive cursor survives
            result = self.tick(entries=180, bytes=192 * 1024)
            self.assertLessEqual(result["metadata_entries"], 180)
            self.assertLessEqual(result["read_bytes"] + result["removed_bytes"], 192 * 1024)
            if not tree.exists() and not tree.with_name(tree.name + ".deleting").exists():
                break
            records = list((self.cfg.state / "recovery").glob("*/manifest.json"))
            if records:
                self.restored(records[-1].parent, expected)
            else:
                self.assertEqual((tree / "Draft.lean").read_text(), "-- exact saved source\n")
        else:
            self.fail("bounded cleanup failed to make progress")
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)
        self.assertTrue(self.co.parent.exists())
        self.assertFalse(list((self.cfg.state / "recovery-cleanup" / "jobs").glob("*.json")))

    def test_interrupted_recursive_delete_and_changed_evidence(self):
        expected = self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        tree = self.retained()
        unlink = os.unlink

        def fail(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "fixture deletion interruption")
            return unlink(path, *args, **kwargs)

        with patch.object(maintenance.os, "unlink", fail):
            self.tick()
        doomed = tree.with_name(tree.name + ".deleting")
        self.assertTrue(doomed.exists())
        job = maintenance._read_job(maintenance._job_path(self.cfg.state, tree))
        self.assertEqual(job["next_action"], "resume-removal")
        self.restored(saved, expected)
        # Loss of evidence during partial deletion must stop further mutation, automatically retry.
        payload = saved / "untracked.tar"
        original = payload.read_bytes()
        payload.write_bytes(b"bad")
        self.tick()
        self.assertTrue(doomed.exists())
        payload.write_bytes(original)
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertFalse(doomed.exists())
        self.restored(saved, expected)

    def test_recursive_cleanup_resumes_after_git_directory_removed(self):
        expected = self.dirty()
        tree = self.retained()
        rmdir = os.rmdir

        def interrupted(name, *args, **kwargs):
            result = rmdir(name, *args, **kwargs)
            if name == ".git":
                raise Crash()
            return result

        with patch.object(maintenance.os, "rmdir", interrupted), self.assertRaises(Crash):
            self.tick()
        doomed = tree.with_name(tree.name + ".deleting")
        self.assertTrue(doomed.exists())
        self.assertFalse((doomed / ".git").exists())
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertFalse(doomed.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_crash_at_deletion_rename_rechecks_source(self):
        expected = self.dirty()
        tree = self.retained()
        rename = os.rename

        def interrupted(source, dest):
            if str(dest).endswith(".deleting"):
                raise Crash()
            return rename(source, dest)

        with patch.object(maintenance.os, "rename", interrupted), self.assertRaises(Crash):
            self.tick()
        self.assertTrue(tree.exists())
        job = maintenance._read_job(maintenance._job_path(self.cfg.state, tree))
        self.assertEqual(job["phase"], "delete")
        self.restored(self.cfg.state / "recovery" / job["record"], expected)
        # If a late write occurred before rename, the deletion receipt cannot authorize it.
        (tree / "Draft.lean").write_text("late source after journal\n")
        late = snapshot_tree(tree)
        late_fingerprint = wr.inspect(tree).fingerprint
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertTrue(tree.exists())
        self.tick()
        records = [p.parent for p in (self.cfg.state / "recovery").glob("*/manifest.json")]
        matching = [p for p in records if wr._read_manifest(p)["fingerprint"] == late_fingerprint]
        self.assertTrue(matching)
        self.restored(matching[-1], late)

    def test_resumed_delete_rechecks_unarchived_metadata(self):
        self.dirty()
        tree = self.retained()
        rename = os.rename

        def interrupted(source, dest):
            if str(dest).endswith(".deleting"):
                raise Crash()
            return rename(source, dest)

        with patch.object(maintenance.os, "rename", interrupted), self.assertRaises(Crash):
            self.tick()
        (tree / ".env").write_text("credential added after crash")
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertEqual((tree / ".env").read_text(), "credential added after crash")
        job = maintenance._read_job(maintenance._job_path(self.cfg.state, tree))
        self.assertEqual(job["next_action"], "retain-unarchived-metadata")

    def test_status_failure_leaves_retryable_finalization(self):
        expected = self.dirty()
        tree = self.retained()
        with patch.object(wr, "_set_status", return_value=False):
            self.tick()
        job_path = maintenance._job_path(self.cfg.state, tree)
        self.assertTrue(job_path.exists())
        job = maintenance._read_job(job_path)
        saved = self.cfg.state / "recovery" / job["record"]
        self.restored(saved, expected)
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertFalse(job_path.exists())
        self.assertEqual(wr.read_json(saved / "status.json")["state"], "checkout-reset")

    def test_crash_after_last_rmdir_retries_journal_finalization(self):
        expected = self.dirty()
        tree = self.retained()
        remove = maintenance._remove

        def crash_after_remove(path, job, work):
            done = remove(path, job, work)
            if done and str(path).endswith(".deleting"):
                raise Crash()
            return done

        with patch.object(maintenance, "_remove", crash_after_remove), self.assertRaises(Crash):
            self.tick()
        job_path = maintenance._job_path(self.cfg.state, tree)
        job = maintenance._read_job(job_path)
        self.assertTrue(job)
        saved = self.cfg.state / "recovery" / job["record"]
        self.restored(saved, expected)
        payload = saved / "untracked.tar"
        exact = payload.read_bytes()
        payload.unlink()
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertTrue(job_path.exists())
        payload.write_bytes(exact)
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertFalse(job_path.exists())
        self.assertFalse(tree.with_name(tree.name + ".owner.json").exists())

    def test_historical_inventory_is_incremental_without_hashing(self):
        self.dirty()
        saved = wr.guard(self.cfg, self.co).record
        for n in range(40):
            shutil.copytree(saved, self.cfg.state / "recovery" / f"record-{n}")
        with patch.object(wr, "_digest", side_effect=AssertionError("historical hash")):
            for _ in range(100):
                measured = self.tick(entries=12)
                self.assertLessEqual(measured["metadata_entries"], 12)
                if wr.summary(self.cfg.state.parent)["records"] == 41:
                    break
            else:
                self.fail("inventory did not resume to completion")

    def test_unknown_live_owners_and_mounts_preserved(self):
        self.dirty()
        tree = Path(wr.retain(self.cfg, self.co, "fixture").details["retained"])
        self.tick()
        self.assertTrue(tree.exists())
        sidecar = tree.with_name(tree.name + ".owner.json")
        sidecar.unlink()
        self.tick()
        self.assertTrue(tree.exists())
        wr.atomic_json(sidecar, dict(round_id="legacy-without-process-birth"))
        self.tick()
        self.assertTrue(tree.exists())
        allow_cleanup(self.cfg, self.co)
        sidecar = tree.with_name(tree.name + ".owner.json")
        owner = wr.read_json(sidecar)
        from tauceti_worker.usage_lifecycle import identity

        alive = dict(owner, process=identity())
        wr.atomic_json(sidecar, alive)
        self.tick()
        self.assertTrue(tree.exists())
        wr.atomic_json(sidecar, owner)
        with patch.object(wr, "live_users", return_value=["fixture live lease"]):
            self.tick()
        self.assertTrue(tree.exists())
        real_read = Path.read_text

        def mounted(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                return f"1 0 0:1 / {tree}/.lake rw - tmpfs tmpfs rw\n"
            return real_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", mounted):
            self.tick()
        self.assertTrue(tree.exists())

        def unavailable(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                raise FileNotFoundError("fixture unsupported mount inspection")
            return real_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", unavailable):
            self.tick()
        self.assertTrue(tree.exists())
        self.tick()
        self.assertFalse(tree.exists())

    def test_symlink_and_tracked_cache_source_survive(self):
        tracked = self.co / "node_modules"
        tracked.mkdir()
        (tracked / "source").write_text("tracked cache-named source")
        git(self.co, "add", "node_modules/source")
        git(self.co, "commit", "-qm", "tracked cache-named source")
        expected = self.dirty()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("untouched")
        (self.co / ".lake").symlink_to(outside, target_is_directory=True)
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())
        self.assertEqual((outside / "keep").read_text(), "untouched")
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_terminal_repair_cache_cleanup_is_deferred_and_budgeted(self):
        self.dirty()
        record = wr.guard(self.cfg, self.co).record
        checkout = record / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(checkout)], check=True)
        cache = checkout / ".lake"
        cache.mkdir()
        with (cache / "large").open("wb") as out:
            out.truncate(1024 * 1024)
        maintenance.defer_caches(self.cfg.state, record)
        registry_path = next((self.cfg.state / "recovery-cleanup" / "cache-roots").glob("*.json"))
        self.tick()
        self.assertTrue(cache.exists())  # unknown/live current owner never permits deletion
        registry = wr.read_json(registry_path)
        dead = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json; from tauceti_worker.usage_lifecycle import identity; print(json.dumps(identity()))",
                ],
                text=True,
            )
        )
        registry["owner"] = dict(process=dead, round_id="fixture")
        wr.atomic_json(registry_path, registry)
        for _ in range(30):
            self.tick(bytes=192 * 1024)
            if not cache.exists():
                break
        self.assertFalse(cache.exists())
        self.assertTrue((checkout / "A.lean").exists())
        self.assertIsNotNone(wr._complete(record))

    def test_cache_generation_change_invalidates_interrupted_removal(self):
        self.dirty()
        record = wr.guard(self.cfg, self.co).record
        checkout = record / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(checkout)], check=True)
        cache = checkout / ".lake"
        cache.mkdir()
        (cache / "large").write_bytes(b"x" * 1024 * 1024)
        maintenance.defer_caches(self.cfg.state, record)
        registry_path = next((self.cfg.state / "recovery-cleanup" / "cache-roots").glob("*.json"))
        owner_tree = self.retained()
        owner = wr.read_json(owner_tree.with_name(owner_tree.name + ".owner.json"))
        registry = wr.read_json(registry_path)
        registry["owner"] = owner
        wr.atomic_json(registry_path, registry)
        self.tick(bytes=192 * 1024)
        self.assertTrue(cache.exists())
        wr.atomic_json(record / "cache-use.json", dict(generation="actual-new-use"))
        # The consumer has begun a new use: the previous registry cannot delete its cache.
        (cache / "new-source").write_bytes(b"new invocation cache")
        self.tick()
        self.assertTrue((cache / "new-source").exists())
        maintenance.defer_caches(self.cfg.state, record)
        registry = wr.read_json(registry_path)
        registry["owner"] = owner
        wr.atomic_json(registry_path, registry)
        self.tick()
        self.assertFalse(cache.exists())

    def test_credentials_and_native_git_state_keep_original_tree(self):
        self.dirty()
        (self.co / ".env").write_text("fixture credential")
        tree = self.retained()
        cache = tree / ".lake"
        cache.mkdir()
        (cache / "generated").write_bytes(b"rebuildable")
        self.tick()
        self.assertTrue(tree.exists())
        self.assertEqual((tree / ".env").read_text(), "fixture credential")
        self.assertFalse(cache.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.assertIsNotNone(wr._complete(saved))
        (tree / ".env").unlink()
        (tree / ".git" / "MERGE_HEAD").write_text(git(tree, "rev-parse", "HEAD"))
        self.tick()
        self.assertTrue((tree / ".git" / "MERGE_HEAD").exists())

    def test_tag_and_reflog_only_source_are_bundled_before_tree_removal(self):
        self.dirty()
        git(self.co, "add", "Draft.lean")
        git(self.co, "commit", "-qm", "unpublished tagged source")
        lost = git(self.co, "rev-parse", "HEAD").strip()
        git(self.co, "tag", "local-tag")
        git(self.co, "reset", "--hard", "HEAD~1")
        expected = snapshot_tree(self.co)
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)
        clone = reconstruct(self.origin, saved, self.root, "history-restored")
        self.assertIn("exact saved source", git(clone, "show", f"{lost}:Draft.lean"))
        self.assertIn(f"history/{lost}", wr._read_manifest(saved)["bundle_refs"])

    def test_force_pushed_remote_and_amended_source_history_reconstruct(self):
        seed = self.root / "seed"
        git(seed, "checkout", "-qb", "pr")
        (seed / "A.lean").write_text("old published source")
        git(seed, "commit", "-qam", "old PR")
        remote_old = git(seed, "rev-parse", "HEAD").strip()
        git(seed, "push", str(self.origin), "pr")
        git(self.co, "fetch", "origin")
        git(seed, "reset", "--hard", "main")
        (seed / "A.lean").write_text("replacement published source")
        git(seed, "commit", "-qam", "rewritten PR")
        git(seed, "push", "--force", str(self.origin), "pr")
        git(self.co, "fetch", "origin")
        (self.co / "A.lean").write_text("amended-away local source")
        git(self.co, "commit", "-qam", "local original")
        local_old = git(self.co, "rev-parse", "HEAD").strip()
        (self.co / "A.lean").write_text("current local source")
        git(self.co, "commit", "-qam", "amend", "--amend")
        expected = self.dirty()
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)
        clone = reconstruct(self.origin, saved, self.root, "orphan-history-restored")
        self.assertEqual(git(clone, "show", f"{remote_old}:A.lean"), "old published source")
        self.assertEqual(git(clone, "show", f"{local_old}:A.lean"), "amended-away local source")

    def test_upstream_release_tags_do_not_park_recoverable_tree(self):
        git(self.root / "seed", "tag", "-am", "release", "v1-fixture")
        git(self.root / "seed", "push", str(self.origin), "v1-fixture")
        git(self.co, "fetch", "origin")
        self.assertIn("v1-fixture", git(self.co, "tag"))
        expected = self.dirty()
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_cache_hardlink_never_truncates_external_source(self):
        self.dirty()
        cache = self.co / ".lake"
        cache.mkdir()
        source = self.root / "external-source"
        with source.open("wb") as out:
            out.truncate(1024 * 1024)
        os.link(source, cache / "linked")
        tree = self.retained()
        for _ in range(20):
            self.tick(bytes=192 * 1024)
            if not tree.exists():
                break
        self.assertFalse(tree.exists())
        self.assertEqual(source.stat().st_size, 1024 * 1024)

    def test_state_write_failure_still_retains_without_destructive_cleanup(self):
        expected = self.dirty()
        with (
            patch.object(maintenance, "register", side_effect=OSError(errno.ENOSPC, "fixture")),
            patch.object(wr, "atomic_json", side_effect=OSError(errno.ENOSPC, "fixture")),
        ):
            decision = wr.retain(self.cfg, self.co, "fixture no space")
        self.assertTrue(decision.ok)
        tree = Path(decision.details["retained"])
        self.assertEqual(snapshot_tree(tree), expected)
        self.tick()
        self.assertTrue(tree.exists())

    def test_git_output_budget_is_enforced_before_capture(self):
        work = wr.Work(seconds=1, entries=100, bytes=8)
        token = wr._WORK.set(work)
        try:
            with self.assertRaises(wr.BudgetExhausted):
                wr._git(self.co, "show", "HEAD:A.lean")
        finally:
            wr._WORK.reset(token)
        self.assertLessEqual(work.read_bytes, 8)

    def test_production_budgets_eventually_preserve_twenty_mib(self):
        self.dirty()
        (self.co / "Large.source").write_bytes(b"source!" * (3 * 1024 * 1024))
        expected = snapshot_tree(self.co)
        tree = self.retained()
        now = wr.time.time()
        promoted = False
        for tick in range(20):
            with patch.object(maintenance.time, "time", return_value=now + tick * 3600):
                report = wr.housekeep(self.cfg.state.parent)  # actual production default policy
            promoted |= report["limits"]["promoted"]
            self.assertLessEqual(report["metadata_entries"], report["limits"]["entries"])
            self.assertLessEqual(report["read_bytes"], report["limits"]["read_bytes"])
            self.assertLessEqual(report["removed_bytes"], report["limits"]["removal_bytes"])
            self.assertLessEqual(report["limits"]["seconds"], 31)
            if not tree.exists() and not tree.with_name(tree.name + ".deleting").exists():
                break
        else:
            self.fail("production preservation allowance did not make progress")
        self.assertTrue(promoted)
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_invalidated_large_payload_validation_gets_a_bounded_allowance(self):
        self.dirty()
        (self.co / "Large.source").write_bytes(b"x" * (20 * 1024 * 1024))
        saved = wr.guard(self.cfg, self.co).record
        tree = self.retained()
        now = time.time()
        for tick in range(20):
            with (
                patch.object(maintenance.time, "time", return_value=now + tick * 3600),
                patch.object(maintenance, "_remove", side_effect=OSError(errno.EIO, "fixture stop after rename")),
            ):
                wr.housekeep(self.cfg.state.parent)
            if tree.with_name(tree.name + ".deleting").exists():
                break
        else:
            self.fail("large source did not reach the deletion journal")
        (saved / "untracked.tar").touch()  # same exact data, invalidated verification metadata
        promoted = False
        for tick in range(20, 40):
            with patch.object(maintenance.time, "time", return_value=now + tick * 3600):
                report = wr.housekeep(self.cfg.state.parent)
            promoted |= report["limits"]["promoted"]
            if not tree.with_name(tree.name + ".deleting").exists():
                break
        else:
            self.fail("invalidated large descriptor validation did not resume")
        self.assertTrue(promoted)
        self.assertIsNotNone(wr._complete(saved))

    def test_mode_zero_overlay_directories_are_reclaimed(self):
        self.dirty()
        locked = self.co / ".lake" / "tauceti-overlay-work" / "work"
        locked.mkdir(parents=True)
        (locked / "opaque").write_bytes(b"cache")
        locked.chmod(0)
        tree = self.retained()
        self.tick()
        self.assertFalse(tree.exists())

    def test_retention_with_real_stopped_owner_without_namespace_support(self):
        self.dirty()
        program = """import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tauceti_worker import work_recovery
cfg = SimpleNamespace(state=Path(sys.argv[1]), wid='w')
# Namespace APIs are unavailable; ordinary process attribution still works.
with patch('os.readlink', side_effect=OSError('PID namespaces unavailable')):
    assert work_recovery.note_owner(cfg, round_id='real-fixture-round')
"""
        subprocess.run([sys.executable, "-c", program, str(self.cfg.state)], check=True)
        tree = Path(wr.retain(self.cfg, self.co, "fixture").details["retained"])
        real_link = os.readlink

        def without_namespaces(path, *args, **kwargs):
            if str(path).endswith("/ns/pid"):
                raise OSError("PID namespaces unavailable")
            return real_link(path, *args, **kwargs)

        with patch.object(os, "readlink", without_namespaces):
            self.tick()
        self.assertFalse(tree.exists())

    def test_prior_boot_owner_can_be_cleaned(self):
        self.dirty()
        tree = self.retained()
        sidecar = tree.with_name(tree.name + ".owner.json")
        owner = wr.read_json(sidecar)
        owner["process"]["boot"] = "00000000-0000-0000-0000-000000000000"
        owner["process"]["session"] = os.getsid(0)  # same SID in a different boot is unrelated
        wr.atomic_json(sidecar, owner)
        self.tick()
        self.assertFalse(tree.exists())

    def test_cache_registry_is_idempotent_until_an_actual_use(self):
        self.dirty()
        record = wr.guard(self.cfg, self.co).record
        maintenance.defer_caches(self.cfg.state, record)
        path = next((self.cfg.state / "recovery-cleanup" / "cache-roots").glob("*.json"))
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        maintenance.defer_caches(self.cfg.state, record)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        wr.atomic_json(record / "cache-use.json", dict(generation="next actual invocation"))
        maintenance.defer_caches(self.cfg.state, record)
        self.assertNotEqual(path.read_bytes(), before)
        self.assertEqual(wr.read_json(path)["created_at"], json.loads(before)["created_at"])

    def test_one_failed_worker_does_not_starve_other_workers(self):
        self.dirty()
        wr.guard(self.cfg, self.co)
        _, co, cfg = fixture(self.root / "second")
        cfg.state = self.cfg.state.parent / "z"
        cfg.wid = "z"
        (co / "Draft.lean").write_text("other worker")
        wr.guard(cfg, co)
        remember = index.remember

        def failed(state, *args, **kwargs):
            if state == self.cfg.state:
                raise OSError(errno.EIO, "one fixture worker")
            return remember(state, *args, **kwargs)

        with patch.object(index, "remember", failed):
            self.tick()
        self.assertEqual(wr.summary(self.cfg.state.parent, workers={"z"})["records"], 1)

    def test_cleanup_never_acquires_worker_round_lock(self):
        import fcntl

        expected = self.dirty()
        tree = self.retained()
        # An unrelated new round can hold its admission lock throughout old-tree cleanup.
        with (self.cfg.state / "round.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_stale_malformed_containment_markers_are_harmless(self):
        expected = self.dirty()
        tree = self.retained()
        (self.cfg.state / "quiescence.json").write_text("{broken")
        markers = self.root / "activity" / "containment-unconfirmed"
        markers.mkdir(parents=True)
        (markers / "stale.json").write_text('{"unconfirmed":true}')
        (markers / "broken.json").write_text("[")
        before = {p: p.read_bytes() for p in markers.iterdir()}
        from tauceti_worker.survey import Counters

        counters = Counters(self.cfg)
        counters.write("rebase-pr-42", 2)
        usage = self.cfg.state / "usage.jsonl"
        usage.write_text('{"total_cost_usd":7,"final":true}\n')
        usage_before = usage.read_bytes()
        self.tick()
        self.assertFalse(tree.exists())
        self.assertEqual({p: p.read_bytes() for p in markers.iterdir()}, before)
        self.assertEqual(counters.read("rebase-pr-42"), 2)
        self.assertEqual(usage.read_bytes(), usage_before)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.co)], check=True)
        (self.co / "Next.lean").write_text("next source")
        self.assertTrue(wr.guard(self.cfg, self.co).ok)
        records = [p.parent for p in (self.cfg.state / "recovery").glob("*/manifest.json")]
        self.restored(next(p for p in records if wr._read_manifest(p)["checkout"] == str(tree)), expected)

    def test_uncertain_candidate_does_not_reserve_other_work(self):
        self.dirty()
        uncertain = Path(wr.retain(self.cfg, self.co, "unknown owner").details["retained"])
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.co)], check=True)
        expected = self.dirty()
        candidate = self.retained()
        # Restore uncertainty only to the first tree after giving the second a stopped owner.
        wr.atomic_json(uncertain.with_name(uncertain.name + ".owner.json"), {"process": "malformed"})
        self.tick()
        self.assertTrue(uncertain.exists())
        self.assertFalse(candidate.exists())
        self.assertFalse((self.cfg.state / "round.lock").exists())
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.co)], check=True)
        self.assertTrue(wr.guard(self.cfg, self.co).ok)
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_live_recovery_lock_withholds_only_its_cache(self):
        import fcntl

        self.dirty()
        record = wr.guard(self.cfg, self.co).record
        checkout = record / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(checkout)], check=True)
        (checkout / ".lake").mkdir()
        (checkout / ".lake" / "cache").write_bytes(b"cache")
        maintenance.defer_caches(self.cfg.state, record)
        path = next((self.cfg.state / "recovery-cleanup" / "cache-roots").glob("*.json"))
        registry = wr.read_json(path)
        tree = self.retained()
        registry["owner"] = wr.read_json(tree.with_name(tree.name + ".owner.json"))
        wr.atomic_json(path, registry)
        with (record / "recovery.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.tick()
        self.assertTrue((checkout / ".lake").exists())
        self.assertFalse(tree.exists())
        self.tick()
        self.assertFalse((checkout / ".lake").exists())

    def test_rollback_keeps_clean_unpublished_and_ignored_source(self):
        (self.co / "A.lean").write_text("local-only committed source")
        git(self.co, "commit", "-qam", "unpublished fixture")
        head = git(self.co, "rev-parse", "HEAD").strip()
        (self.co / ".git" / "info" / "exclude").write_text("Ignored.draft\n")
        (self.co / "Ignored.draft").write_text("ignored unpublished draft")
        with (
            patch.dict(os.environ, TAUCETI_WORK_RECOVERY="off"),
            patch.object(index, "lookup", side_effect=AssertionError("rollback index reliance")),
        ):
            decision = wr.guard(self.cfg, self.co)
            self.assertTrue(self.tick()["disabled"])
        self.assertTrue(decision.ok)
        self.assertIsNotNone(decision.record)
        self.restored(decision.record, snapshot_tree(self.co))
        self.assertEqual(git(self.co, "rev-parse", "HEAD").strip(), head)
        self.assertEqual((self.co / "Ignored.draft").read_text(), "ignored unpublished draft")
        self.assertFalse((self.cfg.state / "recovery-index").exists())

    def test_rollback_reuses_cache_across_round_end_and_preparation(self):
        import tauceti_worker as tc

        self.cfg.state.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.co)  # only the disposable fixture checkout
        run = subprocess.run
        clones = []

        def measured(command, *args, **kwargs):
            if command[:2] == ["git", "clone"]:
                clones.append(command)
            return run(command, *args, **kwargs)

        with (
            patch.dict(os.environ, TAUCETI_WORK_RECOVERY="off"),
            patch.object(index, "lookup", side_effect=AssertionError("rollback index reliance")),
            patch.object(index, "remember", side_effect=AssertionError("rollback index publication")),
            patch.object(tc.agents, "sync_mathlib_pool"),
            patch.object(tc.agents, "clean_lake_cache_after_toolchain_bump"),
            patch.object(subprocess, "run", measured),
        ):
            self.assertTrue(tc.prepare_checkout(self.cfg))
            cache = self.co / ".lake" / "large-cache"
            cache.parent.mkdir()
            with cache.open("wb") as handle:
                handle.truncate(1024**3)
            inode = cache.stat().st_ino
            for _ in range(3):
                self.assertTrue(wr.guard(self.cfg, self.co).ok)  # ordinary round-end guard
                self.assertTrue(tc.prepare_checkout(self.cfg))
                self.assertEqual(cache.stat().st_ino, inode)
            (self.co / "Draft.lean").write_text("unfinished round")
            saved = wr.guard(self.cfg, self.co).record
            self.assertIsNotNone(saved)
            self.assertTrue(tc.prepare_checkout(self.cfg))
            self.assertEqual(cache.stat().st_ino, inode)
        self.assertEqual(len(clones), 1)
        self.assertFalse(list((self.co.parent / wr.RETAINED_DIR).glob("*")))
        self.assertFalse((self.cfg.state / "recovery-index").exists())

    def test_commit_receipt_shortcut_rechecks_late_source_write(self):
        git(self.co, "checkout", "-qb", "unfinished")
        (self.co / "A.lean").write_text("local-only source")
        git(self.co, "commit", "-qam", "unfinished")
        original = wr.guard(self.cfg, self.co).record
        git(self.co, "checkout", "-qB", "main", "origin/main")
        calls = 0

        def late_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                (self.co / "Late.lean").write_text("source written after first inspect")
            return []

        with patch.object(wr, "live_users", late_write):
            decision = wr.guard(self.cfg, self.co)
        self.assertNotEqual(decision.record, original)
        self.restored(decision.record, snapshot_tree(self.co))

    def test_commit_index_disappearance_cannot_crash_preparation(self):
        (self.co / "A.lean").write_text("local-only source")
        git(self.co, "commit", "-qam", "unfinished")
        saved = wr.guard(self.cfg, self.co).record
        with patch.object(index, "stamps", side_effect=OSError("interrupted descriptor replacement")):
            wr._remember_commits(self.cfg.state, saved, wr._read_manifest(saved), wr.inspect(self.co))
        wr._remember_commits(self.cfg.state, saved, None, wr.inspect(self.co))

    def test_invalid_git_directory_cannot_discover_enclosing_repository(self):
        self.dirty()
        git(self.root, "init", "-q")
        (self.root / "Outer.txt").write_text("outer repository source")
        git(self.root, "add", "Outer.txt")
        git(self.root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "outer")
        shutil.rmtree(self.co / ".git")
        (self.co / ".git").mkdir()  # a killed clone can leave an invalid directory
        tree = self.retained()
        refs = git(self.root, "for-each-ref")
        with patch.object(maintenance, "_safe", side_effect=AssertionError("invalid Git ran process scan")):
            self.tick()
            self.tick()
        self.assertTrue((tree / "Draft.lean").exists())
        self.assertEqual(git(self.root, "for-each-ref"), refs)
        self.assertFalse((self.cfg.state / "recovery").exists())
        with self.assertRaises(wr.RecordError):
            wr._git(tree, "rev-parse", "--show-toplevel")
        wr._update_refs(tree, ["update refs/tauceti/test HEAD"], check=False)
        self.assertEqual(git(self.root, "for-each-ref"), refs)

    def test_rollback_clean_leftover_branch_reuses_one_verified_bundle(self):
        git(self.co, "checkout", "-qb", "unfinished")
        (self.co / "A.lean").write_text("local-only source")
        git(self.co, "commit", "-qam", "unfinished")
        head = git(self.co, "rev-parse", "HEAD").strip()
        with (
            patch.dict(os.environ, TAUCETI_WORK_RECOVERY="off"),
            patch.object(index, "lookup", side_effect=AssertionError("rollback index reliance")),
            patch.object(index, "remember", side_effect=AssertionError("rollback index publication")),
        ):
            saved = wr.guard(self.cfg, self.co).record
            git(self.co, "checkout", "-qB", "main", "origin/main")
            for _ in range(4):
                self.assertEqual(wr.guard(self.cfg, self.co).record, saved)
            self.assertEqual(len(list((self.cfg.state / "recovery").glob("*/commits.bundle"))), 1)
            (saved / "commits.bundle").write_bytes(b"corrupt")
            replacement = wr.guard(self.cfg, self.co).record
            self.assertNotEqual(saved, replacement)
            for _ in range(4):
                self.assertEqual(wr.guard(self.cfg, self.co).record, replacement)
            self.assertEqual(len(list((self.cfg.state / "recovery").glob("*/commits.bundle"))), 2)
        clone = reconstruct(self.origin, replacement, self.root, "rollback-history")
        self.assertEqual(git(clone, "show", f"{head}:A.lean"), "local-only source")

    def test_rollback_rebundles_when_old_prerequisite_is_no_longer_published(self):
        seed = self.root / "seed"
        git(seed, "checkout", "-qb", "pr")
        (seed / "A.lean").write_text("old upstream prerequisite")
        git(seed, "commit", "-qam", "old base")
        base = git(seed, "rev-parse", "HEAD").strip()
        git(seed, "push", str(self.origin), "pr")
        git(self.co, "fetch", "origin")
        git(self.co, "checkout", "-qb", "unfinished", "origin/pr")
        (self.co / "Draft.lean").write_text("unfinished source above base")
        git(self.co, "add", "Draft.lean")
        git(self.co, "commit", "-qm", "unfinished")
        head = git(self.co, "rev-parse", "HEAD").strip()
        with patch.dict(os.environ, TAUCETI_WORK_RECOVERY="off"):
            first = wr.guard(self.cfg, self.co).record
            self.assertIn(base, wr._bundle_prerequisites(first / "commits.bundle"))
            git(seed, "reset", "--hard", "main")
            (seed / "A.lean").write_text("replacement remote base")
            git(seed, "commit", "-qam", "rewritten base")
            git(seed, "push", "--force", str(self.origin), "pr")
            git(self.co, "fetch", "origin")
            second = wr.guard(self.cfg, self.co).record
            self.assertNotEqual(first, second)
            self.assertNotIn(base, wr._bundle_prerequisites(second / "commits.bundle"))
            self.assertEqual(wr.guard(self.cfg, self.co).record, second)
        clone = reconstruct(self.origin, second, self.root, "prerequisite-restored")
        self.assertEqual(git(clone, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(git(clone, "show", f"{base}:A.lean"), "old upstream prerequisite")

    def test_excessive_history_parks_without_publishing_oversized_records(self):
        self.dirty()
        git(self.co, "add", "Draft.lean")
        git(self.co, "commit", "-qm", "orphan history")
        orphan = git(self.co, "rev-parse", "HEAD").strip()
        git(self.co, "reset", "--hard", "HEAD~1")
        tree = self.retained()
        with patch.object(wr, "MAX_HISTORY_TIPS", 0):
            self.tick()
        self.assertTrue(tree.exists())
        job = maintenance._read_job(maintenance._job_path(self.cfg.state, tree))
        self.assertIn("history exceeds", job["error"])
        self.assertTrue(job["parked"])
        self.assertFalse(list((self.cfg.state / "recovery").glob("*/manifest.json")))
        with patch.object(wr, "inspect", side_effect=AssertionError("parked oversized history")):
            self.tick()
        job["retry_at"] = 0
        maintenance._write_job(maintenance._job_path(self.cfg.state, tree), job)
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        clone = reconstruct(self.origin, saved, self.root, "bounded-history")
        self.assertIn("exact saved source", git(clone, "show", f"{orphan}:Draft.lean"))

    def test_recycled_session_leader_does_not_protect_old_tree(self):
        self.dirty()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd="/",
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: proc.wait(timeout=3))
        self.addCleanup(proc.terminate)
        tree = self.retained()
        birth = dict(pid=proc.pid, session=proc.pid, start="0", boot=wr.process_owner()["boot"])
        wr.atomic_json(tree.with_name(tree.name + ".owner.json"), dict(process=birth))
        self.tick()
        self.assertFalse(tree.exists())
        self.assertIsNone(proc.poll())  # unrelated replacement session remains healthy

    def test_pending_verification_reference_withholds_only_its_tree(self):
        expected = self.dirty()
        tree = self.retained()
        pending = self.cfg.state / "verification-pending" / "fixture.json"
        wr.atomic_json(pending, dict(root=str(tree)))
        self.tick()
        self.assertTrue(tree.exists())
        self.assertEqual(
            maintenance._read_job(maintenance._job_path(self.cfg.state, tree))["next_action"],
            "await-verification-reference",
        )
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.co)], check=True)
        self.assertTrue(wr.guard(self.cfg, self.co).ok)
        pending.unlink()
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_pending_reference_to_original_path_protects_resumed_deletion(self):
        self.dirty()
        tree = self.retained()
        rename = os.rename

        def crash_after_rename(source, dest):
            rename(source, dest)
            if str(dest).endswith(".deleting"):
                raise Crash()

        with patch.object(maintenance.os, "rename", crash_after_rename), self.assertRaises(Crash):
            self.tick()
        doomed = tree.with_name(tree.name + ".deleting")
        pending = self.cfg.state / "verification-pending" / "fixture.json"
        wr.atomic_json(pending, dict(root=str(tree)))
        maintenance._SWEEPS.clear()
        self.tick()
        self.assertTrue((doomed / "Draft.lean").exists())
        pending.unlink()
        self.tick()
        self.assertFalse(doomed.exists())

    def test_interrupted_verification_temp_files_do_not_block_cleanup(self):
        expected = self.dirty()
        tree = self.retained()
        pending = self.cfg.state / "verification-pending"
        pending.mkdir()
        for name in (".verification-interrupted", ".job.json.abcd", "unpublished.tmp"):
            (pending / name).write_text("{partial")
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_aliased_root_registrations_share_one_deletion_cursor(self):
        expected = self.dirty()
        tree = self.retained()
        alias = self.root / "checkout-alias"
        alias.symlink_to(tree.parent, target_is_directory=True)
        # register canonicalizes new aliases; fixtures also emulate an older root file.
        maintenance.register(self.cfg.state, alias, "TauCeti", "fixture")
        registry = self.cfg.state / "recovery-cleanup" / "roots"
        self.assertEqual(len(list(registry.glob("*.json"))), 1)
        wr.atomic_json(
            registry / "old-alias.json", dict(version=1, parent=str(alias), label="TauCeti", repository="fixture")
        )
        # A symlink at the root itself is rejected. An aliased ancestor is canonicalized.
        alias_parent = self.root / "aliased-parent"
        alias_parent.symlink_to(tree.parent.parent, target_is_directory=True)
        wr.atomic_json(
            registry / "old-parent-alias.json",
            dict(version=1, parent=str(alias_parent / tree.parent.name), label="TauCeti", repository="fixture"),
        )
        seen = []

        def hold(state, candidate, root, work, observation):
            seen.append(str(candidate))
            observation.update(next_action="fixture-budget", created_at=time.time())

        with patch.object(maintenance, "_cleanup", hold):
            self.tick()
        self.assertEqual(seen, [str(tree.resolve())])
        self.tick()
        self.assertFalse(tree.exists())
        saved = next((self.cfg.state / "recovery").glob("*/manifest.json")).parent
        self.restored(saved, expected)

    def test_prepared_summary_and_rollback_do_no_payload_analysis(self):
        self.dirty()
        tree = Path(wr.retain(self.cfg, self.co, "fixture").details["retained"])
        self.tick()
        with patch.object(wr, "_complete", side_effect=AssertionError("analysis in summary")):
            row = wr.summary(self.cfg.state.parent)
        self.assertEqual(row["cleanup_backlog"], 1)
        self.assertGreaterEqual(row["cleanup_oldest_age_seconds"], 0)
        allow_cleanup(self.cfg, self.co)
        with patch.dict(os.environ, TAUCETI_WORK_RECOVERY="off"):
            self.assertTrue(self.tick()["disabled"])
        self.assertTrue(tree.exists())
        self.assertEqual(self.tick(seconds=0)["metadata_entries"], 0)


if __name__ == "__main__":
    unittest.main()

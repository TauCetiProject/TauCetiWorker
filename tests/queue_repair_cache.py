#!/usr/bin/env python3
"""Idle retention preserves future cache hits and excludes live or ambiguous owners."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import repair_cache as cache
from tauceti_queue import repair_sandbox

# Fork pull requests run on hosted runners without bubblewrap. There the fetch views copy back
# unsandboxed, so these tests still cover the views; the fleet runners also cover the sandboxed copy.
FETCH_ENV = {} if shutil.which("bwrap") else {repair_sandbox.DISABLE_VARIABLE: "0"}


class BatchPools(unittest.TestCase):
    """Decision 202 a: every batch builds from a pool of its own, which collection removes once it is finished."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.shared = base / "shared"
        self.worker = base / "repair"
        self.database = base / "queue.db"
        self.store = sqlite3.connect(self.database)
        self.addCleanup(self.store.close)
        self.store.execute("CREATE TABLE claims (task_id INTEGER, released_at REAL, expires_at REAL)")
        self.store.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY, state TEXT)")
        self.store.commit()
        self.environ = patch.dict(os.environ, {"TAUCETI_QUEUE_CACHE_ROOT": str(self.shared)})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def batch(self, batch_id, state):
        self.store.execute("INSERT OR REPLACE INTO batches VALUES (?, ?)", (batch_id, state))
        self.store.commit()

    def env(self, pool_root):
        from tauceti_queue import repair

        return repair.lake_env(self.worker, base_env={}, cache_root=pool_root)

    def test_batches_never_share_a_pool(self):
        first = cache.task_cache(self.worker, 1, 5)
        second = cache.task_cache(self.worker, 2, 6)
        again = cache.task_cache(self.worker, 3, 5)
        self.assertNotEqual(first, second)
        self.assertEqual(first, again)
        self.assertEqual(cache.pool_batch(first), "5")
        self.assertEqual(cache.pool_batch(second), "6")
        self.assertNotEqual(self.env(first)["LAKE_CACHE_DIR"], self.env(second)["LAKE_CACHE_DIR"])
        # The Mathlib cache is written by sandboxed fetches too, so it is the batch's as well.
        self.assertTrue(self.env(first)["MATHLIB_CACHE_DIR"].startswith(str(first)))
        # The binding is fixed: a later lookup without the batch returns it.
        self.assertEqual(cache.task_cache(self.worker, 1), first)

    def test_a_new_binding_needs_a_batch(self):
        with self.assertRaisesRegex(ValueError, "no batch"):
            cache.task_cache(self.worker, 1)
        with self.assertRaisesRegex(ValueError, "batch ID"):
            cache.task_cache(self.worker, 1, "b1")

    def test_without_a_configured_root_the_batch_pool_is_under_the_worker(self):
        with patch.dict(os.environ, {"TAUCETI_QUEUE_CACHE_ROOT": ""}):
            self.assertEqual(cache.task_cache(self.worker, 1, 5), self.worker.absolute() / "batches/5")

    def test_another_batchs_marker_is_refused(self):
        root = cache.batch_pool(self.shared, 5)
        root.mkdir(parents=True)
        (root / cache.BATCH_MARKER).write_text(json.dumps({"batch_id": "6"}))
        with self.assertRaisesRegex(ValueError, "another batch"):
            cache.task_cache(self.worker, 1, 5)

    def test_seed_ledger_and_pool_identity(self):
        root = cache.task_cache(self.worker, 1, 5)
        env = self.env(root)
        self.assertEqual(cache.pool_identity(env), {"scope": "batch", "batch_id": "5", "seeds": []})
        cache.record_seed(env, "tauceti-repair", "a" * 40, packages="seed")
        cache.record_seed(env, "tauceti-repair", "b" * 40, packages="private")
        cache.record_seed(env, "tauceti-repair", "a" * 40, packages="seed")
        cache.record_seed(env, "mathlib", "", packages="private")
        cache.record_seed(env, "tauceti-repair", "not-a-revision", packages="seed")
        self.assertEqual(
            cache.pool_identity(env)["seeds"],
            [
                {"service": "tauceti-repair", "revision": "a" * 40, "packages": "seed"},
                {"service": "tauceti-repair", "revision": "b" * 40, "packages": "private"},
                {"service": "mathlib", "revision": "", "packages": "private"},
            ],
        )
        # The ledger is beside the pool, which the sandbox binds, not inside it.
        self.assertFalse((root / cache.SEEDS).is_relative_to(Path(env["LAKE_CACHE_DIR"])))
        shared = {"LAKE_CACHE_DIR": str(self.shared / "lake-cache")}
        self.assertEqual(cache.pool_identity(shared), {"scope": "shared"})
        cache.record_seed(shared, "tauceti-repair", "a" * 40, packages="seed")
        self.assertFalse((self.shared / cache.SEEDS).exists())
        self.assertEqual(cache.pool_identity({}), {"scope": "none"})

    def test_a_linked_ledger_is_not_followed(self):
        root = cache.task_cache(self.worker, 1, 5)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("")
        (root / cache.SEEDS).symlink_to(outside)
        with self.assertRaises(OSError):
            cache.record_seed(self.env(root), "tauceti-repair", "a" * 40, packages="seed")
        self.assertEqual(cache.pool_identity(self.env(root))["scope"], "unknown")

    def worktree(self, seeded):
        from tauceti_queue import repair, repair_sandbox

        tree = Path(self.temp.name) / ("seeded" if seeded else "private")
        (tree / ".lake").mkdir(parents=True)
        if seeded:
            seed = Path(self.temp.name) / "seed/packages"
            seed.mkdir(parents=True)
            with patch.dict(os.environ, {repair_sandbox.READ_ONLY_VARIABLE: str(seed.parent)}):
                repair.link_shared_packages(tree, seed)
        return tree

    def test_a_fetch_is_recorded_by_what_it_could_run_not_by_what_it_printed(self):
        from tauceti_queue import repair, repair_sandbox

        root = cache.task_cache(self.worker, 1, 5)
        env = self.env(root)
        public = {"service": repair.CACHE_SERVICE, "revision": "a" * 40, "mapping_found": True}
        missing = {
            "service": "tauceti-repair",
            "revision": "b" * 40,
            "mapping_found": False,
            "reason": "tauceti-repair: no published cache mapping for bbbbbbbbbbbb",
        }
        found = {"service": "tauceti-repair", "revision": "c" * 40, "mapping_found": True}
        failed = {"service": "tauceti-repair", "revision": "d" * 40, "mapping_found": False, "reason": "exited 124"}
        # With the read-only seed only Lake runs: a repair fetch that found nothing restored nothing.
        seeded = self.worktree(seeded=True)
        with patch.dict(os.environ, {repair_sandbox.READ_ONLY_VARIABLE: str(Path(self.temp.name) / "seed")}):
            for fetch in (public, missing, found, failed):
                repair._note_seed(env, fetch, seeded)
        self.assertEqual(
            [(s["revision"], s["packages"]) for s in cache.pool_identity(env)["seeds"]],
            [("c" * 40, "seed"), ("d" * 40, "seed")],
        )
        # With candidate-writable packages the fetch ran code candidate code may have written, which could
        # fetch anything and print "not found": every fetch is recorded, public ones too.
        private = self.worktree(seeded=False)
        for fetch in (public, missing):
            repair._note_seed(env, fetch, private)
        repair.note_mathlib_fetch(env, private)
        self.assertEqual(
            [(s["revision"], s["packages"]) for s in cache.pool_identity(env)["seeds"][2:]],
            [("a" * 40, "private"), ("b" * 40, "private"), ("", "private")],
        )

    def test_collection_removes_a_finished_batchs_pool_only(self):
        landed = cache.task_cache(self.worker, 1, 5)
        open_ = cache.task_cache(self.worker, 2, 6)
        unknown = cache.task_cache(self.worker, 3, 7)
        for root in (landed, open_, unknown):
            (root / "lake-cache/artifacts").mkdir(parents=True)
        self.batch(5, "landed")
        self.batch(6, "open")
        results = {r["root"]: r for r in cache.collect(self.database, [self.shared], apply=True)}
        self.assertEqual(results[str(landed)]["state"], "removed")
        self.assertEqual(sorted(p.name for p in landed.iterdir()), [cache.BATCH_MARKER, "cache-users", "cache.lock"])
        again = {r["root"]: r for r in cache.collect(self.database, [self.shared], apply=True)}
        self.assertEqual(again[str(landed)]["state"], "absent")
        self.assertEqual(results[str(open_)]["state"], "collected")
        self.assertTrue(open_.is_dir())
        self.assertEqual(results[str(unknown)]["state"], "unknown")
        self.assertTrue(unknown.is_dir())

    def test_a_bound_running_task_keeps_its_finished_batchs_pool(self):
        root = cache.task_cache(self.worker, 1, 5)
        (root / "lake-cache").mkdir()
        (self.worker / "worktrees/task-1").mkdir(parents=True)
        self.batch(5, "removed")
        result = next(r for r in cache.collect(self.database, [self.shared], apply=True) if r.get("batch_id") == 5)
        self.assertEqual(result["state"], "busy")
        self.assertTrue(root.is_dir())
        with cache.cache_lease(root):
            (self.worker / "worktrees/task-1").rmdir()
            result = next(r for r in cache.collect(self.database, [self.shared], apply=True) if r.get("batch_id") == 5)
            self.assertEqual(result["state"], "busy")
        result = next(r for r in cache.collect(self.database, [self.shared], apply=True) if r.get("batch_id") == 5)
        self.assertEqual(result["state"], "removed")
        self.assertFalse((root / "lake-cache").exists())
        self.assertEqual(cache.pool_batch(root), "5", "the marker stays with the lock")

    def test_the_verifier_drivers_build_without_any_pool(self):
        from tauceti_queue import repair_audit

        env = self.env(cache.task_cache(self.worker, 1, 5))
        clean = repair_audit.pool_free(env)
        self.assertNotIn("LAKE_CACHE_DIR", clean)
        self.assertNotIn("LAKE_RESTORE_ARTIFACTS", clean)
        self.assertEqual(clean["LAKE_ARTIFACT_CACHE"], "false")
        self.assertEqual(clean["LAKE_CONFIG"], env["LAKE_CONFIG"])


class Retention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repair"
        self.pool = self.root / "lake-cache"
        self.database = Path(self.temp.name) / "queue.db"
        self.store = sqlite3.connect(self.database)
        self.addCleanup(self.store.close)
        self.store.execute("CREATE TABLE claims (task_id INTEGER, released_at REAL, expires_at REAL)")
        self.now = time.time()

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def artifact(self, key, suffix="ltar", size=10):
        name = f"{key:016x}.{suffix}"
        return self.write(f"lake-cache/artifacts/{name}", "x" * size)

    def snapshot(self, revision, entries, age=0, scope="TauCeti"):
        path = self.write(
            f"lake-cache/revisions/{scope}/{revision:040x}.jsonl",
            "\n".join(map(json.dumps, ["2026-03-17", *entries])) + "\n",
        )
        os.utime(path, (self.now - age, self.now - age))
        return path

    def output(self, key, data, scope="TauCeti"):
        return self.write(f"lake-cache/outputs/{scope}/{key:016x}.json", json.dumps({"data": data}))

    def collect(self, apply=True, **kwargs):
        return cache.collect(self.database, [self.root], apply=apply, now=self.now, **kwargs)[0]

    def test_native_cache_hit_survives_and_obsolete_expanded_outputs_are_removed(self):
        archive = self.artifact(1)
        native = [
            self.artifact(2, suffix)
            for suffix in ("olean", "olean.server", "olean.private", "ilean", "ir", "ir.sig", "c", "bc")
        ]
        data = {
            "l": archive.name,
            "o": [p.name for p in native[:3]],
            "i": native[3].name,
            "r": native[4].name,
            "rs": native[5].name,
            "c": native[6].name,
            "b": native[7].name,
            "m": True,
        }
        index = self.output(1, data)
        self.snapshot(1, [[f"{1:016x}", archive.name]])
        old = self.artifact(3, "olean")
        stale = self.output(2, old.name)
        result = self.collect()
        self.assertEqual(result["state"], "collected")
        self.assertEqual(result["artifacts_removed"], 1)
        self.assertFalse(old.exists())
        self.assertFalse(stale.exists())
        for name in cache._artifacts(json.loads(index.read_text())["data"]):
            self.assertTrue((self.pool / "artifacts" / name).is_file(), "future native hit must have every facet")

    def test_only_two_recent_snapshots_per_scope_and_strict_budget(self):
        paths = [self.artifact(i) for i in range(1, 5)]
        maps = [self.snapshot(i, [[f"{i:016x}", p.name]], age=i) for i, p in enumerate(paths, 1)]
        self.assertEqual(self.collect(apply=False)["artifacts_removed"], 2)
        self.assertTrue(all(p.exists() for p in paths), "preview is read-only")
        result = self.collect(max_bytes=10)
        self.assertEqual(result["retained_bytes"], 10)
        self.assertTrue(paths[0].exists())
        self.assertFalse(any(p.exists() for p in paths[1:]))
        self.assertEqual(sum(p.exists() for p in maps), 1)

    def test_recent_dependency_records_without_maps_are_reused(self):
        payload = self.artifact(1, "olean")
        index = self.output(1, payload.name, scope="mathlib")
        obsolete = self.artifact(2)
        stale = self.output(2, obsolete.name, scope="mathlib")
        os.utime(stale, (self.now - cache.MAX_AGE - 1,) * 2)
        self.collect()
        self.assertTrue(payload.exists())
        self.assertTrue(index.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(obsolete.exists())

    def test_collection_between_continuous_claims(self):
        path = self.artifact(1)
        self.write("tasks/1/task.json", "{}")
        self.store.execute("INSERT INTO claims VALUES (1, NULL, ?)", (self.now + 600,))
        self.store.commit()
        self.assertEqual(self.collect()["state"], "busy")
        self.assertEqual(cache.before_task(self.root)["state"], "collected")
        self.assertFalse(path.exists())

    def test_age_limit_removes_even_last_snapshot(self):
        path = self.artifact(1)
        mapping = self.snapshot(1, [[f"{1:016x}", path.name]], age=cache.MAX_AGE + 1)
        result = self.collect()
        self.assertEqual(result["snapshots_retained"], 0)
        self.assertFalse(path.exists())
        self.assertFalse(mapping.exists())

    def test_successful_local_publication_retains_new_outputs(self):
        path = self.artifact(1)
        self.write(
            "tasks/7/logs/repair-outputs.jsonl", "\n".join(map(json.dumps, ["2026-03-17", [f"{1:016x}", path.name]]))
        )
        self.write("tasks/7/logs/publish.json", json.dumps({"published": True, "revision": "a" * 40}))
        self.assertEqual(self.collect()["snapshots_retained"], 1)
        self.assertTrue(path.exists())

    def test_nested_early_publication_outputs_are_retained_for_reuse(self):
        path = self.artifact(1)
        directory = "tasks/7/logs/publications/" + "a" * 40
        self.write(
            directory + "/repair-outputs.jsonl", "\n".join(map(json.dumps, ["2026-03-17", [f"{1:016x}", path.name]]))
        )
        self.write(directory + "/publish.json", json.dumps({"published": True, "revision": "a" * 40}))
        self.assertEqual(self.collect()["snapshots_retained"], 1)
        self.assertTrue(path.exists())

    def test_failed_publication_is_not_a_reusable_snapshot(self):
        path = self.artifact(1)
        self.write(
            "tasks/7/logs/repair-outputs.jsonl", "\n".join(map(json.dumps, ["2026-03-17", [f"{1:016x}", path.name]]))
        )
        self.write("tasks/7/logs/publish.json", json.dumps({"published": False, "revision": "a" * 40}))
        self.collect()
        self.assertFalse(path.exists())

    def test_incomplete_native_index_is_removed_and_archive_kept(self):
        archive = self.artifact(1)
        self.snapshot(1, [[f"{1:016x}", archive.name]])
        index = self.output(1, {"l": archive.name, "o": ["1234567890abcdef.olean"]})
        self.collect()
        self.assertFalse(index.exists())
        self.assertTrue(archive.exists())

    def test_hardlinked_private_alias_is_removed_without_deleting_external_copy(self):
        path = self.artifact(1)
        external = self.root.parent / "shared-artifact"
        os.link(path, external)
        result = self.collect()
        self.assertEqual(result["bytes_unlinked"], 10)
        self.assertEqual(result["bytes_freed"], 0)
        self.assertTrue(external.exists())
        self.assertFalse(path.exists())

    def test_legacy_worktree_and_claim_block_pruning(self):
        path = self.artifact(1)
        tree = self.root / "worktrees/task-1"
        tree.mkdir(parents=True)
        self.assertEqual(self.collect()["state"], "busy")
        tree.rmdir()
        self.write("tasks/1/task.json", "{}")
        self.store.execute("INSERT INTO claims VALUES (1, NULL, ?)", (self.now + 600,))
        self.store.commit()
        self.assertEqual(self.collect()["state"], "busy")
        self.assertTrue(path.exists())
        self.store.execute("UPDATE claims SET expires_at=0")
        self.store.commit()
        self.assertEqual(self.collect()["state"], "collected")

    def test_parked_batch_tree_does_not_block_pruning(self):
        path = self.artifact(1)
        (self.root / "worktrees/batch-7/.lake").mkdir(parents=True)
        self.assertEqual(self.collect()["state"], "collected")
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "worktrees/batch-7/.lake").is_dir())
        (self.root / "worktrees/batch-x").mkdir()
        self.assertEqual(self.collect()["state"], "busy", "an unrecognized tree still counts as a user")

    def test_handoff_blocks_collection_until_publisher_can_acquire_lease(self):
        path = self.artifact(1)
        self.write("tasks/1/logs/publish.json", json.dumps({"state": "running", "started_at": self.now}))
        self.assertEqual(self.collect()["state"], "busy")
        self.assertTrue(path.exists())

    def test_active_cache_lease_blocks_collection_and_crash_releases_it(self):
        path = self.artifact(1)
        code = (
            "import sys,time;from pathlib import Path;from tauceti_queue.repair_cache import cache_lease;"
            '\nwith cache_lease(Path(sys.argv[1])):\n print("ready",flush=True);time.sleep(120)'
        )
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.root)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            self.assertEqual(self.collect()["state"], "busy")
            self.assertTrue(path.exists())
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        self.assertEqual(self.collect()["state"], "collected")

    def test_bad_selected_mapping_fails_closed_before_mutation(self):
        path = self.artifact(1)
        self.snapshot(1, [[f"{1:016x}", "../outside.ltar"]])
        self.assertEqual(self.collect()["state"], "error")
        self.assertTrue(path.exists())

    def test_symlink_pool_is_never_traversed(self):
        external = self.root.parent / "outside"
        external.mkdir()
        self.root.mkdir()
        self.pool.symlink_to(external, target_is_directory=True)
        self.assertEqual(self.collect()["state"], "error")

    def test_two_workers_bind_to_one_cache_and_keep_independent_worktrees(self):
        from tauceti_queue import repair

        shared = self.root.parent / "shared"
        other = self.root.parent / "other-worker"
        with patch.dict(os.environ, {"TAUCETI_QUEUE_CACHE_ROOT": str(shared)}):
            first = cache.task_cache(self.root, 1, 7)
            second = cache.task_cache(other, 2, 7)
        self.assertEqual(first, second)
        self.assertEqual(first, shared.absolute() / "batches/7")
        left = repair.lake_env(self.root, base_env={}, cache_root=first)
        right = repair.lake_env(other, base_env={}, cache_root=second)
        self.assertEqual(left["LAKE_CACHE_DIR"], right["LAKE_CACHE_DIR"])
        self.assertNotEqual(left["LAKE_CONFIG"], right["LAKE_CONFIG"])
        self.assertEqual(set(cache.participants(first)), {first, self.root, other})

    def test_existing_attempt_keeps_private_pool_across_configuration_changes(self):
        (self.root / "worktrees/task-1").mkdir(parents=True)
        with patch.dict(os.environ, {"TAUCETI_QUEUE_CACHE_ROOT": str(self.root.parent / "shared")}):
            self.assertEqual(cache.task_cache(self.root, 1), self.root)
        with patch.dict(os.environ, {"TAUCETI_QUEUE_CACHE_ROOT": str(self.root.parent / "different")}):
            self.assertEqual(cache.task_cache(self.root, 1), self.root)

    def test_idle_migration_deduplicates_and_removes_legacy_pool(self):
        source = self.artifact(1)
        shared = self.root.parent / "shared"
        target = shared / "lake-cache/artifacts" / source.name
        target.parent.mkdir(parents=True)
        target.write_text(source.read_text())
        inode = target.stat().st_ino
        second = self.artifact(2)
        second_inode = second.stat().st_ino
        self.assertEqual(cache.migrate(self.database, [self.root], shared)[0]["state"], "eligible")
        self.assertTrue(source.exists(), "preview preserves legacy data")
        result = cache.migrate(self.database, [self.root], shared, apply=True)[0]
        self.assertEqual(result["state"], "migrated")
        self.assertFalse((self.root / "lake-cache").exists())
        self.assertEqual(target.stat().st_ino, inode, "existing common artifacts must never be overwritten")
        self.assertEqual((target.parent / second.name).stat().st_ino, second_inode)

    def test_migration_excludes_active_owner_and_legacy_worktrees(self):
        source = self.artifact(1)
        shared = self.root.parent / "shared"
        with cache.cache_lease(self.root):
            self.assertEqual(cache.migrate(self.database, [self.root], shared, apply=True)[0]["state"], "busy")
        (self.root / "worktrees/task-1").mkdir(parents=True)
        self.assertEqual(cache.migrate(self.database, [self.root], shared, apply=True)[0]["state"], "busy")
        self.assertTrue(source.exists())

    def test_shared_retention_follows_publications_from_every_worker(self):
        other = self.root.parent / "other"
        cache.register(self.root, other)
        payload = self.artifact(1)
        logs = other / "tasks/7/logs"
        logs.mkdir(parents=True)
        (logs / "publish.json").write_text(json.dumps({"published": True, "revision": "b" * 40}))
        (logs / "repair-outputs.jsonl").write_text(
            "\n".join(map(json.dumps, ["2026-03-17", [f"{1:016x}", payload.name]]))
        )
        self.assertEqual(self.collect()["snapshots_retained"], 1)
        self.assertTrue(payload.exists())

    def test_shared_collection_excludes_only_owners_of_that_pool(self):
        legacy = self.root.parent / "legacy"
        (legacy / "worktrees/task-3").mkdir(parents=True)
        cache.register(self.root, legacy)
        payload = self.artifact(1)
        self.assertEqual(self.collect()["state"], "collected")
        self.assertFalse(payload.exists(), "an active private pool cannot pin unrelated shared artifacts")
        payload = self.artifact(1)
        binding = legacy / "tasks/3/cache-root.json"
        binding.parent.mkdir(parents=True)
        binding.write_text(json.dumps({"root": str(self.root)}))
        self.assertEqual(self.collect()["state"], "busy")
        self.assertTrue(payload.exists())

    def test_migration_refuses_symlinks_without_deleting_source(self):
        source = self.artifact(1)
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.pool / "unexpected").symlink_to(outside, target_is_directory=True)
        result = cache.migrate(self.database, [self.root], self.root.parent / "shared", apply=True)[0]
        self.assertEqual(result["state"], "error")
        self.assertTrue(source.exists())

    def test_migration_refuses_overlapping_source_and_destination(self):
        source = self.artifact(1)
        shared = self.pool / "nested"
        self.assertEqual(cache.migrate(self.database, [self.root], shared, apply=True)[0]["state"], "error")
        self.assertTrue(source.exists())

    def test_migration_does_not_follow_destination_ancestor_symlink(self):
        source = self.artifact(1)
        shared = self.root.parent / "shared"
        shared.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (shared / "lake-cache").symlink_to(outside, target_is_directory=True)
        result = cache.migrate(self.database, [self.root], shared, apply=True)[0]
        self.assertEqual(result["state"], "error")
        self.assertTrue(source.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_fetch_views_share_archives_but_isolate_temporary_files(self):
        cache.register(self.root, self.root.parent / "worker")
        archive = self.artifact(1)
        env = {"LAKE_CACHE_DIR": str(self.pool), **FETCH_ENV}
        with cache.downloads(env) as first, cache.downloads(env) as second:
            left, right = Path(first["LAKE_CACHE_DIR"]), Path(second["LAKE_CACHE_DIR"])
            self.assertNotEqual(left, right)
            self.assertEqual((left / "artifacts" / archive.name).stat().st_ino, archive.stat().st_ino)
            self.assertEqual((right / "artifacts" / archive.name).stat().st_ino, archive.stat().st_ino)
            for view, content in [(left, "first"), (right, "second")]:
                (view / "artifacts" / (archive.name + ".tmp")).write_text(content)
            self.assertEqual((left / "artifacts" / (archive.name + ".tmp")).read_text(), "first")
            outputs = right / "outputs/TauCeti"
            outputs.mkdir(parents=True)
            (outputs / f"{1:016x}.json").write_text(json.dumps({"data": archive.name}))
        self.assertFalse(left.exists())
        self.assertFalse(right.exists())
        self.assertEqual(list((self.pool / "artifacts").glob("*.tmp")), [])
        self.assertTrue((self.pool / "outputs/TauCeti" / f"{1:016x}.json").is_file())
        self.assertEqual(list((self.root / "cache-downloads").iterdir()), [])

    def test_fetch_does_not_refresh_unused_historical_snapshot_clocks(self):
        cache.register(self.root, self.root.parent / "worker")
        payload = self.artifact(1)
        old = self.snapshot(1, [[f"{1:016x}", payload.name]], age=86400)
        stamp = old.stat().st_mtime_ns
        with cache.downloads({"LAKE_CACHE_DIR": str(self.pool), **FETCH_ENV}) as env:
            view = Path(env["LAKE_CACHE_DIR"])
            self.assertFalse((view / "revisions").exists())
            fresh = view / "revisions/TauCeti" / ("a" * 40 + ".jsonl")
            fresh.parent.mkdir(parents=True)
            fresh.write_text(old.read_text())
        self.assertEqual(old.stat().st_mtime_ns, stamp)
        self.assertTrue((old.parent / fresh.name).exists())

    def test_idle_collection_removes_crashed_fetch_scratch_and_partial_artifacts(self):
        partial = self.write("lake-cache/artifacts/0123456789abcdef.ltar.tmp", "partial")
        scratch = self.write("cache-downloads/fetch-crashed/outputs/partial.json", "incomplete")
        self.collect()
        self.assertFalse(partial.exists())
        self.assertFalse(scratch.exists())

    def test_old_object_and_extensionless_artifacts_are_collected(self):
        old_object = self.artifact(1, "o")
        opaque = self.write("lake-cache/artifacts/0123456789abcdef", "old")
        self.assertEqual(self.collect()["artifacts_removed"], 2)
        self.assertFalse(old_object.exists())
        self.assertFalse(opaque.exists())

    def test_malformed_publication_does_not_fail_a_new_task(self):
        path = self.artifact(1)
        self.write("tasks/1/logs/publish.json", "[]")
        self.assertEqual(cache.before_task(self.root)["state"], "error")
        self.assertTrue(path.exists())

    def test_unrecognized_native_facet_fails_closed(self):
        path = self.artifact(1)
        self.snapshot(1, [[f"{1:016x}", path.name]])
        self.output(1, {"l": path.name, "newFacet": "opaque"})
        self.assertEqual(self.collect()["state"], "error")
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()

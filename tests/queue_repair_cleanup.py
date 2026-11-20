#!/usr/bin/env python3
"""Real worktrees are collected only after terminal queue evidence and exclusive ownership."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tauceti_queue import repair_cleanup as cleanup


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base", "--allow-empty")
        self.worker = self.root / "repair"
        (self.worker / "worktrees").mkdir(parents=True)
        self.database = self.root / "queue.sqlite3"
        self.store = sqlite3.connect(self.database)
        self.addCleanup(self.store.close)
        self.store.executescript("""
          CREATE TABLE batches (id INTEGER PRIMARY KEY, state TEXT);
          CREATE TABLE tasks (id INTEGER PRIMARY KEY, batch_id INTEGER, state TEXT,
                              repository TEXT, task_binding TEXT DEFAULT '{}');
          CREATE TABLE claims (task_id INTEGER, released_at REAL, expires_at REAL);
        """)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.clone), *args], stderr=subprocess.STDOUT, text=True)

    def tree(self, task_id=1, *, batch="head", state="open", suffix="", binding=None):
        self.store.execute("INSERT OR IGNORE INTO batches VALUES (?, ?)", (task_id, batch))
        self.store.execute(
            "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?, ?)",
            (task_id, task_id, state, "org/project", json.dumps(binding or {})),
        )
        self.store.commit()
        path = self.worker / "worktrees" / f"task-{task_id}{suffix}"
        self.git("worktree", "add", "--detach", str(path), "HEAD")
        record = self.worker / "tasks" / str(task_id)
        record.mkdir(parents=True, exist_ok=True)
        task = {"task_id": str(task_id), "batch_id": str(task_id), "repository": "org/project"}
        if binding:
            task["binding"] = binding
        (record / "task.json").write_text(json.dumps({"task": task, "state": "error"}))
        (record / "outcome.json").write_text('{"status":"error"}')
        return path

    def collect(self, *, apply=True):
        return cleanup.collect(self.database, self.clone, [self.worker], apply=apply)

    def state(self, state):
        self.store.execute("UPDATE tasks SET state=?", (state,))
        self.store.commit()

    def test_finished_batches_remove_even_failed_open_tasks_and_git_registration(self):
        for task_id, batch in ((1, "landed"), (2, "removed")):
            path = self.tree(task_id, batch=batch)
            (path / ".lake").mkdir()
            (path / ".lake/output").write_text("build outputs")
        rows = self.collect()
        self.assertEqual([x["state"] for x in rows], ["removed", "removed"])
        self.assertTrue(all(x["reason"] == "batch-finished" for x in rows))
        self.assertEqual(list((self.worker / "worktrees").iterdir()), [])
        self.assertNotIn("task-", self.git("worktree", "list", "--porcelain"))
        self.assertTrue((self.worker / "tasks/1/outcome.json").exists(), "retain diagnosis and checkpoints")

    def test_superseded_and_submitted_tasks_do_not_wait_for_the_batch_to_land(self):
        self.tree(1, state="superseded")
        self.tree(2, state="submitted", batch="open")
        self.assertEqual([x["state"] for x in self.collect()], ["removed", "removed"])

    def test_parallel_worktrees_are_collected_with_the_terminal_task(self):
        self.tree(state="superseded")
        self.tree(state="superseded", suffix="-group-2")
        self.assertEqual(len(self.collect()), 2)
        self.assertEqual(list((self.worker / "worktrees").iterdir()), [])

    def test_open_and_interrupted_work_is_preserved(self):
        path = self.tree()
        self.assertEqual(self.collect()[0]["state"], "unfinished")
        self.assertTrue(path.exists())

    def test_live_claim_survives_terminal_snapshot_and_expired_claim_does_not_retain_forever(self):
        path = self.tree(batch="landed")
        self.store.execute("INSERT INTO claims VALUES (1, NULL, ?)", (time.time() + 600,))
        self.store.commit()
        self.assertEqual(self.collect()[0]["state"], "claimed")
        self.assertTrue(path.exists())
        self.store.execute("UPDATE claims SET expires_at=0")
        self.store.commit()
        self.assertEqual(self.collect()[0]["state"], "removed")

    def test_active_owner_excludes_collection_and_a_dead_owner_releases_automatically(self):
        path = self.tree(batch="landed")
        code = (
            "import sys,time;from pathlib import Path;"
            "from tauceti_queue.repair_cleanup import worktree_lease;"
            '\nwith worktree_lease(Path(sys.argv[1]), 1):\n print("owned",flush=True);time.sleep(120)'
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(self.worker)], cwd=REPO, stdout=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "owned")
            self.assertEqual(self.collect()[0]["state"], "busy")
            self.assertTrue(path.exists())
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        lock = self.worker / "worktree-leases/1.lock"
        inode = lock.stat().st_ino
        self.assertEqual(self.collect()[0]["state"], "removed")
        self.assertEqual(lock.stat().st_ino, inode, "never unlink a lock inode")

    def test_worker_and_publisher_share_ownership_until_both_finish(self):
        self.tree(state="submitted")
        with cleanup.worktree_lease(self.worker, 1):
            with cleanup.worktree_lease(self.worker, 1):
                self.assertEqual(self.collect()[0]["state"], "busy")
            self.assertEqual(self.collect()[0]["state"], "busy")
        self.assertEqual(self.collect()[0]["state"], "removed")

    def test_publisher_handoff_is_bounded_and_stale_running_records_do_not_leak(self):
        self.tree(state="submitted")
        logdir = self.worker / "tasks/1/logs"
        logdir.mkdir()
        (logdir / "publish.json").write_text(json.dumps({"state": "running", "started_at": time.time()}))
        self.assertEqual(self.collect()[0]["state"], "publisher-starting")
        with patch.object(cleanup.time, "time", return_value=time.time() + cleanup.HANDOFF_SECONDS + 1):
            self.assertEqual(self.collect()[0]["state"], "removed")

    def test_preview_preserves_tree_and_dependency_symlink_never_deletes_seed(self):
        path = self.tree(state="superseded")
        seed = self.root / "seed"
        seed.mkdir()
        (seed / "olean").write_text("shared")
        (path / ".lake").mkdir()
        (path / ".lake/packages").symlink_to(seed)
        self.assertEqual(self.collect(apply=False)[0]["state"], "eligible")
        self.assertTrue(path.exists())
        self.assertEqual(self.collect()[0]["state"], "removed")
        self.assertEqual((seed / "olean").read_text(), "shared")

    def test_unknown_database_or_mismatched_identity_never_authorizes_deletion(self):
        path = self.tree(batch="landed")
        self.store.execute("UPDATE tasks SET repository='different/project'")
        self.store.commit()
        self.assertEqual(self.collect()[0]["state"], "unknown")
        self.assertTrue(path.exists())
        with self.assertRaises(sqlite3.OperationalError):
            cleanup.collect(self.root / "missing.sqlite3", self.clone, [self.worker], apply=True)
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_malformed_checkpoint_is_reported_without_blocking_other_terminal_trees(self):
        first = self.tree(batch="landed")
        self.tree(2, batch="landed")
        (self.worker / "tasks/1/task.json").write_text('{"task": []}')
        rows = self.collect()
        self.assertEqual([x["state"] for x in rows], ["error", "removed"])
        self.assertTrue(first.exists())

    def test_native_task_binding_must_match_database_in_full(self):
        path = self.tree(batch="landed", binding={"queue_epoch": "a"})
        self.store.execute("UPDATE tasks SET task_binding=?", ('{"queue_epoch":"b"}',))
        self.store.commit()
        self.assertEqual(self.collect()[0]["state"], "unknown")
        self.store.execute("UPDATE tasks SET task_binding=?", ('{"queue_epoch":"a"}',))
        self.store.commit()
        self.assertEqual(self.collect()[0]["state"], "removed")
        self.assertFalse(path.exists())

    def test_symlinked_worktrees_and_roots_are_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.worker / "worktrees/task-99").symlink_to(outside)
        self.assertEqual(self.collect(), [])
        alias = self.root / "alias"
        alias.symlink_to(self.worker)
        with self.assertRaises(ValueError):
            cleanup.collect(self.database, self.clone, [alias], apply=True)
        self.assertTrue(outside.is_dir())

    def test_worker_attempt_holds_the_cleanup_lease_including_error_paths(self):
        from types import SimpleNamespace

        from tauceti_queue import repair_task
        from tauceti_queue.repair_cache import cache_lease

        self.tree(state="submitted")
        cfg = SimpleNamespace(root=self.worker, binding=None)
        task = SimpleNamespace(task_id="1", kind="repair", binding=None, extra={})

        def attempt(*args, **kwargs):
            with cache_lease(self.worker, exclusive=True) as acquired:
                self.assertFalse(acquired)
            self.assertEqual(self.collect()[0]["state"], "busy")
            raise RuntimeError("worker failed")

        with (
            patch.object(repair_task, "_run_task", side_effect=attempt),
            self.assertRaisesRegex(RuntimeError, "worker failed"),
        ):
            repair_task.run_task(cfg, task, None)
        self.assertEqual(self.collect()[0]["state"], "removed")

    def test_publisher_holds_the_same_lease_even_after_worker_submission(self):
        from tauceti_queue import repair_publish
        from tauceti_queue.repair_cache import cache_lease

        path = self.tree(state="submitted")
        spec = self.root / "publish-spec.json"
        spec.write_text(json.dumps({"worktree": str(path), "task_id": "1"}))

        def publish(_path):
            with cache_lease(self.worker, exclusive=True) as acquired:
                self.assertFalse(acquired)
            self.assertEqual(self.collect()[0]["state"], "busy")
            return 0

        with patch.object(repair_publish, "_run_spec", side_effect=publish):
            self.assertEqual(repair_publish.run_spec(spec), 0)
        self.assertEqual(self.collect()[0]["state"], "removed")

    def test_publisher_holds_the_tasks_shared_cache_lease(self):
        from tauceti_queue import repair_publish
        from tauceti_queue.repair_cache import cache_lease

        path = self.tree(state="submitted")
        shared = self.root / "shared-cache"
        spec = self.root / "publish-spec.json"
        spec.write_text(json.dumps({"worktree": str(path), "task_id": "1", "cache_root": str(shared)}))

        def publish(_path):
            with cache_lease(shared, exclusive=True) as acquired:
                self.assertFalse(acquired)
            return 0

        with patch.object(repair_publish, "_run_spec", side_effect=publish):
            self.assertEqual(repair_publish.run_spec(spec), 0)

    def test_early_snapshot_handoff_is_protected_then_terminal_scratch_is_removed(self):
        tree = self.tree(state="submitted", suffix="-publish-" + "a" * 40)
        (tree / "outputs.jsonl").write_text('"2026-03-17"\n')
        (tree / ("1" * 16 + ".ltar")).write_text("frozen artifact")
        logdir = self.worker / "tasks/1/logs/publications" / ("a" * 40)
        logdir.mkdir(parents=True)
        record = logdir / "publish.json"
        record.write_text(json.dumps({"state": "running", "started_at": time.time()}))
        self.assertEqual(self.collect()[0]["state"], "publisher-starting")
        record.write_text(json.dumps({"state": "running", "started_at": 0}))
        self.assertEqual(self.collect()[0]["state"], "removed")
        self.assertFalse(tree.exists(), "a crashed early publisher cannot leave frozen artifacts behind")

    def test_failed_removal_is_reported_and_retried(self):
        self.tree(batch="landed")
        with patch.object(cleanup.repair, "remove_worktree"):
            self.assertEqual(self.collect()[0]["state"], "error")
        self.assertEqual(self.collect()[0]["state"], "removed")

    # A task's tree is parked as its batch's tree and the batch's next task takes it whole.
    def test_park_then_adopt_keeps_lake_and_resets_worker_state(self):
        path = self.tree(1, batch="head")
        (path / ".lake/build").mkdir(parents=True)
        (path / ".lake/build/M.olean").write_text("built")
        (path / ".lake/outputs.jsonl").write_text("previous map")
        (path / ".tauceti-repair").mkdir()
        (path / ".tauceti-repair/build.lock").mkdir()
        (path / ".tauceti-repair/errors.jsonl").write_text("{}")
        head = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(cleanup.retire(self.clone, self.worker, "1", 1, path, keep=True), "parked")
        parked = self.worker / "worktrees/batch-1"
        self.assertFalse(path.exists())
        self.assertTrue((parked / ".lake/build/M.olean").exists())
        self.assertEqual(json.loads((parked / ".lake/tauceti-batch-tree.json").read_text()), {"task_id": "1"})
        target = self.worker / "worktrees/task-2"
        self.assertEqual(cleanup.adopt(self.clone, self.worker, "1", target), head)
        self.assertFalse(parked.exists())
        self.assertEqual((target / ".lake/build/M.olean").read_text(), "built")
        self.assertFalse((target / ".lake/outputs.jsonl").exists())
        self.assertFalse((target / ".tauceti-repair").exists())
        self.assertIn(str(target), self.git("worktree", "list", "--porcelain"))
        self.assertIsNone(cleanup.adopt(self.clone, self.worker, "1", self.worker / "worktrees/task-3"))

    def test_unprepared_trees_and_second_trees_of_a_batch_are_removed(self):
        first, second, third = (self.tree(n) for n in (1, 2, 3))
        self.assertEqual(cleanup.retire(self.clone, self.worker, "9", 1, first, keep=False), "removed")
        self.assertEqual(cleanup.retire(self.clone, self.worker, "9", 2, second, keep=True), "parked")
        self.assertEqual(cleanup.retire(self.clone, self.worker, "9", 3, third, keep=True), "removed")
        self.assertEqual(cleanup.retire(self.clone, self.worker, "", 3, third, keep=True), "removed")
        self.assertEqual(sorted(p.name for p in (self.worker / "worktrees").iterdir()), ["batch-9"])

    def test_a_parked_tree_being_adopted_is_not_collected(self):
        cleanup.retire(self.clone, self.worker, "1", 1, self.tree(1, batch="landed"), keep=True)
        with cleanup.worktree_lease(self.worker, "batch-1"):  # what adoption holds while it renames
            self.assertEqual(self.collect()[0]["state"], "busy")
        self.assertEqual(self.collect()[0]["state"], "removed")

    def test_a_tree_is_never_moved_into_an_existing_parked_tree(self):
        first, second = self.tree(1), self.tree(2)
        cleanup.retire(self.clone, self.worker, "9", 1, first, keep=True)
        parked = self.worker / "worktrees/batch-9"
        with self.assertRaises(OSError):
            cleanup._move(self.clone, second, parked)  # what a racing second publisher would attempt
        self.assertTrue(second.is_dir())
        self.assertEqual(sorted(p.name for p in parked.iterdir()), [".git", ".lake"])

    def test_parked_trees_are_collected_when_their_batch_is_finished(self):
        for task_id, batch in ((1, "landed"), (2, "removed"), (3, "open")):
            cleanup.retire(self.clone, self.worker, str(task_id), task_id, self.tree(task_id, batch=batch), keep=True)
        unrecorded = self.worker / "worktrees/batch-4"
        self.git("worktree", "add", "--detach", str(unrecorded), "HEAD")
        rows = {row["batch_id"]: row["state"] for row in self.collect()}
        self.assertEqual(rows, {1: "removed", 2: "removed", 3: "unfinished", 4: "unknown"})
        self.assertEqual(sorted(p.name for p in (self.worker / "worktrees").iterdir()), ["batch-3", "batch-4"])


if __name__ == "__main__":
    unittest.main()

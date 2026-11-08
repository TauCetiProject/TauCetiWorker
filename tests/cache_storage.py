"""Storage policy fixtures: no real fleet paths, service calls, or GitHub requests."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cache_storage as storage
from tauceti_worker import config, retention
from tauceti_worker import expanded_cache as cache
from tauceti_worker.runtime_status import atomic_json


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
        self.env = patch.dict(
            os.environ,
            {"TAUCETI_BUILD_CACHE_ROOT": str(self.pool), "TAUCETI_ACTIVITY_DIR": str(self.root / "activity")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        storage.initialize(self.pool)
        real_usage = shutil.disk_usage(self.root)
        usage = shutil._ntuple_diskusage(real_usage.total, real_usage.total // 2, real_usage.total // 2)
        mock_usage = patch.object(storage.shutil, "disk_usage", return_value=usage)
        mock_usage.start()
        self.addCleanup(mock_usage.stop)

    def entry(self, key="a" * 16, age=0):
        path = self.pool / "entries" / key
        path.mkdir(parents=True)
        (path / "data").write_bytes(b"x" * 8192)
        atomic_json(path / "entry.json", {"data": {}})
        os.utime(path / "entry.json", (time.time() - age,) * 2)
        return path

    def test_ownership_and_symlinks(self):
        foreign = self.root / "operator"
        foreign.mkdir()
        (foreign / "unrelated").write_text("keep")
        with self.assertRaises(ValueError):
            storage.initialize(foreign)
        alias = self.root / "alias"
        alias.symlink_to(foreign)
        with self.assertRaises(ValueError):
            storage.initialize(alias)
        self.assertFalse((foreign / "owner.json").exists())

    def test_inventory_counts_hardlinks_once_and_never_follows_symlinks(self):
        first = self.root / "a"
        first.mkdir()
        source = first / "payload"
        source.write_bytes(b"x" * 8192)
        second = self.root / "b"
        second.mkdir()
        os.link(source, second / "copy")
        (first / "outside").symlink_to("/etc")
        row = storage.inventory([first, second])[source.stat().st_dev]
        self.assertEqual(row["managed_bytes"], source.stat().st_blocks * 512)
        self.assertEqual(row["externally_linked_bytes"], 0)

    def test_age_cleanup_keeps_recent_and_busy_entries(self):
        recent = self.entry()
        old = self.entry("b" * 16, age=4 * 86400)
        busy = self.entry("c" * 16, age=4 * 86400)
        with cache.locked(self.pool, busy.name):
            result = storage.collect([])
        self.assertTrue(recent.exists())
        self.assertFalse(old.exists())
        self.assertTrue(busy.exists())
        self.assertFalse(result["errors"])

    def test_pressure_evicts_without_waiting_for_age(self):
        old = self.entry(age=3600)
        with patch.dict(os.environ, {"TAUCETI_CACHE_MAX_GIB": "0.000001"}):
            result = storage.collect([])
        self.assertFalse(old.exists())
        self.assertIn("managed_bytes", result)
        self.assertIn("free_bytes", result["filesystems"][0])

    def test_dry_run_does_not_create_locks_or_change_files(self):
        self.entry(age=4 * 86400)

        def snapshot():
            return {str(p.relative_to(self.pool)): p.read_bytes() for p in self.pool.rglob("*") if p.is_file()}

        before = snapshot()
        storage.collect([], dry=True)
        self.assertEqual(snapshot(), before)

    def test_deleting_pool_links_does_not_claim_worker_bytes_freed(self):
        entry = self.entry(age=4 * 86400)
        private = self.root / "private"
        os.link(entry / "data", private)
        result = storage.collect([])
        self.assertFalse(entry.exists())
        self.assertEqual(private.read_bytes(), b"x" * 8192)
        self.assertGreaterEqual(result["reclaimed_bytes"], 0)

    def test_critical_hysteresis_recovers_without_human_and_preserves_active_rounds(self):
        cfg = SimpleNamespace(state=self.root)
        usage = shutil._ntuple_diskusage
        with (
            patch.object(storage.shutil, "disk_usage", return_value=usage(1000, 960, 40)),
            patch.object(retention, "request_cleanup"),
        ):
            with self.assertRaises(config.AdmissionUnavailable) as error:
                storage.admit(cfg)
            self.assertEqual(error.exception.state, "waiting-storage")
        with patch.object(storage.shutil, "disk_usage", return_value=usage(1000, 900, 100)):
            with self.assertRaises(config.AdmissionUnavailable):
                storage.admit(cfg)
        with patch.object(storage.shutil, "disk_usage", return_value=usage(1000, 800, 200)):
            storage.admit(cfg)
        self.assertFalse(list(self.pool.glob("critical-*.json")))

    def test_large_disk_recovers_old_latch_with_hundreds_of_gib_free(self):
        gib = 1024**3
        marker = self.root / "activity" / f"critical-{self.root.stat().st_dev}.json"
        atomic_json(marker, dict(active=True))
        with patch.object(
            storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(3500 * gib, 3220 * gib, 280 * gib)
        ):
            storage.admit(SimpleNamespace(state=self.root))
        self.assertFalse(marker.exists())
        self.assertEqual(storage.headroom(3500 * gib, storage.CRITICAL), 20 * gib)
        self.assertEqual(storage.headroom(3500 * gib, storage.LOW), 40 * gib)
        self.assertEqual(storage.headroom(3500 * gib, storage.TARGET), 60 * gib)

    def test_live_checkout_hardlinks_do_not_flush_pool_or_request_recycle(self):
        entry = self.entry()
        canonical = self.pool / "artifacts/data"
        canonical.parent.mkdir()
        os.link(entry / "data", canonical)
        state = self.root / "worker"
        current = state / "checkout/.lake"
        private = state / "home/.cache/lake"
        current.mkdir(parents=True)
        private.mkdir(parents=True)
        os.link(canonical, current / "data")
        os.link(canonical, private / "data")
        cfg = SimpleNamespace(wid="one", state=state, checkout=state / "checkout")
        with (
            patch.dict(os.environ, {"TAUCETI_CACHE_MAX_GIB": "0.000001"}),
            patch.object(retention, "workspace_idle", return_value=False),
        ):
            storage.collect([cfg])
        self.assertTrue(entry.exists())
        request = json.loads((self.root / "activity/cache-pressure.json").read_text())
        self.assertFalse(request["workers"])
        self.assertEqual((current / "data").read_bytes(), b"x" * 8192)

    def test_pressure_requests_one_old_worker_and_obeys_cooldown(self):
        configs = []
        for index in (1, 2, 3):
            state = self.root / str(index)
            private = state / "home/.cache/lake"
            private.mkdir(parents=True)
            (private / "unused").write_bytes(b"x" * 8192)
            atomic_json(state / "cache-generation.json", dict(started_at=index))
            configs.append(SimpleNamespace(wid=str(index), state=state, checkout=state / "checkout"))
        with (
            patch.dict(os.environ, {"TAUCETI_CACHE_MAX_GIB": "0.000001"}),
            patch.object(storage, "MIN_PRIVATE_RECLAIM", 4096),
        ):
            with (
                retention.locked(configs[0].state / "round.lock"),
                retention.locked(configs[1].state / "verification-jobs/held.lock"),
            ):
                self.assertEqual(storage.collect(configs)["private_reclamation_worker"], "3")
                self.assertEqual(storage.collect(configs, opened=None)["private_reclamation_worker"], "1")
                with (
                    retention.locked(configs[1].state / "round.lock"),
                    retention.locked(configs[2].state / "round.lock"),
                ):
                    self.assertEqual(storage.collect(configs)["private_reclamation_worker"], "1")
            first = storage.collect(configs)
            self.assertEqual(first["private_reclamation_worker"], "1")
            atomic_json(configs[0].state / "cache-generation.json", dict(started_at=1, last_pressure_at=time.time()))
            second = storage.collect(configs)
        self.assertEqual(second["private_reclamation_worker"], "2")

    def test_private_pressure_collection_works_without_shared_cache(self):
        shutil.rmtree(self.pool)
        state = self.root / "worker"
        private = state / "home/.cache/lake"
        private.mkdir(parents=True)
        payload = private / "payload"
        payload.write_bytes(b"x" * 8192)
        cfg = SimpleNamespace(wid="one", state=state, checkout=state / "checkout")
        with (
            patch.dict(os.environ, {"TAUCETI_CACHE_MAX_GIB": "0.000001"}),
            patch.object(storage, "MIN_PRIVATE_RECLAIM", 4096),
        ):
            result = storage.collect([cfg])
        request = json.loads((self.root / "activity/cache-pressure.json").read_text())
        self.assertGreater(request["until"], time.time())
        self.assertTrue(payload.exists())  # The worker reclaims it at its safe boundary.
        self.assertGreater(result["managed_bytes"], 0)
        self.assertFalse(self.pool.exists())

    def test_full_checkout_filesystem_defers_even_when_state_filesystem_has_space(self):
        checkout = self.root / "scratch-checkout"
        checkout.mkdir()
        real_stat = Path.stat

        def metadata(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == checkout:
                return SimpleNamespace(st_dev=info.st_dev + 1)
            return info

        def usage(path):
            return (
                shutil._ntuple_diskusage(1000, 960, 40)
                if path == checkout
                else shutil._ntuple_diskusage(1000, 500, 500)
            )

        cfg = SimpleNamespace(state=self.root, checkout=checkout)
        with (
            patch.object(Path, "stat", metadata),
            patch.object(storage.shutil, "disk_usage", side_effect=usage),
            patch.object(retention, "request_cleanup"),
        ):
            with self.assertRaises(config.AdmissionUnavailable) as error:
                storage.admit(cfg)
        self.assertEqual(error.exception.state, "waiting-storage")

    def test_resume_interrupted_delete_and_preserve_operator_cache(self):
        entry = self.entry(age=4 * 86400)
        pending = entry.with_name(entry.name + ".retention-deleting")
        entry.rename(pending)
        operator = self.root / "operator-cache"
        operator.mkdir()
        (operator / "old.ltar").write_text("keep")
        storage.collect([])
        self.assertFalse(pending.exists())
        self.assertEqual((operator / "old.ltar").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

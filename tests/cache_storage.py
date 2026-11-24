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
            {
                "TAUCETI_BUILD_CACHE_ROOT": str(self.pool),
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "mathlib-builds"),
                "HOME": str(self.root / "home"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        from tauceti_worker import activity

        assert cache.root() == self.pool and activity.root() == self.root / "activity", "unisolated test"
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

    def test_inventory_remembers_only_hard_linked_inodes(self):
        mine, theirs, busy = (self.root / name for name in ("mine", "theirs", "busy"))
        for directory in (mine, theirs, busy):
            (directory / "deep" / "er").mkdir(parents=True)
        single = mine / "deep" / "er" / "single"
        single.write_bytes(b"s" * 12288)
        shared = mine / "shared"
        shared.write_bytes(b"h" * 8192)
        os.link(shared, theirs / "deep" / "shared")
        (busy / "deep" / "held").write_bytes(b"b" * 4096)
        (mine / "deep" / "link").symlink_to(single)
        (mine / "dirlink").symlink_to(busy)
        details, private = {}, {}
        totals = storage.inventory(
            [mine, theirs, busy],
            protected={busy},
            owners={mine: "w1", theirs: "w2"},
            details=details,
            private=private,
        )
        device = single.stat().st_dev
        size = lambda path: path.stat().st_blocks * 512  # noqa: E731
        row = totals[device]
        self.assertEqual(row["managed_bytes"], size(single) + size(shared) + size(busy / "deep" / "held"))
        self.assertEqual(row["named_bytes"], size(single) + 2 * size(shared) + size(busy / "deep" / "held"))
        self.assertEqual(row["unique_inodes"], 3)
        self.assertEqual(row["protected_bytes"], size(busy / "deep" / "held"))
        self.assertEqual(list(details), [(device, shared.stat().st_ino)])  # single links are not kept
        self.assertEqual(details[(device, shared.stat().st_ino)][4], {"w1": 1, "w2": 1})
        self.assertEqual(private, {(device, "w1"): size(single)})

    def test_a_walk_past_the_deadline_defers_and_deletes_nothing(self):
        old = self.entry(age=4 * 86400)
        with self.assertRaises(storage.InventoryDeferred):
            storage.inventory([self.pool], deadline=time.monotonic() - 1)
        result = storage.collect([], deadline=time.monotonic() - 1)  # a small pool, default interval
        self.assertEqual(result["deferred"], ["shared-cache-inventory", "shared-cache-deletion"])
        self.assertEqual(result["removed"], [])
        self.assertTrue(old.exists())
        self.assertFalse(storage.collect([], deadline=time.monotonic() + 600).get("deferred"))
        self.assertFalse(old.exists())

    def test_a_deferred_walk_still_deletes_expired_entries(self):
        old = self.entry(age=4 * 86400)
        with patch.object(storage, "inventory", side_effect=storage.InventoryDeferred("inventory deadline")):
            result = storage.collect([], deadline=time.monotonic() + 600)
        self.assertEqual(result["deferred"], ["shared-cache-inventory"])
        self.assertFalse(old.exists())

    def test_overlapping_roots_still_count_each_file_once(self):
        outer = self.root / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (inner / "single").write_bytes(b"s" * 8192)
        details, private = {}, {}
        totals = storage.inventory(
            [outer, inner], protected={inner}, owners={outer: "w1"}, details=details, private=private
        )
        info = (inner / "single").stat()
        row = totals[info.st_dev]
        self.assertEqual(row["managed_bytes"], info.st_blocks * 512)
        self.assertEqual(row["unique_inodes"], 1)
        self.assertEqual(row["named_bytes"], 2 * info.st_blocks * 512)
        self.assertEqual(row["protected_bytes"], info.st_blocks * 512)
        self.assertTrue(details[(info.st_dev, info.st_ino)][3])  # protected through the inner root
        self.assertEqual(private, {})

    def test_overlap_is_found_past_siblings_and_repeated_spellings(self):
        base = self.root / "x"
        for name in ("a/inner", "a-aux"):
            (base / name).mkdir(parents=True)
        (base / "alias").symlink_to(base / "a")
        self.assertTrue(storage._overlapping([base / "a", base / "a-aux", base / "a/inner"]))
        self.assertTrue(storage._overlapping([base / "a", base / "alias"]))
        self.assertFalse(storage._overlapping([base / "a", base / "a-aux"]))

    def test_an_aggregation_finishing_past_the_deadline_defers(self):
        (self.root / "many").mkdir()
        for index in range(3):
            (self.root / "many" / f"f{index}").write_bytes(b"x")
            os.link(self.root / "many" / f"f{index}", self.root / "many" / f"g{index}")
        moments = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
        with (
            patch.object(storage, "INVENTORY_CHECK_EVERY", 1),
            patch.object(storage.time, "monotonic", side_effect=lambda: next(moments, 10.0)),
        ):
            details = {}
            with self.assertRaises(storage.InventoryDeferred):
                storage.inventory([self.root / "many"], details=details, deadline=5.0)
        self.assertEqual(details, {})  # nothing handed back from a deferred inventory

    def test_a_directory_swapped_for_a_link_mid_walk_is_not_followed(self):
        root, outside = self.root / "walked", self.root / "outside"
        (root / "sub").mkdir(parents=True)
        (root / "file").write_bytes(b"f" * 4096)
        outside.mkdir()
        (outside / "foreign").write_bytes(b"x" * 4096)
        walk = storage._scan(root, storage._Clock(None))
        first = next(walk)  # the root's own listing; sub/ is opened only afterwards
        (root / "sub").rmdir()
        (root / "sub").symlink_to(outside)
        rest = list(walk)
        self.assertEqual([info.st_ino for info in [first, *rest]], [(root / "file").stat().st_ino])

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

    def test_pressure_reclaims_payloads_linked_only_by_pool_names(self):
        from tauceti_worker import canonical_store

        for disabled in (False, True):
            key = ("b" if disabled else "a") * 16
            name = ("d" if disabled else "c") * 16 + ".olean"
            entry = self.pool / "entries" / key
            entry.mkdir(parents=True)
            (entry / name).write_bytes(b"x" * 8192)
            atomic_json(entry / "entry.json", {"data": {}})
            legacy = self.pool / "artifacts" / name
            canonical = canonical_store.store(self.pool) / "objects" / name
            for alias in (legacy, canonical):
                alias.parent.mkdir(parents=True, exist_ok=True)
                os.link(entry / name, alias)  # Entry, legacy copy and canonical object: 3 links.
            if disabled:
                (canonical_store.store(self.pool) / "disabled").touch()  # Must not strand links.
            self.assertEqual(legacy.stat().st_nlink, 3)
            with patch.dict(os.environ, {"TAUCETI_CACHE_MAX_GIB": "0.000001"}):
                storage.collect([], opened=set())
            self.assertFalse(entry.exists(), disabled)
            self.assertFalse(legacy.exists(), disabled)
            self.assertFalse(canonical.exists(), disabled)

    def test_pool_names_survive_while_a_worker_links_the_payload(self):
        from tauceti_worker import canonical_store

        entry = self.pool / "entries" / ("a" * 16)
        entry.mkdir(parents=True)
        name = "c" * 16 + ".olean"
        (entry / name).write_bytes(b"x" * 8192)
        atomic_json(entry / "entry.json", {"data": {}})
        os.utime(entry / "entry.json", (time.time() - 4 * 86400,) * 2)
        legacy = self.pool / "artifacts" / name
        canonical = canonical_store.store(self.pool) / "objects" / name
        for alias in (legacy, canonical):
            alias.parent.mkdir(parents=True, exist_ok=True)
            os.link(entry / name, alias)
        worker = self.root / "worker-store/artifacts"
        worker.mkdir(parents=True)
        os.link(entry / name, worker / name)
        storage.collect([], opened=set())
        self.assertFalse(entry.exists())  # The aged entry goes,
        self.assertTrue(canonical.exists() and legacy.exists())  # but a linked object stays.

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
                retention.locked(configs[1].state / "round.lock"),
            ):
                self.assertEqual(storage.collect(configs)["private_reclamation_worker"], "3")
                self.assertEqual(storage.collect(configs, opened=None)["private_reclamation_worker"], "1")
                with retention.locked(configs[2].state / "round.lock"):
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

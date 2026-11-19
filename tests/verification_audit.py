#!/usr/bin/env python3
"""Private-state scaling, concurrent verification and mechanical recovery fixtures; no agents, Lean or network."""

import contextlib
import io
import json
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verification import REPO, Receipts, write

from tauceti_worker import verification as v


def hash_cache_entries(payload):
    return {
        key: dict(path=directory, sha256=value)
        for directory, values in v._HashCache.directories(payload).items()
        for key, value in values.items()
    }


class Recovery(Receipts):
    # Reuse the private Git/fake-verifier fixture; load_tests collects only these audit cases.
    def test_persisted_hash_cache_migrates_once_without_immutable_inventory_growth(self):
        import hashlib

        for index in range(2000):
            write(self.prefix, f"lib/immutable-{index}", "immutable payload\n")
        self.assertEqual(self.verify(stage="build")["status"], "success")
        current = self.state / "verification-hashes-v2.json"
        entries = hash_cache_entries(v.read_json(current))
        self.assertGreater(len(entries), 2000)
        legacy = self.state / "verification-hashes.json"
        v.atomic(legacy, {key: value["sha256"] for key, value in entries.items()})
        original_legacy = legacy.read_bytes()
        current.unlink()
        v._HashCache(self.state, self.root).marker.unlink()
        self.assertFalse(v._HashCache(self.state, self.root).imported)
        manifest = self.home / "registered.json"
        from tauceti_worker import verification_tools

        v.atomic(manifest, {str(self.prefix): "trusted-publisher-1", str(verification_tools.root()): "tools-1"})
        real_digest, real_read = hashlib.file_digest, v.read_json
        hashed, reads = [], []

        def measured_digest(stream, *args, **kwargs):
            hashed.append(Path(stream.name))
            return real_digest(stream, *args, **kwargs)

        def measured_read(path):
            reads.append(Path(path))
            return real_read(path)

        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-readonly"]),
            patch.object(v, "read_json", side_effect=measured_read),
            patch.object(hashlib, "file_digest", side_effect=measured_digest),
        ):
            result = self.verify(stage="build")
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertEqual(reads.count(legacy), 1)
            self.assertNotIn(self.root / "TauCeti/A.lean", hashed, "retain unchanged mutable hashes")
            self.assertNotIn(
                REPO / "tauceti_worker/verification.py", hashed, "migrate inputs not visited before the deadline"
            )
            self.assertNotIn(self.prefix / "lib/immutable-0", hashed, "retain valid stat-keyed payload hashes")
            compact = real_read(current)
            self.assertTrue(v._HashCache(self.state, self.root).imported)
            self.assertLess(len(hash_cache_entries(compact)), 200)
            self.assertFalse(
                any(Path(e["path"]).is_relative_to(self.prefix) for e in hash_cache_entries(compact).values())
            )
            size = current.stat().st_size
            reads.clear()
            hashed.clear()
            warm = self.verify(stage="build")
            self.assertEqual(warm["status"], "success")
            self.assertIn("reused_from", warm["phases"]["build"])
            self.assertNotIn(legacy, reads)
            self.assertEqual(hashed, [])
            self.assertEqual(current.stat().st_size, size)
            write(self.root, "TauCeti/A.lean", "candidate changed\n")
            reads.clear()
            changed = self.verify(stage="build")
            self.assertEqual(changed["status"], "success")
            self.assertNotIn("reused_from", changed["phases"]["build"])
            self.assertNotIn(legacy, reads)
            self.assertIn(self.root / "TauCeti/A.lean", hashed)
        self.assertEqual(legacy.read_bytes(), original_legacy, "rollback retains the V1 cache")

    def test_writable_installations_keep_the_general_payload_hash_cache(self):
        import hashlib

        for index in range(100):
            write(self.prefix, f"lib/mutable-{index}", "mutable payload\n")
        self.assertEqual(self.verify(stage="build")["status"], "success")
        with patch.object(hashlib, "file_digest", wraps=hashlib.file_digest) as hashes:
            self.assertEqual(self.verify(stage="build")["status"], "success")
            self.assertEqual(hashes.call_count, 0)
        entries = hash_cache_entries(v.read_json(self.state / "verification-hashes-v2.json"))
        self.assertTrue(any(Path(e["path"]).is_relative_to(self.prefix) for e in entries.values()))
        payload = v.read_json(self.state / "verification-hashes-v2.json")
        opaque = {key: value["sha256"] for key, value in entries.items()}
        self.assertLess(len(json.dumps(payload)), len(json.dumps(opaque)) + 4096)
        self.assertEqual(json.dumps(payload).count(str(self.prefix / "lib")), 1)

    def test_concurrent_cache_saves_merge_entries_and_root_migrations(self):
        first = v._HashCache(self.state, self.root)
        second = v._HashCache(self.state, self.home / "other-root")
        first.put("a" * 64, str(self.root / "First.lean"), "1" * 64)
        second.put("b" * 64, str(self.root / "Second.lean"), "2" * 64)
        first.finish_import()
        first.save({})
        # A stale early-deadline writer cannot lose entries or completed migration markers.
        second.save({})
        second.finish_import()
        second.save({})
        payload = v.read_json(first.path)
        self.assertEqual(len(hash_cache_entries(payload)), 2)
        self.assertTrue(first.marker.is_file())
        self.assertTrue(second.marker.is_file())

    def test_current_hashes_survive_history_eviction_in_alphabetically_early_directories(self):
        old = v._HashCache(self.state)
        for directory in ("aaa-live", "zzz-old", "zzz-older"):
            for index in range(3):
                old.put(v.digest([directory, index]), str(self.home / directory / str(index)), "1" * 64)
        old.save({})
        current = v._HashCache(self.state)
        key = v.digest(["aaa-live", 0])
        self.assertEqual(current.lookup(key, str(self.home / "aaa-live/0")), "1" * 64)
        fresh = v.digest("new-live-key")
        current.put(fresh, str(self.home / "aaa-live/fresh"), "2" * 64)
        with patch.object(v, "HASH_CACHE_LIMIT", 4):
            current.save({})
        entries = hash_cache_entries(v.read_json(current.path))
        self.assertEqual(len(entries), 4)
        self.assertIn(key, entries)
        self.assertIn(fresh, entries)

    def test_stale_untouched_inventory_cannot_restore_pruned_records(self):
        old = v._HashCache(self.state)
        old.put("a" * 64, str(self.prefix / "lib/obsolete"), "1" * 64)
        old.save({})
        stale = v._HashCache(self.state)
        prune = v._HashCache(self.state)
        prune.save({self.prefix})
        stale.put("b" * 64, str(self.root / "fresh"), "2" * 64)
        stale.save({})
        entries = hash_cache_entries(v.read_json(stale.path))
        self.assertNotIn("a" * 64, entries)
        self.assertIn("b" * 64, entries)

    def test_direct_hashes_survive_other_writers_with_different_executables(self):
        first, second = v._HashCache(self.state), v._HashCache(self.state)
        a, b = "a" * 64, "b" * 64
        first.lookup(a, str(self.prefix / "bin/first"))
        first.put(a, str(self.prefix / "bin/first"), "1" * 64)
        first.save({self.prefix})
        second.lookup(b, str(self.prefix / "bin/second"))
        second.put(b, str(self.prefix / "bin/second"), "2" * 64)
        second.save({self.prefix})
        self.assertEqual(set(hash_cache_entries(v.read_json(first.path))), {a, b})

    def test_hash_cache_lock_wait_is_bounded_and_optional(self):
        import fcntl

        self.state.mkdir(exist_ok=True)
        cache = v._HashCache(self.state)
        with cache.path.with_suffix(".lock").open("a+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            start = time.monotonic()
            with self.assertRaises(BlockingIOError):
                cache.save({})
            self.assertLess(time.monotonic() - start, 2)
            result = self.verify(stage="build")
            self.assertEqual(result["status"], "success", result.get("error"))

    def test_optional_hash_cache_write_failure_preserves_completed_result_and_status(self):
        with (
            patch.object(v._HashCache, "save", side_effect=OSError("fixture cache failure")),
            patch.object(v, "report_runtime") as status,
        ):
            result = self.verify(stage="all")
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(status.call_args.args[0], "working")

    def test_direct_executable_hashes_inside_an_immutable_prefix_are_retained(self):
        import hashlib
        import shutil

        lake = self.prefix / "bin/lake"
        shutil.copy2(self.bin / "lake", lake)
        manifest = self.home / "direct-executable.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        with (
            patch.dict(
                os.environ,
                PATH=f"{lake.parent}:{self.env['PATH']}",
                TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest),
            ),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-readonly"]),
        ):
            self.assertEqual(self.verify(stage="build")["status"], "success")
            with patch.object(hashlib, "file_digest", wraps=hashlib.file_digest) as hashes:
                result = self.verify(stage="build")
                self.assertEqual(result["status"], "success", result.get("error"))
                self.assertIn("reused_from", result["phases"]["build"])
                self.assertEqual(hashes.call_count, 0)

    def test_registering_an_existing_installation_prunes_its_v2_per_file_records(self):
        for index in range(100):
            write(self.prefix, f"lib/registered-later-{index}", "payload\n")
        self.assertEqual(self.verify(stage="build")["status"], "success")
        current = self.state / "verification-hashes-v2.json"
        before = current.stat().st_size
        manifest = self.home / "registered-later.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-readonly"]),
        ):
            self.assertEqual(self.verify(stage="build")["status"], "success")
            entries = hash_cache_entries(v.read_json(current))
            self.assertFalse(any(Path(e["path"]).is_relative_to(self.prefix) for e in entries.values()))
            self.assertLess(current.stat().st_size, before)

    def test_unwritable_identity_cache_does_not_repeat_payload_hashing(self):
        import hashlib
        from collections import Counter

        for index in range(100):
            write(self.prefix, f"lib/no-seal-cache-{index}", "payload\n")
        manifest = self.home / "readonly-installation.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        real_atomic, real_digest = v.atomic, hashlib.file_digest
        hashed = []

        def unavailable(path, value):
            if path.parent.name == "installations":
                raise OSError("fixture: seal cache unavailable")
            return real_atomic(path, value)

        def measured(stream, *args, **kwargs):
            hashed.append(Path(stream.name))
            return real_digest(stream, *args, **kwargs)

        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-readonly"]),
            patch.object(v, "atomic", side_effect=unavailable),
            patch.object(hashlib, "file_digest", side_effect=measured),
        ):
            result = self.verify(stage="build")
            self.assertEqual(result["status"], "success", result.get("error"))
            immutable_hashes = Counter(path for path in hashed if path.is_relative_to(self.prefix))
            self.assertGreater(len(immutable_hashes), 100)
            self.assertEqual(set(immutable_hashes.values()), {1})
            hashed.clear()
            self.assertEqual(self.verify(stage="build")["status"], "success")
            self.assertEqual(hashed, [], "fallback retains the ordinary stat-keyed hash cache")

    def test_malformed_compact_identity_is_revalidated(self):
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only"]),
        ):
            self.assertEqual(self.verify(stage="build")["status"], "success")
            [path] = list((v.cache_root() / "installations").glob("*.json"))
            old = v.read_json(path)
            old.update(sha256="invalid")
            old.pop("files")
            v.atomic(path, old)
            self.assertEqual(self.verify(stage="build")["status"], "success")
            self.assertEqual(len(v.read_json(path)["sha256"]), 64)
        self.assertEqual(v.read_json(path)["files"], 2)  # Lean and the local checker's native compiler.

    def test_unavailable_identity_cache_falls_back_to_complete_validation(self):
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(self.prefix): "publisher-1"})
        for error in (PermissionError("cache unavailable"), ValueError("unsafe cache")):
            with (
                self.subTest(error=error),
                patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
                patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only"]),
                patch.object(v, "cache_root", side_effect=error),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                verification = v.Verification(self.root, self.state, stage="build", reuse=False)
                first = verification.run()
                self.assertEqual(first["status"], "success", first.get("error"))
                self.assertIn(self.prefix, verification.installations.walked)
                self.assertNotIn(self.prefix, verification.installations.validated)
                write(self.prefix, "bin/lean", str(error))
                changed = self.verify(stage="build")
                self.assertEqual(changed["status"], "success", changed.get("error"))
                self.assertNotIn("reused_from", changed["phases"]["build"])

    def test_permanent_incomplete_does_not_suppress_inference(self):
        from tauceti_worker import loop

        write(self.root, ".git/shallow", self.base + "\n")
        result = self.verify(stage="lint")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["result_type"], "incomplete")
        self.assertNotIn("continuation_job", result)
        self.assertFalse(list((self.state / "verification-pending").glob("*.json")))
        with (
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "choose_model", side_effect=KeyboardInterrupt) as model,
        ):
            loop.cmd_loop(
                SimpleNamespace(), SimpleNamespace(state=self.state, wid="fixture"), only=["fix"], agent="claude"
            )
        model.assert_called_once()

    def test_default_all_runs_sources_once_before_build(self):
        result = self.verify(stage="all")
        self.assertEqual(result["status"], "success", result.get("error"))
        commands = result["commands"]
        sources = [i for i, c in enumerate(commands) if "sources" in c["argv"]]
        build = next(i for i, c in enumerate(commands) if c["argv"] == ["lake", "build", "--iofail"])
        self.assertEqual(len(sources), 1)
        self.assertLess(sources[0], build)

    def test_full_size_immutable_inventory_warm_cost_and_invalidation(self):
        from tauceti_worker.verification_installations import Installations

        # Thirty thousand files / 120 MiB: metadata and payload independent warm preparation.
        large = self.home / "immutable"
        payload = b"fixture source\n".ljust(4096, b" ")
        for directory in range(100):
            folder = large / str(directory)
            folder.mkdir(parents=True)
            for index in range(300):
                (folder / f"{index}.lean").write_bytes(payload)
        manifest = self.home / "installations.json"
        v.atomic(manifest, {str(large): "publisher-revision-1"})
        metrics = dict(
            files=0,
            bytes=0,
            walks=0,
            stats=0,
            statvfs=0,
            mountinfo_reads=0,
            mountinfo_bytes=0,
            hash_cache_bytes=0,
        )

        def files(root):
            metrics["walks"] += 1
            yield from v.files_under(root)

        def record(path):
            metrics["files"] += 1
            metrics["bytes"] += path.stat().st_size
            return v.installation_file_record(path)

        real_stat, real_lstat, real_read = os.stat, os.lstat, Path.read_text

        def readonly(path):
            metrics["statvfs"] += 1
            return SimpleNamespace(f_flag=os.ST_RDONLY)

        def read_text(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if str(path) == "/proc/self/mountinfo":
                metrics["mountinfo_reads"] += 1
                metrics["mountinfo_bytes"] += len(value.encode())
            if path.name == "verification-hashes-v2.json":
                metrics["hash_cache_bytes"] += len(value.encode())
            return value

        def counted(function):
            def call(path, *args, **kwargs):
                if str(path).startswith(str(large)):
                    metrics["stats"] += 1
                return function(path, *args, **kwargs)

            return call

        def installations(state=None):
            return Installations(
                state or self.state,
                digest=v.digest,
                atomic=v.atomic,
                read_json=v.read_json,
                files_under=files,
                file_record=record,
                stamps=v.stamps,
                cache_directory=lambda: v.cache_root() / "installations",
            )

        # Fixture-only read-only mount identity. Real mutable directories never take this path.
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("os.stat", side_effect=counted(real_stat)),
            patch("os.lstat", side_effect=counted(real_lstat)),
            patch("os.statvfs", side_effect=readonly),
            patch.object(Path, "read_text", read_text),
        ):
            installation = installations()
            cache = v._HashCache(self.state)
            token = v._HASH_CACHE.set(cache)
            try:
                cold = installation.inventory(large)
            finally:
                v._HASH_CACHE.reset(token)
            cache.save(installation.validated)
            self.assertEqual(v.read_json(cache.path)["directories"], [])
            original_cache_size = cache.path.stat().st_size
            self.assertEqual(metrics["bytes"], 122880000)
            cold_metrics = dict(metrics)
            metrics.update({key: 0 for key in metrics})
            # Unrelated historical receipts must have no effect on preparation.
            for index in range(2000):
                write(self.state, f"verification/history-{index}/receipt.json", "{}")

            def contender(index):
                installation = installations(self.home / f"worker-{index}-state")
                cache = v._HashCache(self.state)
                token = v._HASH_CACHE.set(cache)
                try:
                    identity = installation.inventory(large)
                    installation.validate()
                finally:
                    v._HASH_CACHE.reset(token)
                cache.save(installation.validated)
                return identity, installation

            with ThreadPoolExecutor(max_workers=20) as contenders:
                results = list(contenders.map(contender, range(20)))
            for identity, _installation in results:
                self.assertEqual(identity, cold)
            installation = results[-1][1]
            self.assertEqual(
                {key: metrics[key] for key in ("files", "bytes", "walks")}, dict(files=0, bytes=0, walks=0)
            )
            self.assertLessEqual(metrics["stats"], 120)
            self.assertEqual(metrics["statvfs"], 40)
            self.assertEqual(metrics["mountinfo_reads"], 40)
            self.assertLess(metrics["hash_cache_bytes"], 4096)
            print("IMMUTABLE_COST", json.dumps(dict(cold=cold_metrics, warm_twenty=dict(metrics)), sort_keys=True))
            v.atomic(manifest, {str(large): "publisher-revision-2"})
            changed = installations().inventory(large)
            self.assertNotEqual(cold, changed)
            self.assertEqual(metrics["files"], 30000)
            v.atomic(manifest, {str(large): "publisher-revision-3"})
            with self.assertRaisesRegex(ValueError, "provenance changed"):
                installation.validate()
            for index in range(3000):
                write(large, f"growth/{index}.lean", "additional immutable payload\n")
            larger = installations()
            cache = v._HashCache(self.state)
            token = v._HASH_CACHE.set(cache)
            try:
                grown = larger.inventory(large)
            finally:
                v._HASH_CACHE.reset(token)
            cache.save(larger.validated)
            self.assertEqual(grown["files"], 33000)
            self.assertEqual(cache.path.stat().st_size, original_cache_size)
            print(
                "IMMUTABLE_CACHE_GROWTH",
                json.dumps(dict(files=[30000, 33000], json_bytes=[original_cache_size, cache.path.stat().st_size])),
            )
        # A registration alone never trusts a writable installation.
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=None),
        ):
            self.assertNotIn("immutable_installation", installations().inventory(large))

    def test_readonly_mount_validation_includes_nested_mounts(self):
        from tauceti_worker.verification_installations import mount_identity

        nested = self.prefix / "nested directory"
        nested.mkdir()
        escaped = str(nested).replace(" ", r"\040")
        topology = f"1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:2 / {self.prefix} ro - ext4 /dev/tool ro\n3 2 8:3 / {escaped} rw - ext4 /dev/nested rw\n"
        with patch.object(Path, "read_text", return_value=topology), patch("os.statvfs") as filesystem:
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=0 if Path(path) == nested else os.ST_RDONLY)
            self.assertIsNone(mount_identity(self.prefix))
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=os.ST_RDONLY)
            self.assertEqual(len(mount_identity(self.prefix)), 3)
            filesystem.side_effect = lambda path: SimpleNamespace(f_flag=0)
            self.assertIsNone(mount_identity(self.prefix))

    def test_immutable_receipts_skip_walks_and_invalidate_provenance(self):
        from tauceti_worker import verification_tools

        deps = self.root / ".lake/packages"
        write(deps, "mathlib/Mathlib/A.lean", "dependency")
        manifest = self.home / "installations.json"
        tools = {str(verification_tools.root()): "tools-1"}
        v.atomic(manifest, {str(self.prefix): "lean-1", str(deps): "mathlib-1", **tools})
        with (
            patch.dict(os.environ, TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=str(manifest)),
            patch("tauceti_worker.verification_installations.mount_identity", return_value=["fixture-read-only-mount"]),
        ):
            first = self.verify(stage="build")
            self.assertEqual(first["status"], "success", first.get("error"))
            with (
                patch("tauceti_worker.verification.files_under", side_effect=AssertionError("warm immutable walk")),
                patch.dict(os.environ, TAUCETI_VERIFY_BUBBLE="1"),
            ):
                second = self.verify(stage="build")
            self.assertIn("reused_from", second["phases"]["build"])
            self.assertEqual(self.verify(stage="all")["status"], "success")
            write(self.prefix, "bin/lean", "publisher replaced toolchain")
            v.atomic(manifest, {str(self.prefix): "lean-2", str(deps): "mathlib-1", **tools})
            third = self.verify(stage="build")
            self.assertNotIn("reused_from", third["phases"]["build"])

    def test_v0_dispatch_default_and_explicit_opt_in(self):
        from tauceti_worker.agents import local_verification

        with patch.dict(os.environ):
            os.environ.pop("TAUCETI_VERIFY_HELPER", None)
            self.assertNotIn("tauceti-verify", local_verification("ordinary"))
            for value in ("", "false", "no", "0"):
                os.environ["TAUCETI_VERIFY_HELPER"] = value
                self.assertNotIn("tauceti-verify", local_verification("ordinary"))
            os.environ["TAUCETI_VERIFY_HELPER"] = "1"
            self.assertIn("tauceti-verify", local_verification("ordinary"))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(Recovery(name) for name in Recovery.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()

"""Native Lake cache restoration and safe publication; real Lean fixtures are tiny and offline."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cache_storage
from tauceti_worker import expanded_cache as cache
from tauceti_worker.runtime_status import atomic_json


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
        cache_storage.initialize(self.pool)

    def test_shared_preparation_fetches_before_hydration_finishes(self):
        from tauceti_worker.agents import prepare_host_build_caches

        events = []
        env = dict(LAKE_CACHE_DIR=str(self.root / "private"))
        warm_env = dict(env, LAKE_CACHE_DIR=str(self.root / "downloads"))

        @contextlib.contextmanager
        def preparation(cwd, environment, *, refetch, report=None):
            self.assertEqual(cwd, self.root)
            self.assertIs(environment, env)
            events.append("prepare")
            yield warm_env
            events.append("hydrate")

        def warm(cwd, environment, logdir, *, report=None):
            self.assertIs(environment, warm_env)
            events.append("fetch")

        with (
            patch.object(cache, "preparation", preparation),
            patch("tauceti_worker.agents.warm_host_build_caches", side_effect=warm),
        ):
            prepare_host_build_caches(self.root, env, self.root / "logs")
        self.assertEqual(events, ["prepare", "fetch", "hydrate"])

    def test_reject_unsafe_and_incomplete_descriptions(self):
        for data in [{}, {"o": ["../../foreign"], "i": "a" * 16 + ".ilean"}, {"o": ["a" * 16 + ".olean"]}]:
            with self.assertRaises(ValueError):
                cache.descriptions(data)

    def test_hydration_uses_shared_inodes_and_private_metadata(self):
        entry = self.pool / "entries" / ("a" * 16)
        entry.mkdir(parents=True)
        data = dict(o=["b" * 16 + ".olean"], i="c" * 16 + ".ilean", m=False)
        for name in cache.descriptions(data):
            (entry / name).write_bytes(b"cached")
        atomic_json(entry / "entry.json", dict(data=data))
        for n in (1, 2):
            cache.hydrate(entry, self.root / str(n), "d" * 16)
        artifact = data["o"][0]
        self.assertEqual(
            (self.root / "1/artifacts" / artifact).stat().st_ino, (self.root / "2/artifacts" / artifact).stat().st_ino
        )
        one = self.root / "1/outputs/TauCeti" / ("d" * 16 + ".json")
        two = self.root / "2/outputs/TauCeti" / ("d" * 16 + ".json")
        self.assertNotEqual(one.stat().st_ino, two.stat().st_ino)
        before = two.stat().st_mtime_ns
        with patch.object(cache, "atomic_json", side_effect=AssertionError("rewrote warm metadata")):
            cache.hydrate(entry, self.root / "2", "d" * 16)
        self.assertEqual(two.stat().st_mtime_ns, before)
        one.write_text("{}")
        self.assertEqual(json.loads(two.read_text())["data"], data)
        # A later preparation must repair mutable metadata and deleted artifacts.
        (self.root / "1/artifacts" / artifact).unlink()
        cache.hydrate(entry, self.root / "1", "d" * 16)
        self.assertEqual(json.loads(one.read_text())["data"], data)
        self.assertEqual((self.root / "1/artifacts" / artifact).read_bytes(), b"cached")

    def test_download_context_never_promotes_agent_outputs(self):
        private = self.root / "private"
        (private / "outputs/TauCeti").mkdir(parents=True)
        atomic_json(
            private / "outputs/TauCeti" / ("a" * 16 + ".json"),
            dict(data=dict(o=["b" * 16 + ".olean"], i="c" * 16 + ".ilean")),
        )
        env = dict(LAKE_CACHE_DIR=str(private), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with cache.preparation(self.root, env) as warm:
            fresh = Path(warm["LAKE_CACHE_DIR"])
            self.assertNotEqual(fresh, private)
            self.assertFalse((fresh / "outputs").exists())
        self.assertFalse(list((self.pool / "entries").glob("*")))

    def test_corrupt_local_archive_is_refetched_once_without_manual_cleanup(self):
        private = self.root / "private"
        name = "a" * 16 + ".ltar"
        (private / "artifacts").mkdir(parents=True)
        (private / "artifacts" / name).write_bytes(b"bad")
        env = dict(LAKE_CACHE_DIR=str(private), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))

        def refetch(warm):
            self.assertFalse((private / "artifacts" / name).exists())
            (Path(warm["LAKE_CACHE_DIR"]) / "artifacts" / name).write_bytes(b"good")

        with patch.object(cache, "expand", side_effect=[cache.CorruptArchive(name), None]) as expansion:
            with cache.preparation(self.root, env, refetch=refetch) as warm:
                atomic_json(Path(warm["LAKE_CACHE_DIR"]) / "outputs/TauCeti" / ("b" * 16 + ".json"), dict(data=name))
        self.assertEqual(expansion.call_count, 2)
        self.assertEqual((private / "artifacts" / name).read_bytes(), b"good")

    def test_transfer_does_not_mirror_unreferenced_seed_archives(self):
        pool_archive = self.pool / "archives" / ("a" * 16 + ".ltar")
        pool_archive.parent.mkdir(parents=True)
        pool_archive.write_bytes(b"unneeded")
        private = self.root / "private"
        env = dict(LAKE_CACHE_DIR=str(private), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with patch.object(cache, "expand"):
            with cache.preparation(self.root, env) as warm:
                download = Path(warm["LAKE_CACHE_DIR"])
                wanted = "b" * 16 + ".ltar"
                (download / "artifacts" / wanted).write_bytes(b"needed")
                atomic_json(download / "outputs/TauCeti" / ("c" * 16 + ".json"), dict(data=wanted))
        self.assertFalse((private / "artifacts" / pool_archive.name).exists())
        self.assertEqual((private / "artifacts" / wanted).read_bytes(), b"needed")

    def test_interrupted_fetch_preserves_good_private_mapping(self):
        private = self.root / "private"
        key = "a" * 16
        good = dict(schemaVersion=cache.SCHEMA, data="b" * 16 + ".ltar")
        target = private / "outputs/TauCeti" / (key + ".json")
        atomic_json(target, good)
        env = dict(LAKE_CACHE_DIR=str(private), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with self.assertRaisesRegex(OSError, "interrupted fetch"):
            with cache.preparation(self.root, env) as warm:
                partial = Path(warm["LAKE_CACHE_DIR"]) / "outputs/TauCeti" / target.name
                partial.parent.mkdir(parents=True)
                partial.write_text('{"data":')
                raise OSError("interrupted fetch")
        self.assertEqual(json.loads(target.read_text()), good)

    def test_disappearing_seed_is_an_individual_cache_miss(self):
        archives = self.pool / "archives"
        archives.mkdir(exist_ok=True)
        vanished = archives / ("a" * 16 + ".ltar")
        good = archives / ("b" * 16 + ".ltar")
        vanished.write_bytes(b"vanished")
        good.write_bytes(b"good")
        private = self.root / "private"
        env = dict(LAKE_CACHE_DIR=str(private), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        real_link = cache.link

        def gc_race(source, target):
            if source == vanished:
                raise FileNotFoundError(source)
            real_link(source, target)

        with patch.object(cache, "link", side_effect=gc_race):
            with cache.preparation(self.root, env) as warm:
                download = Path(warm["LAKE_CACHE_DIR"])
                self.assertNotEqual(download, private)
                self.assertEqual((download / "artifacts" / good.name).read_bytes(), b"good")
                self.assertFalse((download / "artifacts" / vanished.name).exists())

    def test_preparation_lock_contention_fails_instead_of_private_fallback(self):
        from contextlib import contextmanager

        @contextmanager
        def busy(*args, **kwargs):
            raise BlockingIOError("busy")
            yield

        env = dict(LAKE_CACHE_DIR=str(self.root / "private"), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with patch.object(cache, "locked", busy):
            with self.assertRaises(cache.Unavailable):
                with cache.preparation(self.root, env):
                    self.fail("must not run the round on a private cache")

    def test_entry_lock_contention_waits_then_fails_at_the_deadline(self):
        import fcntl
        import time

        download = self.root / "download"
        atomic_json(download / "outputs/TauCeti" / ("b" * 16 + ".json"), dict(data="a" * 16 + ".ltar"))
        with cache.locked(self.pool, "a" * 16):
            started = time.monotonic()
            with self.assertRaisesRegex(cache.Unavailable, "still locked"):
                cache.expand(
                    download,
                    self.root / "private",
                    self.root,
                    {},
                    self.pool,
                    dict(hits=0, expanded=0),
                    deadline=time.monotonic() + 0.5,
                )
            self.assertGreaterEqual(time.monotonic() - started, 0.4)
        # Released by a concurrent publisher before the deadline: wait, then hydrate its entry.
        entry = self.pool / "entries" / ("a" * 16)
        data = dict(o=["c" * 16 + ".olean"], i="d" * 16 + ".ilean")
        for name in cache.descriptions(data):
            (entry / name).parent.mkdir(parents=True, exist_ok=True)
            (entry / name).write_bytes(b"x")
        atomic_json(entry / "entry.json", dict(data=data))
        path = next((self.pool / "locks").glob("entry-*.lock"))
        holder = path.open("a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        import threading

        threading.Timer(0.3, holder.close).start()
        report = dict(hits=0, expanded=0)
        cache.expand(download, self.root / "private", self.root, {}, self.pool, report, deadline=time.monotonic() + 10)
        self.assertEqual(report["hits"], 1)

    def warm_entries(self, keys, download):
        for index, key in enumerate(keys):
            entry = self.pool / "entries" / key
            data = dict(o=[f"{index:016x}.olean"], i=f"{index:016x}.ilean")
            entry.mkdir(parents=True)
            for name in cache.descriptions(data):
                (entry / name).write_bytes(key.encode())
            atomic_json(entry / "entry.json", dict(data=data))
            atomic_json(download / "outputs/TauCeti" / (f"{index + 1:016x}" + ".json"), dict(data=key + ".ltar"))

    def test_entry_lock_shard_collisions_within_a_batch_do_not_self_deadlock(self):
        import time

        keys = ["0000000000000036", "000000000000005b"]
        self.assertEqual(cache.lock_path(self.pool, keys[0]), cache.lock_path(self.pool, keys[1]))
        download = self.root / "download"
        self.warm_entries(keys, download)
        report = dict(hits=0, expanded=0)
        started = time.monotonic()
        cache.expand(download, self.root / "private", self.root, {}, self.pool, report, deadline=started + 5)
        self.assertEqual(report["hits"], 2)
        self.assertLess(time.monotonic() - started, 2)

    def test_reversed_batches_on_two_workers_do_not_deadlock(self):
        import threading
        import time

        keys = [f"{n:016x}" for n in range(0, 4000, 7)][:128]
        forward, backward = self.root / "forward", self.root / "backward"
        self.warm_entries(keys, forward)
        for index, key in enumerate(reversed(keys)):
            atomic_json(backward / "outputs/TauCeti" / (f"{index + 1:016x}" + ".json"), dict(data=key + ".ltar"))
        reports, errors = {}, []

        def run(download):
            try:
                report = reports[download.name] = dict(hits=0, expanded=0)
                for _ in range(5):
                    cache.expand(
                        download,
                        self.root / ("private-" + download.name),
                        self.root,
                        {},
                        self.pool,
                        report,
                        deadline=time.monotonic() + 10,
                    )
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        threads = [threading.Thread(target=run, args=(d,)) for d in (forward, backward)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({r["hits"] for r in reports.values()}, {5 * len(keys)})

    def test_refetch_failure_after_corrupt_archive_fails_preparation(self):
        from tauceti_worker.config import NoProgress

        env = dict(LAKE_CACHE_DIR=str(self.root / "private"), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with patch.object(cache, "expand", side_effect=cache.CorruptArchive("a" * 16 + ".ltar")):
            with self.assertRaisesRegex(cache.Unavailable, "offline"):
                with cache.preparation(self.root, env, refetch=lambda _: (_ for _ in ()).throw(NoProgress("offline"))):
                    pass

    def test_hydration_space_error_preserves_good_pool_entry(self):
        entry = self.pool / "entries" / ("a" * 16)
        entry.mkdir(parents=True)
        atomic_json(entry / "entry.json", dict(data={}))
        download = self.root / "download"
        atomic_json(download / "outputs/TauCeti" / ("b" * 16 + ".json"), dict(data="a" * 16 + ".ltar"))
        with (
            patch.object(cache, "hydrate", side_effect=OSError("no space")),
            patch.object(cache, "native", side_effect=AssertionError("must not expand good entry")),
        ):
            with self.assertRaisesRegex(OSError, "no space"):
                cache.expand(download, self.root / "private", self.root, {}, self.pool, dict(hits=0, expanded=0))
        self.assertTrue((entry / "entry.json").is_file())

    def test_link_failure_raises_instead_of_copying(self):
        import errno

        source = self.root / "source"
        source.write_bytes(b"bytes")
        for code in (errno.EXDEV, errno.EPERM):
            target = self.root / f"target-{code}"
            with patch.object(cache.os, "link", side_effect=OSError(code, os.strerror(code))):
                with self.assertRaises(cache.SharedLinkUnavailable):
                    cache.link(source, target)
            self.assertFalse(target.exists())
        cache.link(source, self.root / "linked")
        cache.link(source, self.root / "linked")  # An existing link is success.
        self.assertEqual((self.root / "linked").stat().st_ino, source.stat().st_ino)
        with self.assertRaises(FileNotFoundError):
            cache.link(self.root / "vanished", self.root / "never")

    def test_native_helper_failure_fails_partial_expansion(self):
        download = self.root / "download"
        for key, archive in (("b", "a"), ("c", "e")):
            atomic_json(download / "outputs/TauCeti" / (key * 16 + ".json"), dict(data=archive * 16 + ".ltar"))
        entry = self.pool / "entries" / ("a" * 16)
        data = dict(o=["c" * 16 + ".olean"], i="d" * 16 + ".ilean")
        for name in cache.descriptions(data):
            (entry / name).parent.mkdir(parents=True, exist_ok=True)
            (entry / name).write_bytes(b"x")
        atomic_json(entry / "entry.json", dict(data=data))
        # One archive hydrates from the pool; the other needs the toolchain's hash helper.
        with (
            patch.object(cache, "native", side_effect=OSError("helper unavailable")),
            self.assertRaisesRegex(OSError, "helper unavailable"),
        ):
            cache.expand(download, self.root / "private", self.root, {}, self.pool, dict(hits=0, expanded=0))
        env = dict(LAKE_CACHE_DIR=str(self.root / "private"), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        with patch.object(cache, "native", side_effect=OSError("helper unavailable")):
            with self.assertRaisesRegex(cache.Unavailable, "helper unavailable"):
                with cache.preparation(self.root, env) as warm:
                    shutil.copytree(download / "outputs", Path(warm["LAKE_CACHE_DIR"]) / "outputs")

    def test_preparation_requires_one_filesystem(self):
        env = dict(LAKE_CACHE_DIR=str(self.root / "private"), TAUCETI_BUILD_CACHE_ROOT=str(self.pool))
        real_stat = Path.stat

        def elsewhere(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == self.root / "private" or path == self.root:
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 1, *tuple(info)[3:]))
            return info

        (self.root / "private").mkdir()
        with patch.object(Path, "stat", elsewhere):
            with self.assertRaisesRegex(cache.SharedLinkUnavailable, "different filesystems"):
                with cache.preparation(self.root, env):
                    self.fail("must not prepare across filesystems")

    @unittest.skipUnless(shutil.which("lean") and shutil.which("lake"), "Lean/Lake needed for native fixture")
    def test_real_lake_restores_without_unpack_and_rebuild_preserves_other_worker(self):
        prefix = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip())
        if not (prefix / "bin/leantar").is_file():
            self.skipTest("toolchain has no leantar")
        project = self.root / "project"
        project.mkdir()
        (project / "lakefile.toml").write_text('name = "cache_fixture"\n[[lean_lib]]\nname = "CacheFixture"\n')
        source = project / "CacheFixture.lean"
        source.write_text("module\npublic def answer : Nat := 42\n")
        env = dict(
            os.environ,
            PATH=str(prefix / "bin") + ":" + os.environ["PATH"],
            LAKE_ARTIFACT_CACHE="0",
            LAKE_RESTORE_ARTIFACTS="1",
        )

        def lake(*args, **extra):
            return subprocess.run(
                [str(prefix / "bin/lake"), *args],
                cwd=project,
                env={**env, **extra},
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).stdout

        lake("build", "+CacheFixture:ltar")
        archive = next((project / ".lake/build").rglob("*.ltar"))
        trace = json.loads(next((project / ".lake/build").rglob("*.trace")).read_text())
        input_hash = trace["depHash"]
        program = cache.native(project, env, self.pool)
        archive_hash = cache.hashes(program, [archive], self.root)[str(archive)]
        download = self.root / "download"
        (download / "artifacts").mkdir(parents=True)
        shutil.copyfile(archive, download / "artifacts" / (archive_hash + ".ltar"))
        atomic_json(download / "outputs/TauCeti" / (input_hash + ".json"), dict(data=archive_hash + ".ltar"))
        atomic_json(download / "outputs/TauCeti" / ("e" * 16 + ".json"), dict(data=archive_hash + ".ltar"))
        first = self.root / "first"
        second = self.root / "second"
        report = dict(hits=0, expanded=0)
        cache.expand(download, first, project, env, self.pool, report)
        self.assertEqual((report["hits"], report["expanded"]), (0, 1))
        self.assertTrue((first / "outputs/TauCeti" / ("e" * 16 + ".json")).exists())
        with (
            patch.object(cache, "native", side_effect=AssertionError("warm hit needs no helper")),
            patch.object(cache.subprocess, "run", side_effect=AssertionError("warm hit must not unpack")),
        ):
            report = dict(hits=0, expanded=0)
            cache.expand(download, second, project, env, self.pool, report)
        self.assertEqual((report["hits"], report["expanded"]), (1, 0))
        # Use the actual project scope to ask Lake to restore this mapping.
        mapping = second / "outputs/TauCeti" / (input_hash + ".json")
        scoped = second / "outputs/cache_fixture" / mapping.name
        scoped.parent.mkdir(parents=True)
        shutil.copyfile(mapping, scoped)
        shutil.rmtree(project / ".lake/build")
        result = lake("build", "CacheFixture", LAKE_CACHE_DIR=str(second), LAKE_ARTIFACT_CACHE="1")
        self.assertNotIn("Unpacked", result)
        self.assertNotIn("Built CacheFixture", result)
        outputs = json.loads(mapping.read_text())["data"]
        shared = first / "artifacts" / outputs["o"][0]
        before = shared.read_bytes()
        source.write_text("module\npublic def changedAnswer : Nat := 43\n")
        lake("build", "CacheFixture", LAKE_CACHE_DIR=str(second), LAKE_ARTIFACT_CACHE="1")
        self.assertEqual(shared.read_bytes(), before)
        self.assertNotEqual((project / ".lake/build/lib/lean/CacheFixture.olean").read_bytes(), before)
        # A modified/truncated compressed artifact is never admitted to the pool.
        bad = self.root / "bad"
        shutil.copytree(download, bad)
        (bad / "artifacts" / (archive_hash + ".ltar")).unlink()
        (bad / "artifacts" / (archive_hash + ".ltar")).write_bytes(b"corrupt")
        shutil.rmtree(self.pool / "entries" / archive_hash)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            cache.expand(bad, self.root / "third", project, env, self.pool, dict(hits=0, expanded=0))


if __name__ == "__main__":
    unittest.main()

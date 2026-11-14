"""Canonical build outputs: one inode per host, sha256-checked convergence, and safe eviction."""

import errno
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cache_storage
from tauceti_worker import canonical_store as canonical
from tauceti_worker import expanded_cache as cache


def lake_hash(data):
    return hashlib.sha256(data).hexdigest()[:16]


def fake_hashes(program, paths, directory):
    return {str(p): lake_hash(Path(p).read_bytes()) for p in paths}


def module(checkout, name, seed, *, setup=True):
    """A completed module: trace outputs name each file by its (fake) Lake hash."""
    build = checkout / ".lake/build"
    parts = name.split(".")
    lib = build / "lib/lean" / Path(*parts[:-1]) if len(parts) > 1 else build / "lib/lean"
    ir = build / "ir" / Path(*parts[:-1]) if len(parts) > 1 else build / "ir"
    lib.mkdir(parents=True, exist_ok=True)
    ir.mkdir(parents=True, exist_ok=True)
    stem = parts[-1]
    outputs = {}
    for key, extension, directory in (
        ("o", "olean", lib),
        ("o", "olean.server", lib),
        ("i", "ilean", lib),
        ("rs", "ir.sig", lib),
        ("c", "c", ir),
    ):
        data = f"{seed}:{name}:{extension}".encode() * 64
        descriptor = lake_hash(data) + "." + extension
        path = directory / (stem + "." + extension)
        path.unlink(missing_ok=True)  # Like Lake: replace, never write a (possibly shared) inode.
        path.write_bytes(data)
        if key == "o":
            outputs.setdefault("o", []).append(descriptor)
        else:
            outputs[key] = descriptor
    (lib / (stem + ".trace")).write_text(json.dumps(dict(depHash="0" * 16, outputs=outputs, log=[])))
    if setup:
        (ir / (stem + ".setup.json")).write_text("{}" * 1000)
    return outputs


def paths(checkout):
    modules, _ = canonical.outputs(checkout / ".lake/build")
    return [pair for pairs in modules.values() for pair in pairs]


class Canonical(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
        # Never let a default root reach the host's real pool or activity store.
        isolation = patch.dict(
            os.environ,
            {
                "TAUCETI_BUILD_CACHE_ROOT": str(self.pool),
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "mathlib-builds"),
                "HOME": str(self.root / "home"),
                # Toolchains stay readable (elan resolves them from ELAN_HOME, not HOME).
                "ELAN_HOME": os.environ.get("ELAN_HOME") or str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".elan"),
            },
        )
        isolation.start()
        self.addCleanup(isolation.stop)
        from tauceti_worker import activity

        assert cache.root() == self.pool and activity.root() == self.root / "activity", "unisolated test"
        cache_storage.initialize(self.pool)
        if self._testMethodName.startswith("test_real_lake"):
            return  # The real toolchain computes the Lake hashes.
        for target, value in (("native", lambda *a: Path("helper")), ("hashes", fake_hashes)):
            patcher = patch.object(cache, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def checkout(self, name, *modules, seed="same"):
        checkout = self.root / name
        for mod in modules:
            module(checkout, mod, seed)
        return checkout

    def relink(self, checkout, **kwargs):
        return canonical.relink(checkout, self.root / (checkout.name + "-store"), self.pool, **kwargs)

    def assert_one_inode(self, *checkouts):
        objects = canonical.store(self.pool) / "objects"
        for path, name in paths(checkouts[0]):
            inodes = {(c / path.relative_to(checkouts[0])).stat().st_ino for c in checkouts}
            self.assertEqual(inodes, {(objects / name).stat().st_ino}, path)

    def test_missing_or_empty_build_is_a_no_op(self):
        self.assertEqual(self.relink(self.root / "absent"), {})
        (self.root / "empty/.lake/build").mkdir(parents=True)
        self.assertEqual(self.relink(self.root / "empty"), {})
        self.assertFalse((self.pool / canonical.NAMESPACE).exists())

    def test_concurrent_identical_outputs_converge_on_one_inode(self):
        modules = [f"TauCeti.M{i}" for i in range(20)]
        checkouts = [self.checkout(f"w{n}", *modules) for n in range(4)]
        self.assertNotEqual(
            (checkouts[0] / ".lake/build/lib/lean/TauCeti/M0.olean").stat().st_ino,
            (checkouts[1] / ".lake/build/lib/lean/TauCeti/M0.olean").stat().st_ino,
        )
        barrier = threading.Barrier(len(checkouts))
        errors = []

        def run(checkout):
            try:
                barrier.wait()
                self.relink(checkout)
            except Exception as error:  # noqa: BLE001 - reported by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=run, args=(c,)) for c in checkouts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assert_one_inode(*checkouts)
        meta = json.loads(next((canonical.store(self.pool) / "meta").rglob("*.json")).read_text())
        self.assertEqual(meta["provenance"], "local")
        self.assertEqual(len(meta["sha256"]), 64)

    def test_losing_publisher_converges_on_the_winner(self):
        first = self.checkout("first", "TauCeti.A")
        second = self.checkout("second", "TauCeti.A")
        objects = canonical.store(self.pool) / "objects"
        real_link = os.link
        raced = []

        def concurrent_winner(source, target, **kwargs):
            target = Path(target)
            if target.parent == objects and not raced:
                raced.append(target)
                real_link(first / Path(source).relative_to(second), target)  # the other worker won
            return real_link(source, target, **kwargs)

        with patch.object(canonical.os, "link", concurrent_winner):
            report = self.relink(second)
        self.assertTrue(raced)
        self.assertGreaterEqual(report.get("relinked", 0), 1)
        winner = objects / raced[0].name
        path = next(p for p, n in paths(second) if n == raced[0].name)
        self.assertEqual(path.stat().st_ino, winner.stat().st_ino)
        self.assertEqual(winner.stat().st_ino, (first / path.relative_to(second)).stat().st_ino)

    def test_stale_bytes_beside_an_old_trace_are_rebuilt_not_shared(self):
        good = self.checkout("good", "TauCeti.A", "TauCeti.B")
        self.relink(good)
        bad = self.checkout("bad", "TauCeti.A", "TauCeti.B")
        # An interrupted rebuild: Lake wrote new bytes, then died before replacing the old trace.
        target = bad / ".lake/build/lib/lean/TauCeti/A.olean"
        target.write_bytes(bytes(reversed(target.read_bytes())))
        report = self.relink(bad)
        self.assertEqual(report["invalidated_modules"], 1)
        lib = bad / ".lake/build/lib/lean/TauCeti"
        self.assertFalse((lib / "A.trace").exists())
        self.assertFalse(target.exists())
        # Verified siblings are never deleted; they converge like any other output.
        self.assertEqual((lib / "A.ilean").stat().st_ino, (good / ".lake/build/lib/lean/TauCeti/A.ilean").stat().st_ino)
        # The canonical object is untouched and the other module still converged.
        objects = canonical.store(self.pool) / "objects"
        name = next(n for p, n in paths(good) if p.name == "A.olean")
        self.assertEqual((objects / name).read_bytes(), (good / ".lake/build/lib/lean/TauCeti/A.olean").read_bytes())
        self.assertEqual((lib / "B.olean").stat().st_ino, (good / ".lake/build/lib/lean/TauCeti/B.olean").stat().st_ino)
        # The next round rebuilds the module (here: rewrites it) and relinks it normally.
        module(bad, "TauCeti.A", "same")
        report = self.relink(bad)
        self.assertNotIn("invalidated_modules", report)
        self.assert_one_inode(good, bad)

    def test_corrupted_canonical_object_is_evicted_and_republished(self):
        good = self.checkout("good", "TauCeti.A")
        self.relink(good)
        objects = canonical.store(self.pool) / "objects"
        name = next(n for p, n in paths(good) if p.name == "A.olean")
        # Someone wrote the shared inode in place: every link now sees the bad bytes.
        os.chmod(objects / name, 0o644)
        (objects / name).write_bytes(b"x" * (objects / name).stat().st_size)
        fresh = self.checkout("fresh", "TauCeti.A")
        report = self.relink(fresh)
        self.assertEqual(report["integrity_failures"], [name])
        olean = fresh / ".lake/build/lib/lean/TauCeti/A.olean"
        self.assertEqual((objects / name).stat().st_ino, olean.stat().st_ino)
        self.assertEqual(lake_hash(olean.read_bytes()), name.split(".")[0])
        # The checkout still linking the corrupt inode is invalidated at its next relink.
        report = self.relink(good)
        self.assertEqual(report["invalidated_modules"], 1)
        self.assertFalse((good / ".lake/build/lib/lean/TauCeti/A.trace").exists())

    def test_warm_in_place_corruption_is_noticed_by_the_fingerprint(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        objects = canonical.store(self.pool) / "objects"
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        self.assertEqual(olean.stat().st_ino, (objects / name).stat().st_ino)
        os.chmod(olean, 0o644)
        olean.write_bytes(b"x" * olean.stat().st_size)  # Writes the shared inode through the checkout.
        os.utime(olean, (time.time() - 60,) * 2)
        report = self.relink(checkout)
        self.assertEqual(report["integrity_failures"], [name])
        self.assertEqual(report["invalidated_modules"], 1)
        self.assertFalse((objects / name).exists())
        self.assertFalse(olean.exists())

    def test_unverifiable_outputs_are_removed_not_kept_private(self):
        checkout = self.checkout("w", "TauCeti.A")
        target = checkout / ".lake/build/lib/lean/TauCeti/A.ilean"
        target.write_bytes(b"not what the trace hashed")
        name = next(n for p, n in paths(checkout) if p == target)
        report = self.relink(checkout)
        self.assertEqual(report["invalidated_modules"], 1)
        self.assertFalse((canonical.store(self.pool) / "objects" / name).exists())
        self.assertFalse(target.exists())
        self.assertFalse((checkout / ".lake/build/lib/lean/TauCeti/A.trace").exists())

    def test_busy_or_changed_stale_files_are_never_deleted(self):
        good = self.checkout("good", "TauCeti.A")
        self.relink(good)
        for case in ("open", "changed"):
            bad = self.checkout("bad-" + case, "TauCeti.A")
            target = bad / ".lake/build/lib/lean/TauCeti/A.olean"
            target.write_bytes(bytes(reversed(target.read_bytes())))
            if case == "open":
                stream = open(target, "r+b")
                self.addCleanup(stream.close)
            else:
                os.utime(target, (time.time() + 3600,) * 2)
            report = self.relink(bad)
            self.assertNotIn("invalidated_modules", report, case)
            self.assertTrue(target.exists() and (target.parent / "A.trace").exists(), case)
            self.assertGreaterEqual(report["deferred_busy"], 1, case)

    def test_damaged_pool_copy_never_deletes_a_verified_checkout_output(self):
        objects = canonical.store(self.pool) / "objects"
        (self.pool / "artifacts").mkdir()
        # Pool damaged, checkout good: quarantine the pool copy, publish the checkout's.
        good = self.checkout("good", "TauCeti.A")
        olean, name = next((p, n) for p, n in paths(good) if p.name == "A.olean")
        (self.pool / "artifacts" / name).write_bytes(b"damaged pool copy")
        report = self.relink(good)
        self.assertEqual(report["pool_quarantined"], 1)
        self.assertEqual((objects / name).stat().st_ino, olean.stat().st_ino)
        self.assertNotIn("invalidated_modules", report)
        # Pool good, checkout damaged: adopt the pool copy, remove only the damaged file.
        other = self.checkout("other", "TauCeti.B", seed="b")
        b_olean, b_name = next((p, n) for p, n in paths(other) if p.name == "B.olean")
        shutil.copyfile(b_olean, self.pool / "artifacts" / b_name)
        b_olean.write_bytes(b"damaged checkout copy")
        report = self.relink(other)
        self.assertEqual(report["published_ci"], 1)
        self.assertEqual(report["invalidated_modules"], 1)
        self.assertFalse(b_olean.exists())
        self.assertTrue((b_olean.parent / "B.ilean").exists())
        # Neither verifies: leave both and count it.
        third = self.checkout("third", "TauCeti.C", seed="c")
        c_olean, c_name = next((p, n) for p, n in paths(third) if p.name == "C.olean")
        c_olean.write_bytes(b"damaged checkout copy")
        (self.pool / "artifacts" / c_name).write_bytes(b"damaged pool copy")
        report = self.relink(third)
        self.assertEqual(report["unverifiable"], 1)
        self.assertTrue(c_olean.exists() and (self.pool / "artifacts" / c_name).exists())
        self.assertFalse((objects / c_name).exists())

    def test_limited_passes_make_strict_progress_to_zero(self):
        modules = [f"TauCeti.M{i}" for i in range(12)]
        checkout = self.checkout("w", *modules)
        objects = canonical.store(self.pool) / "objects"

        def unshared():
            return sum(1 for p, n in paths(checkout) if not (objects / n).exists())

        remaining = [unshared()]
        for _ in range(20):
            report = self.relink(checkout, limit=7)
            remaining.append(unshared())
            if not report.get("incomplete"):
                break
        self.assertEqual(remaining[-1], 0)
        self.assertTrue(all(a > b for a, b in zip(remaining, remaining[1:], strict=False)), remaining)
        self.assertGreater(len(remaining), 3)  # It really took several bounded passes.
        self.assertEqual(report["setup_removed"], 12)

    def test_sweep_skips_busy_shards_instead_of_waiting(self):
        from tauceti_worker import retention

        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        (self.pool / "artifacts").mkdir(exist_ok=True)
        shutil.copyfile(olean, self.pool / "artifacts" / name)  # A second inode to reconcile.
        directory = canonical.store(self.pool)
        with canonical.object_lock(directory, name, exclusive=True):
            started = time.monotonic()
            canonical.sweep(self.pool, retention.fresh_report(time.time()), lifetime=86400, remove=retention.remove)
            self.assertLess(time.monotonic() - started, 5)
        self.assertNotEqual((self.pool / "artifacts" / name).stat().st_ino, olean.stat().st_ino)
        canonical.sweep(self.pool, retention.fresh_report(time.time()), lifetime=86400, remove=retention.remove)
        self.assertEqual((self.pool / "artifacts" / name).stat().st_ino, olean.stat().st_ino)

    def test_files_still_open_for_writing_or_changed_are_not_published(self):
        checkout = self.checkout("w", "TauCeti.A")
        found = paths(checkout)
        held = found[0][0]
        stream = open(held, "r+b")  # A writer the process scan cannot see.
        self.addCleanup(stream.close)
        later = found[1][0]
        os.utime(later, (time.time() + 3600,) * 2)  # Changed after the boundary began.
        report = self.relink(checkout)
        objects = canonical.store(self.pool) / "objects"
        self.assertEqual(report["deferred_busy"], 2)
        self.assertFalse((objects / found[0][1]).exists())
        self.assertFalse((objects / found[1][1]).exists())
        self.assertTrue(held.exists() and later.exists())
        self.assertEqual(report["published_local"], len(found) - 2)

    def test_budget_and_host_slots_bound_relinking_and_it_resumes(self):
        modules = [f"TauCeti.M{i}" for i in range(4)]
        checkout = self.checkout("w", *modules)
        report = self.relink(checkout, budget=0)
        self.assertTrue(report["incomplete"])
        self.assertNotIn("setup_removed", report)  # Setup files go only with a complete pass.
        self.assertNotIn("published_local", report)
        directory = canonical.store(self.pool)
        with canonical.slot(directory, count=1, wait=0) as held:
            self.assertTrue(held)
            report = self.relink(checkout, slots=1, slot_wait=0.2)
            self.assertIn("relinks already running", report["skipped"])
        report = self.relink(checkout, slots=1)
        self.assertEqual(report["published_local"], len(paths(checkout)))
        self.assertEqual(report["setup_removed"], 4)

    def test_legacy_copy_collected_mid_adoption_is_a_retryable_miss(self):
        checkout = self.checkout("w", "TauCeti.A")
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        legacy = self.pool / "artifacts" / name
        legacy.parent.mkdir(parents=True)
        shutil.copyfile(olean, legacy)
        real_digest = canonical.digest

        def collected(path, deadline=None):
            if Path(path) == legacy:
                legacy.unlink()  # Pool GC won the race.
                raise FileNotFoundError(path)
            return real_digest(path, deadline)

        with patch.object(canonical, "digest", collected):
            report = self.relink(checkout)
        # A lost race is a retry, not a failure: the checkout's verified bytes are published.
        self.assertEqual(report["published_local"], len(paths(checkout)))
        self.assertEqual((canonical.store(self.pool) / "objects" / name).stat().st_ino, olean.stat().st_ino)

    def test_legacy_gc_waits_for_the_object_lock(self):
        name = "a" * 16 + ".olean"
        legacy = self.pool / "artifacts" / name
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"pool copy")
        os.utime(legacy, (0, 0))
        from tauceti_worker import retention

        self.assertEqual(cache.root(), self.pool)
        with (
            patch.object(retention, "open_files", return_value=set()),
            canonical.object_lock(canonical.store(self.pool), name, exclusive=True),
        ):
            cache_storage.collect([], opened=set())
        self.assertTrue(legacy.exists())
        cache_storage.collect([], opened=set())
        self.assertFalse(legacy.exists())

    def test_local_first_then_ci_expansion_keeps_one_inode(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        # CI later publishes the same bytes as a second inode: a pool alias and an entry alias.
        entry = self.pool / "entries" / ("e" * 16)
        entry.mkdir(parents=True)
        outputs = json.loads((checkout / ".lake/build/lib/lean/TauCeti/A.trace").read_text())["outputs"]
        for path, descriptor in paths(checkout):
            shutil.copyfile(path, entry / descriptor)
        (self.pool / "artifacts").mkdir(exist_ok=True)
        shutil.copyfile(olean, self.pool / "artifacts" / name)
        cache.atomic_json(entry / "entry.json", dict(data=outputs))
        private = self.root / "other-store"
        cache.hydrate(entry, private, "d" * 16)
        inode = olean.stat().st_ino
        self.assertEqual((entry / name).stat().st_ino, inode)
        self.assertEqual((private / "artifacts" / name).stat().st_ino, inode)
        from tauceti_worker import retention

        canonical.sweep(self.pool, retention.fresh_report(time.time()), lifetime=3 * 86400, remove=retention.remove)
        self.assertEqual((self.pool / "artifacts" / name).stat().st_ino, inode)

    def test_idempotent_and_deletes_setup_files(self):
        checkout = self.checkout("w", "TauCeti.A", "TauCeti.B")
        self.assertTrue(list(checkout.rglob("*.setup.json")))
        first = self.relink(checkout)
        self.assertEqual(first["setup_removed"], 2)
        self.assertFalse(list(checkout.rglob("*.setup.json")))
        self.assertEqual(first["published_local"], len(paths(checkout)))
        for path, _ in paths(checkout):
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        with (
            patch.object(cache, "native", side_effect=AssertionError("warm relink needs no Lake hashing")),
            patch.object(canonical, "digest", side_effect=AssertionError("warm relink needs no sha256")),
            patch.object(canonical, "object_lock", side_effect=AssertionError("warm relink takes no lock")),
        ):
            second = self.relink(checkout)
        self.assertEqual(second["in_place"], len(paths(checkout)))
        self.assertNotIn("relinked", second)
        lease = next((canonical.store(self.pool) / "uses").glob("*.list")).read_text().split()
        self.assertEqual(sorted(lease), sorted(n for _, n in paths(checkout)))

    def test_private_store_and_pool_copies_adopt_one_inode(self):
        checkout = self.checkout("w", "TauCeti.A")
        store = self.root / "w-store"
        (store / "artifacts").mkdir(parents=True)
        found = paths(checkout)
        # The pool already holds a CI-verified copy of the olean; the private store a third copy.
        olean, name = next((p, n) for p, n in found if p.name == "A.olean")
        legacy = self.pool / "artifacts" / name
        legacy.parent.mkdir(parents=True)
        shutil.copyfile(olean, legacy)
        shutil.copyfile(olean, store / "artifacts" / name)
        # A private-store-only file with no canonical copy is left to Lake.
        (store / "artifacts" / ("f" * 16 + ".olean")).write_bytes(b"private")
        report = canonical.relink(checkout, store, self.pool, sweep_private=True)
        objects = canonical.store(self.pool) / "objects"
        inode = legacy.stat().st_ino
        self.assertEqual((objects / name).stat().st_ino, inode)
        self.assertEqual(olean.stat().st_ino, inode)
        self.assertEqual((store / "artifacts" / name).stat().st_ino, inode)
        self.assertEqual(report["published_ci"], 1)
        self.assertEqual(report["relinked_private"], 1)
        self.assertEqual((store / "artifacts" / ("f" * 16 + ".olean")).stat().st_nlink, 1)
        self.assertEqual(
            json.loads(canonical.sidecar(canonical.store(self.pool), name).read_text())["provenance"], "ci"
        )

    def test_corrupt_private_store_file_is_removed(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        store = self.root / "w-store"
        name = paths(checkout)[0][1]
        (store / "artifacts").mkdir(parents=True, exist_ok=True)
        (store / "artifacts" / name).write_bytes(b"forged")
        report = canonical.relink(checkout, store, self.pool, sweep_private=True)
        self.assertEqual(report["stale_store_files"], 1)
        self.assertFalse((store / "artifacts" / name).exists())

    def test_link_failure_is_fatal(self):
        first = self.checkout("first", "TauCeti.A")
        self.relink(first)
        second = self.checkout("second", "TauCeti.A")
        before = {p: p.stat().st_ino for p, _ in paths(second)}
        with patch.object(canonical.os, "link", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            with self.assertRaisesRegex(canonical.Unavailable, "cross-device"):
                self.relink(second)
        self.assertEqual({p: p.stat().st_ino for p, _ in paths(second)}, before)
        self.assertFalse(list(second.rglob(".relink-*")))

    def test_probe_failure_is_unavailable(self):
        checkout = self.checkout("w", "TauCeti.A")
        canonical.probe(checkout, self.root / "store", canonical.store(self.pool))
        self.assertFalse(list(checkout.rglob(".link-probe-*")))
        with patch.object(canonical.os, "link", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            with self.assertRaisesRegex(canonical.Unavailable, "hard links unavailable"):
                canonical.probe(checkout, self.root / "store", canonical.store(self.pool))
        # Through a namespace prefix too: here a plain `env` command that runs the probe.
        canonical.probe(checkout, self.root / "store", canonical.store(self.pool), prefix=["env"], env=dict(os.environ))
        self.assertFalse(list((self.root / "store/artifacts").glob(".link-probe-*")))

    @unittest.skipUnless(sys.platform == "linux", "process discovery uses /proc")
    def test_lake_writers_skip_relinking_but_servers_do_not(self):
        checkout = self.checkout("w", "TauCeti.A")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        def named(name, *args, cwd):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"import time, ctypes; ctypes.CDLL(None).prctl(15, b'{name}', 0, 0, 0); time.sleep(30)",
                    *args,
                ],
                cwd=cwd,
            )
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            return process

        server = named("lean", "--server", cwd=checkout)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=checkout)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        time.sleep(0.3)
        self.assertEqual(canonical.writers(checkout), [])
        self.assertNotIn("skipped", self.relink(checkout))
        # `lake -d <checkout>` from elsewhere names the checkout by argument, not cwd.
        builder = named("lake", "-d", str(checkout), "build", cwd=elsewhere)
        for _ in range(100):
            if builder.pid in canonical.writers(checkout):
                break
            time.sleep(0.05)
        self.assertNotIn(server.pid, canonical.writers(checkout))
        module(checkout, "TauCeti.B", "same")
        report = self.relink(checkout)
        self.assertIn("name the checkout", report["skipped"])
        self.assertFalse(any(n.startswith(lake_hash(b"")) for n in os.listdir(canonical.store(self.pool) / "objects")))

    def test_eviction_respects_links_leases_and_the_object_lock(self):
        from tauceti_worker import retention

        live = self.checkout("live", "TauCeti.A")
        self.relink(live)
        gone = self.checkout("gone", "TauCeti.B", seed="other")
        self.relink(gone)
        shutil.rmtree(gone)
        objects = canonical.store(self.pool) / "objects"
        b_names = {n for n in os.listdir(objects)} - {n for _, n in paths(live)}
        report = retention.fresh_report(time.time())

        def sweep(**kwargs):
            return canonical.sweep(self.pool, report, lifetime=3 * 86400, remove=retention.remove, **kwargs)

        sweep()  # The removed checkout's lease still protects its names.
        self.assertTrue(all((objects / n).exists() for n in b_names))
        with canonical.object_lock(canonical.store(self.pool), sorted(b_names)[0]):
            sweep(urgent=True)  # A relink holding the shared lock wins against eviction.
        self.assertTrue((objects / sorted(b_names)[0]).exists())
        sweep(urgent=True)
        self.assertFalse(any((objects / n).exists() for n in b_names))
        self.assertFalse(any(canonical.sidecar(canonical.store(self.pool), n).exists() for n in b_names))
        # Objects a live checkout links are never evicted, under pressure or not.
        self.assertTrue(all((objects / n).exists() for _, n in paths(live)))

    def test_gc_counts_the_namespace_once_and_evicts_adopted_aliases(self):
        checkout = self.checkout("w", "TauCeti.A")
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        legacy = self.pool / "artifacts" / name
        legacy.parent.mkdir(parents=True)
        shutil.copyfile(olean, legacy)
        self.relink(checkout)
        details = {}
        totals = cache_storage.inventory([self.pool, checkout / ".lake"], details=details)
        row = details[(legacy.stat().st_dev, legacy.stat().st_ino)]
        self.assertEqual(row[1], 3)  # objects/, artifacts/ and the checkout: counted once.
        self.assertEqual(sum(r["managed_bytes"] for r in totals.values()), sum(r[0] for r in details.values()))
        shutil.rmtree(checkout)
        for lease in (canonical.store(self.pool) / "uses").glob("*"):
            lease.unlink()
        from tauceti_worker import retention

        canonical.sweep(self.pool, retention.fresh_report(time.time()), lifetime=1, remove=retention.remove)
        self.assertFalse(legacy.exists())
        self.assertFalse((canonical.store(self.pool) / "objects" / name).exists())

    @unittest.skipUnless(shutil.which("lean") and shutil.which("lake"), "Lean/Lake needed for native fixture")
    def test_real_lake_stays_up_to_date_after_relinking(self):
        # Pin the production toolchain where elan has it, so this exercises the deployed Lake.
        toolchain = os.environ.get("TAUCETI_TEST_LEAN_TOOLCHAIN", "leanprover/lean4:v4.36.0-rc2")
        try:
            prefix = Path(
                subprocess.check_output(
                    ["lean", "+" + toolchain, "--print-prefix"], text=True, stderr=subprocess.DEVNULL, timeout=60
                ).strip()
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest(f"toolchain {toolchain} unavailable")
        env = dict(os.environ, PATH=str(prefix / "bin") + ":" + os.environ["PATH"], LAKE_ARTIFACT_CACHE="1")

        def project(name):
            path = self.root / name
            (path / "Fx").mkdir(parents=True)
            (path / "lakefile.toml").write_text('name = "fx"\n[[lean_lib]]\nname = "Fx"\n')
            (path / "lean-toolchain").write_text(toolchain + "\n")
            (path / "Fx/A.lean").write_text("module\npublic def a : Nat := 1\n")
            (path / "Fx.lean").write_text("module\npublic import Fx.A\npublic def b : Nat := a + 1\n")
            return path

        def lake(path, *args):
            return subprocess.run(
                [str(prefix / "bin/lake"), *args],
                cwd=path,
                env=dict(env, LAKE_CACHE_DIR=str(self.root / (path.name + "-store"))),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        first, second = project("first"), project("second")
        for path in (first, second):
            result = lake(path, "build", "Fx")
            self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(list(first.rglob("*.setup.json")))
        self.assertNotEqual(
            (first / ".lake/build/lib/lean/Fx/A.olean").stat().st_ino,
            (second / ".lake/build/lib/lean/Fx/A.olean").stat().st_ino,
        )
        for path in (first, second):
            report = canonical.relink(path, self.root / (path.name + "-store"), self.pool, env=env, sweep_private=True)
            self.assertNotIn("unverified", report)
        self.assertFalse(list(first.rglob("*.setup.json")))
        self.assert_one_inode(first, second)
        store_copy = self.root / "second-store/artifacts" / paths(second)[0][1]
        self.assertEqual(store_copy.stat().st_ino, paths(second)[0][0].stat().st_ino)
        for path in (first, second):
            result = lake(path, "build", "--no-build", "Fx")
            self.assertEqual(result.returncode, 0, result.stdout)
            result = lake(path, "build", "Fx")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Built", result.stdout)
            self.assertNotIn("Unpacked", result.stdout)
        # A rebuild in one checkout never writes the shared inode in place.
        olean = second / ".lake/build/lib/lean/Fx/A.olean"
        before = (first / ".lake/build/lib/lean/Fx/A.olean").read_bytes()
        (second / "Fx/A.lean").write_text("module\npublic def a : Nat := 1\npublic def changed : Nat := 2\n")
        result = lake(second, "build", "Fx")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(olean.read_bytes() != before, "the rebuild must produce a different olean")
        self.assertTrue((first / ".lake/build/lib/lean/Fx/A.olean").read_bytes() == before)


if __name__ == "__main__":
    unittest.main()

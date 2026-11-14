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


def module(checkout, name, seed, *, setup=True, shared=False):
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
        data = (f"{seed}:{extension}" if shared else f"{seed}:{name}:{extension}").encode() * 64
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
        self.assertEqual(report["invalidated_outputs"], 1)
        lib = bad / ".lake/build/lib/lean/TauCeti"
        self.assertTrue((lib / "A.trace").exists())  # Kept: siblings stay listed.
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
        self.assertNotIn("invalidated_outputs", report)
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
        self.assertEqual(report["invalidated_outputs"], 1)
        self.assertTrue((good / ".lake/build/lib/lean/TauCeti/A.trace").exists())  # Kept: siblings stay listed.

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
        self.assertEqual(report["invalidated_outputs"], 1)
        self.assertFalse((objects / name).exists())
        self.assertFalse(olean.exists())

    def test_unverifiable_outputs_are_removed_not_kept_private(self):
        checkout = self.checkout("w", "TauCeti.A")
        target = checkout / ".lake/build/lib/lean/TauCeti/A.ilean"
        target.write_bytes(b"not what the trace hashed")
        name = next(n for p, n in paths(checkout) if p == target)
        report = self.relink(checkout)
        self.assertEqual(report["invalidated_outputs"], 1)
        self.assertFalse((canonical.store(self.pool) / "objects" / name).exists())
        self.assertFalse(target.exists())
        self.assertTrue((checkout / ".lake/build/lib/lean/TauCeti/A.trace").exists())  # Kept: siblings stay listed.

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
            self.assertNotIn("invalidated_outputs", report, case)
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
        self.assertNotIn("invalidated_outputs", report)
        # Pool good, checkout damaged: adopt the pool copy, remove only the damaged file.
        other = self.checkout("other", "TauCeti.B", seed="b")
        b_olean, b_name = next((p, n) for p, n in paths(other) if p.name == "B.olean")
        shutil.copyfile(b_olean, self.pool / "artifacts" / b_name)
        b_olean.write_bytes(b"damaged checkout copy")
        report = self.relink(other)
        self.assertEqual(report["published_ci"], 1)
        self.assertEqual(report["invalidated_outputs"], 1)
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

    def test_two_names_on_one_inode_are_judged_separately(self):
        checkout = self.checkout("w", "TauCeti.X", "TauCeti.Y")
        lib = checkout / ".lake/build/lib/lean/TauCeti"
        good, name = next((p, n) for p, n in paths(checkout) if p.name == "X.olean")
        stale = lib / "Y.olean"
        stale.unlink()
        os.link(good, stale)  # Y's trace still names its own (now wrong) hash.
        report = self.relink(checkout)
        objects = canonical.store(self.pool) / "objects"
        self.assertTrue(good.exists())
        self.assertEqual((objects / name).stat().st_ino, good.stat().st_ino)
        self.assertFalse(stale.exists())
        self.assertEqual(report["invalidated_outputs"], 1)

    def test_busy_pool_files_are_never_adopted_quarantined_or_evicted(self):
        objects = canonical.store(self.pool) / "objects"
        (self.pool / "artifacts").mkdir()
        # Adoption: a verified pool copy someone holds open for writing.
        first = self.checkout("first", "TauCeti.A")
        olean, name = next((p, n) for p, n in paths(first) if p.name == "A.olean")
        pool_copy = self.pool / "artifacts" / name
        shutil.copyfile(olean, pool_copy)
        held = open(pool_copy, "r+b")
        self.addCleanup(held.close)
        report = self.relink(first)
        self.assertNotIn("published_ci", report)
        self.assertNotEqual(
            (objects / name).stat().st_ino if (objects / name).exists() else None, pool_copy.stat().st_ino
        )
        self.assertGreaterEqual(report["deferred_busy"], 1)
        # Quarantine: a damaged pool copy held open stays where it is.
        second = self.checkout("second", "TauCeti.B", seed="b")
        b_name = next(n for p, n in paths(second) if p.name == "B.olean")
        damaged = self.pool / "artifacts" / b_name
        damaged.write_bytes(b"damaged")
        held_damaged = open(damaged, "r+b")
        self.addCleanup(held_damaged.close)
        report = self.relink(second)
        self.assertTrue(damaged.exists())
        self.assertNotIn("pool_quarantined", report)
        # Eviction: a corrupt canonical object held open is neither evicted nor re-recorded.
        third = self.checkout("third", "TauCeti.C", seed="c")
        self.relink(third)
        c_olean, c_name = next((p, n) for p, n in paths(third) if p.name == "C.olean")
        os.chmod(c_olean, 0o644)
        writer = open(c_olean, "r+b")
        self.addCleanup(writer.close)
        writer.write(b"x")
        writer.flush()
        os.utime(c_olean, (time.time() - 60,) * 2)
        before = canonical.sidecar(canonical.store(self.pool), c_name).read_text()
        report = self.relink(third)
        self.assertNotIn("integrity_failures", report)
        self.assertTrue((objects / c_name).exists())
        self.assertEqual(canonical.sidecar(canonical.store(self.pool), c_name).read_text(), before)

    def test_many_copies_of_one_name_make_progress_under_a_limit(self):
        modules = [f"TauCeti.M{i}" for i in range(40)]
        checkout = self.root / "w"
        for mod in modules:
            module(checkout, mod, "same", shared=True)
        objects = canonical.store(self.pool) / "objects"
        self.assertEqual(len({n for _, n in paths(checkout)}), 5)  # 40 copies of each of 5 names

        def unshared():
            return sum(
                1
                for p, n in paths(checkout)
                if not (objects / n).exists() or p.stat().st_ino != (objects / n).stat().st_ino
            )

        def slow(program, paths, directory):
            time.sleep(0.01 * len(paths))  # Hashing dominates, as on a cold host.
            return fake_hashes(program, paths, directory)

        remaining = [unshared()]
        for _ in range(60):
            with patch.object(cache, "hashes", slow):
                report = self.relink(checkout, budget=0.2)
            remaining.append(unshared())
            if not report.get("incomplete"):
                break
        self.assertEqual(remaining[-1], 0)
        self.assertTrue(all(a > b for a, b in zip(remaining, remaining[1:], strict=False)), remaining)

    def test_an_invalidated_output_leaves_its_siblings_listed_for_the_next_pass(self):
        checkout = self.checkout("w", "TauCeti.A")
        found = sorted(paths(checkout), key=lambda pair: pair[1])
        first_path = found[0][0]
        first_path.write_bytes(b"interrupted")
        report = self.relink(checkout, limit=1)
        self.assertEqual(report["invalidated_outputs"], 1)
        self.assertEqual(report["remaining"], len(found) - 1)
        report = self.relink(checkout)
        self.assertEqual(report["outputs"], len(found))
        self.assertEqual(report["published_local"], len(found) - 1)
        self.assertNotIn("incomplete", report)

    def test_reconcile_never_spreads_or_records_a_writable_inode(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        alias = self.pool / "artifacts" / name
        alias.parent.mkdir(parents=True)
        shutil.copyfile(olean, alias)
        directory = canonical.store(self.pool)
        canonical.sidecar(directory, name).unlink()
        for target in (olean, alias):  # Either side held open for writing blocks it.
            os.chmod(target, 0o644)
            held = open(target, "r+b")
            try:
                self.assertFalse(canonical.reconcile(self.pool, name, alias))
            finally:
                held.close()
            self.assertNotEqual(alias.stat().st_ino, olean.stat().st_ino)
            self.assertFalse(canonical.sidecar(directory, name).exists())  # Not healed while writable.
        self.assertTrue(canonical.reconcile(self.pool, name, alias))
        self.assertEqual(alias.stat().st_ino, olean.stat().st_ino)

    def test_corrupt_shared_inode_also_loses_its_private_store_alias(self):
        checkout = self.checkout("w", "TauCeti.A")
        store = self.root / "w-store"
        (store / "artifacts").mkdir(parents=True)
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        os.link(olean, store / "artifacts" / name)  # As Lake's saveArtifact does.
        canonical.relink(checkout, store, self.pool, sweep_private=True)
        objects = canonical.store(self.pool) / "objects"
        self.assertEqual((store / "artifacts" / name).stat().st_ino, (objects / name).stat().st_ino)
        os.chmod(olean, 0o644)
        olean.write_bytes(b"x" * olean.stat().st_size)  # Corrupts all three names at once.
        os.utime(olean, (time.time() - 60,) * 2)
        report = canonical.relink(checkout, store, self.pool, sweep_private=True)
        self.assertEqual(report["integrity_failures"], [name])
        self.assertFalse((objects / name).exists())
        self.assertFalse(olean.exists())
        self.assertFalse((store / "artifacts" / name).exists())  # Lake cannot restore the bad bytes.
        self.assertTrue((olean.parent / "A.trace").exists())

    def test_sweep_protects_objects_held_open(self):
        from tauceti_worker import retention

        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        objects = canonical.store(self.pool) / "objects"
        name = paths(checkout)[0][1]
        shutil.rmtree(checkout)
        for lease in (canonical.store(self.pool) / "uses").glob("*"):
            lease.unlink()
        info = (objects / name).stat()

        def sweep(opened):
            canonical.sweep(
                self.pool, retention.fresh_report(time.time()), lifetime=1, remove=retention.remove, opened=opened
            )

        sweep({(info.st_dev, info.st_ino)})
        self.assertTrue((objects / name).exists())
        self.assertTrue(canonical.sidecar(canonical.store(self.pool), name).exists())
        sweep(set())
        self.assertFalse((objects / name).exists())

    def test_hashing_that_outlasts_the_grace_still_makes_progress(self):
        modules = [f"TauCeti.M{i}" for i in range(8)]
        checkout = self.checkout("w", *modules)
        objects = canonical.store(self.pool) / "objects"

        def slow(program, paths, directory):
            time.sleep(0.3)  # Each helper call outlasts the whole grace window.
            return fake_hashes(program, paths, directory)

        def unshared():
            return sum(1 for p, n in paths(checkout) if not (objects / n).exists())

        remaining = [unshared()]
        with patch.object(canonical, "GRACE", 0.2), patch.object(cache, "hashes", slow):
            for _ in range(40):
                report = self.relink(checkout, budget=0)
                remaining.append(unshared())
                if not report.get("incomplete"):
                    break
        self.assertEqual(remaining[-1], 0)
        self.assertTrue(all(a > b for a, b in zip(remaining, remaining[1:], strict=False)), remaining)

    def entry_for(self, checkout, key="e" * 16):
        """A pool entry whose payload names link the checkout's files, as expansion leaves them."""
        entry = self.pool / "entries" / key
        entry.mkdir(parents=True)
        trace = next((checkout / ".lake/build/lib/lean").rglob("*.trace"))
        for path, name in paths(checkout):
            if path.exists():
                os.link(path, entry / name)
        cache.atomic_json(entry / "entry.json", dict(data=json.loads(trace.read_text())["outputs"]))
        return entry

    def test_hydration_never_links_a_writable_payload(self):
        checkout = self.checkout("w", "TauCeti.A")
        entry = self.entry_for(checkout)
        name = paths(checkout)[0][1]
        os.chmod(entry / name, 0o644)
        held = open(entry / name, "r+b")
        self.addCleanup(held.close)
        private = self.root / "other-store"
        with self.assertRaises(cache.LinkDeferred):
            cache.hydrate(entry, private, "d" * 16)
        self.assertFalse((private / "outputs").exists())  # No mapping names unlinked artifacts.
        self.assertFalse((private / "artifacts" / name).exists())
        held.close()
        cache.hydrate(entry, private, "d" * 16)
        self.assertTrue((private / "outputs/TauCeti" / ("d" * 16 + ".json")).exists())

    def test_rejected_bytes_are_never_rehydrated_or_kept_privately(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        entry = self.entry_for(checkout)
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        os.chmod(olean, 0o644)
        olean.write_bytes(b"x" * olean.stat().st_size)  # Corrupts the object and the entry alias.
        os.utime(olean, (time.time() - 60,) * 2)
        bad = (entry / name).lstat()
        report = self.relink(checkout)
        self.assertEqual(report["integrity_failures"], [name])
        self.assertIn(canonical.fingerprint(bad), canonical.rejected(self.pool))
        private = self.root / "w-store"
        with self.assertRaisesRegex(ValueError, "rejected"):  # Expansion then re-expands the entry.
            cache.hydrate(entry, private, "d" * 16)
        self.assertFalse((private / "artifacts" / name).exists())
        # A later pass (fresh `suspect` set) still removes a private alias of the rejected bytes.
        (private / "artifacts").mkdir(parents=True, exist_ok=True)
        os.link(entry / name, private / "artifacts" / name)
        report = canonical.relink(checkout, private, self.pool, sweep_private=True)
        self.assertEqual(report["stale_store_files"], 1)
        self.assertFalse((private / "artifacts" / name).exists())

    def test_private_alias_is_dropped_only_while_it_is_the_rejected_snapshot(self):
        checkout = self.checkout("w", "TauCeti.A")
        olean, name = next((p, n) for p, n in paths(checkout) if p.name == "A.olean")
        private = self.root / "w-store/artifacts"
        private.mkdir(parents=True)
        os.link(olean, private / name)
        snapshot = olean.lstat()
        # Repaired in place after verification: same inode, new bytes and mtime.
        os.chmod(olean, 0o644)
        olean.write_bytes(olean.read_bytes())
        os.utime(olean, ns=(snapshot.st_mtime_ns + 10**9,) * 2)
        work = canonical.Pass(
            checkout,
            self.pool,
            private.parent,
            dict(os.environ),
            {},
            deadline=time.monotonic() + 30,
            not_after=time.time() + 60,
        )
        work.drop_private_alias(name, snapshot)
        self.assertTrue((private / name).exists())
        # Still exactly the rejected snapshot: dropped.
        work.drop_private_alias(name, (private / name).lstat())
        self.assertFalse((private / name).exists())

    def test_a_file_whose_sha256_never_fits_does_not_pin_later_work(self):
        good = self.checkout("good", *[f"TauCeti.M{i}" for i in range(6)])
        self.relink(good)  # Canonical objects exist, so convergence needs sha256.
        checkout = self.checkout("w", *[f"TauCeti.M{i}" for i in range(6)])
        objects = canonical.store(self.pool) / "objects"
        stuck = sorted(paths(checkout), key=lambda pair: pair[1])[0][0]
        real_digest = canonical.digest

        def slow(path, deadline=None):
            if Path(path) == stuck and deadline is not None:
                raise TimeoutError("hashing reached the relink deadline")  # Always too slow.
            return real_digest(path, deadline)

        def unshared():
            return sum(1 for p, n in paths(checkout) if p.stat().st_ino != (objects / n).stat().st_ino)

        remaining = [unshared()]
        with patch.object(canonical, "digest", slow):
            for _ in range(40):
                report = self.relink(checkout, budget=0)
                remaining.append(unshared())
                if remaining[-1] == 1:
                    break
        self.assertEqual(remaining[-1], 1)  # Everything but the stuck file converged.
        self.assertTrue(all(a > b for a, b in zip(remaining, remaining[1:], strict=False)), remaining)
        self.assertGreaterEqual(report.get("deferred_slow", 0), 1)
        cursor = next((canonical.store(self.pool) / "cursors").glob("*.json"))
        self.assertIn(str(stuck), json.loads(cursor.read_text())["slow"])

    def test_retried_slow_file_keeps_its_longer_allowance(self):
        good = self.checkout("good", "TauCeti.A")
        self.relink(good)
        checkout = self.checkout("w", "TauCeti.A")
        objects = canonical.store(self.pool) / "objects"
        stuck, stuck_name = sorted(paths(checkout), key=lambda pair: pair[1])[0]
        real_digest = canonical.digest

        def slow(path, deadline=None):
            if Path(path) == stuck and deadline is not None:
                finish = time.monotonic() + 0.4  # Longer than GRACE, shorter than 3 * GRACE.
                while time.monotonic() < finish:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("hashing reached the relink deadline")
                    time.sleep(0.01)
            return real_digest(path)

        with patch.object(canonical, "GRACE", 0.2), patch.object(canonical, "digest", slow):
            first = self.relink(checkout, budget=0)  # Budget exhausted before any work.
            self.assertEqual(first["deferred_slow"], 1)
            second = self.relink(checkout, budget=0)  # The retry gets 3 * GRACE and finishes.
        self.assertNotIn("deferred_slow", second)
        self.assertEqual(stuck.stat().st_ino, (objects / stuck_name).stat().st_ino)

    def test_operator_switches_disable_relinking_immediately(self):
        checkout = self.checkout("w", "TauCeti.A")
        env = dict(os.environ, LAKE_CACHE_DIR=str(self.root / "w-store"))
        (canonical.store(self.pool)).mkdir(parents=True, exist_ok=True)
        (canonical.store(self.pool) / "disabled").touch()
        self.assertIsNone(canonical.boundary(checkout, env, phase="prepare"))
        (canonical.store(self.pool) / "disabled").unlink()
        self.assertIsNone(canonical.boundary(checkout, dict(env, TAUCETI_CANONICAL_RELINK="0"), phase="round-end"))
        self.assertFalse((canonical.store(self.pool) / "objects").exists())
        self.assertIsNotNone(canonical.boundary(checkout, env, phase="round-end"))

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
        self.assertEqual(report["published_local"], 1)  # Even an exhausted budget finishes one file.
        directory = canonical.store(self.pool)
        with canonical.slot(directory, count=1, wait=0) as held:
            self.assertTrue(held)
            report = self.relink(checkout, slots=1, slot_wait=0.2)
            self.assertIn("relinks already running", report["skipped"])
        report = self.relink(checkout, slots=1)
        self.assertEqual(report["published_local"], len(paths(checkout)) - 1)
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

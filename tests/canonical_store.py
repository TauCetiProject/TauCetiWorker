"""Canonical build outputs: one inode per host, sha256-checked convergence, and safe eviction."""

import errno
import hashlib
import json
import os
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
    found, _ = canonical.outputs(checkout / ".lake/build")
    return found


class Canonical(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
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

    def test_sha256_mismatch_fails_without_replacing_the_file(self):
        good = self.checkout("good", "TauCeti.A", "TauCeti.B")
        self.relink(good)
        bad = self.checkout("bad", "TauCeti.A", "TauCeti.B")
        target = bad / ".lake/build/lib/lean/TauCeti/A.olean"
        original = target.read_bytes()
        forged = bytes(reversed(original))
        target.write_bytes(forged)  # Same name in the trace, different bytes, same size.
        before = target.stat().st_ino
        with self.assertRaisesRegex(canonical.Tampered, "differ from canonical"):
            self.relink(bad)
        self.assertEqual(target.read_bytes(), forged)
        self.assertEqual(target.stat().st_ino, before)
        # Everything else still converged before the round failed.
        self.assertEqual(
            (bad / ".lake/build/lib/lean/TauCeti/B.olean").stat().st_ino,
            (good / ".lake/build/lib/lean/TauCeti/B.olean").stat().st_ino,
        )
        objects = canonical.store(self.pool) / "objects"
        self.assertEqual((objects / next(n for p, n in paths(good) if p.name == "A.olean")).read_bytes(), original)

    def test_unverifiable_name_is_never_published(self):
        checkout = self.checkout("w", "TauCeti.A")
        target = checkout / ".lake/build/lib/lean/TauCeti/A.ilean"
        target.write_bytes(b"not what the trace hashed")
        report = self.relink(checkout)
        self.assertEqual(report["unverified"], 1)
        name = next(n for p, n in paths(checkout) if p == target)
        self.assertFalse((canonical.store(self.pool) / "objects" / name).exists())
        self.assertEqual(target.stat().st_nlink, 1)

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

    def test_tampered_private_store_fails(self):
        checkout = self.checkout("w", "TauCeti.A")
        self.relink(checkout)
        store = self.root / "w-store"
        name = paths(checkout)[0][1]
        (store / "artifacts").mkdir(parents=True, exist_ok=True)
        (store / "artifacts" / name).write_bytes(b"forged")
        with self.assertRaises(canonical.Tampered):
            canonical.relink(checkout, store, self.pool, sweep_private=True)
        self.assertEqual((store / "artifacts" / name).read_bytes(), b"forged")

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
    def test_running_lake_in_the_checkout_refuses_relinking(self):
        checkout = self.checkout("w", "TauCeti.A")
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=checkout)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        # A fake `lake` (PR_SET_NAME) with its cwd in the checkout, as an unreaped build would be.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time, ctypes; ctypes.CDLL(None).prctl(15, b'lake', 0, 0, 0); time.sleep(30)",
            ],
            cwd=checkout,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        for _ in range(100):
            if holder.pid in canonical.writers(checkout):
                break
            time.sleep(0.05)
        self.assertNotIn(bystander.pid, canonical.writers(checkout))
        with self.assertRaises(canonical.Busy):
            self.relink(checkout)
        self.assertFalse((canonical.store(self.pool) / "objects").exists())

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
        prefix = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip())
        env = dict(os.environ, PATH=str(prefix / "bin") + ":" + os.environ["PATH"], LAKE_ARTIFACT_CACHE="1")

        def project(name):
            path = self.root / name
            (path / "Fx").mkdir(parents=True)
            (path / "lakefile.toml").write_text('name = "fx"\n[[lean_lib]]\nname = "Fx"\n')
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

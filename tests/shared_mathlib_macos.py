"""Shared Mathlib on macOS: per-round views made of APFS clones instead of overlay mounts.

On macOS these tests use the real ``clonefile(2)``. Elsewhere the clone primitive is replaced by an
ordinary file copy and ``platform.system`` reports Darwin, which exercises the backend's logic but
not APFS itself; the one test of the real primitive is skipped there.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_mathlib import Fixture, git, writable_bits  # the Linux module's fixture checkout

from tauceti_worker import round_storage
from tauceti_worker import shared_mathlib as s
from tauceti_worker.runtime_status import read_json

REAL = sys.platform == "darwin"


def copy(source, destination):
    """What clonefile(2) does, without the shared blocks: a new inode with the same content and mode."""
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(destination))
    shutil.copy2(source, destination, follow_symlinks=False)


class MacFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.clones = 0
        self.clone_error = None  # raised by the primitive when set
        patches = [patch.object(s.platform, "system", return_value="Darwin")]
        if not REAL:
            patches += [
                patch.object(s, "clone_file", side_effect=self.clone),
                patch.object(s, "boot_id", return_value="fixture-boot"),
            ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        # Read-only clones would defeat the temporary directory's cleanup.
        self.addCleanup(lambda: s.remove_tree(self.cwd / ".lake"))

    def clone(self, source, destination):
        if self.clone_error is not None:
            raise self.clone_error
        self.clones += 1
        copy(source, destination)

    def extract(self, command, cwd, env, output, timeout):
        """Mathlib's `lake exe cache get`, run in the staging workspace without any namespace."""
        self.downloads += 1
        self.assertEqual(command, ["lake", "exe", "cache", "get"])
        self.assertNotIn("TAUCETI_SHARED_MATHLIB_LEASE_FD", env)
        self.assertEqual(cwd.name, "root")  # a staging workspace, never a checkout
        self.assertFalse((cwd / ".git").exists())
        self.assertTrue(cwd.is_relative_to(self.P / "staging"))
        revisions = {p["name"]: p["rev"] for p in json.loads((cwd / "lake-manifest.json").read_text())["packages"]}
        for name in ("mathlib", "batteries"):
            package = cwd / ".lake/packages" / name
            self.assertFalse(package.is_symlink())  # a private clone of the shared source
            self.assertEqual(git(package, "rev-parse", "HEAD"), revisions[name])
            self.assertEqual(list((package / ".lake/build").iterdir()), [])
        build = cwd / ".lake/packages/mathlib/.lake/build"
        for module in ("Mathlib", "Mathlib/Test"):
            for suffix in (".olean", ".ilean"):
                target = build / "lib/lean" / (module + suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.artifact)
        batteries = cwd / ".lake/packages/batteries/.lake/build/lib/lean"
        batteries.mkdir(parents=True)
        (batteries / "Batteries.olean").write_bytes(b"batteries artifact")
        return 0

    def view(self):
        return s.view(self.cwd, self.env, self.root / "logs", self.extract)

    def link(self, name="mathlib"):
        return self.cwd / ".lake/packages" / name

    @property
    def current(self):
        """The link through which the checkout's packages name the view's clones."""
        return self.cwd / ".lake" / s.META / s.CLONE_CURRENT

    def generations(self):
        meta = self.cwd / ".lake" / s.META
        return sorted(p.name for p in meta.iterdir() if p.is_dir() and not p.is_symlink()) if meta.is_dir() else []

    def assert_detached(self):
        """Outside a view: the packages are the shared sources, whose build directories are empty."""
        for name in ("mathlib", "batteries"):
            self.assertEqual(os.readlink(self.link(name)), str(self.source / name))
            self.assertEqual(list((self.link(name) / ".lake/build").iterdir()), [])
        with self.assertRaises(PermissionError):
            (self.link() / ".lake/build/Mathlib.olean").write_bytes(b"private copy")
        self.assertFalse(self.current.is_symlink())


class BackendTests(unittest.TestCase):
    def test_linux_keeps_the_overlay_backend_and_other_systems_have_none(self):
        for system, expected in (("Linux", "overlay"), ("Darwin", "clone"), ("FreeBSD", None)):
            with patch.object(s.platform, "system", return_value=system):
                self.assertEqual(s.backend(), expected)

    def test_an_unsupported_system_keeps_the_linux_requirement_message(self):
        with patch.object(s.platform, "system", return_value="FreeBSD"):
            ok, note = s.capability()
        self.assertFalse(ok)
        self.assertEqual(
            note,
            "Linux user namespaces and OverlayFS are required for shared Mathlib; this host runs no rounds that"
            " need Mathlib",
        )

    @unittest.skipIf(REAL, "the primitive exists on macOS")
    def test_the_primitive_never_copies_where_clonefile_is_missing(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(s.platform, "system", return_value="Darwin"):
            source = Path(raw) / "source"
            source.write_text("shared")
            for system in ("Darwin", "Linux"):  # a libc without clonefile, and a system that is not macOS
                with patch.object(s.platform, "system", return_value=system):
                    with self.assertRaises(OSError) as raised:
                        s.clone_file(source, Path(raw) / "copy")
                    self.assertEqual(raised.exception.errno, errno.ENOTSUP)
            self.assertFalse((Path(raw) / "copy").exists())
            ok, note = s.capability(Path(raw))
            self.assertFalse(ok)
            self.assertIn("one APFS volume", note)

    @unittest.skipUnless(REAL, "clonefile(2) and APFS exist only on macOS")
    def test_real_clonefile_gives_a_private_copy_and_leaves_the_original_untouched(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            here = Path(raw)
            ok, note = s.capability(here)
            self.assertTrue(ok, note)
            original = here / "original"
            (original / "lib").mkdir(parents=True)
            (original / "lib/big.olean").write_bytes(os.urandom(1 << 20))
            (original / "lib/link").symlink_to("big.olean")
            content = (original / "lib/big.olean").read_bytes()
            s._freeze(original)
            self.addCleanup(lambda: s.remove_tree(original))
            before = {
                str(path.relative_to(original)): (info := path.lstat())
                and (info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                for path in original.rglob("*")
                if not path.is_symlink()
            }
            s._clone_tree(original, here / "private", file_bits=0o200)
            clone = here / "private/lib/big.olean"
            self.assertEqual(clone.read_bytes(), content)
            self.assertNotEqual(clone.stat().st_ino, (original / "lib/big.olean").stat().st_ino)
            self.assertEqual(clone.stat().st_mtime_ns, (original / "lib/big.olean").stat().st_mtime_ns)
            self.assertEqual(os.readlink(here / "private/lib/link"), "big.olean")
            with clone.open("r+b") as stream:
                stream.write(b"private")
            (here / "private/lib/new.olean").write_bytes(b"new")
            self.assertEqual((original / "lib/big.olean").read_bytes(), content)
            self.assertFalse((original / "lib/new.olean").exists())
            # Cloning must not disturb what `ready.json` records of a published build (ctime included).
            after = {
                str(path.relative_to(original)): (info := path.lstat())
                and (info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                for path in original.rglob("*")
                if not path.is_symlink()
            }
            self.assertEqual(after, before)
            with self.assertRaises(FileExistsError):
                s.clone_file(original / "lib/big.olean", clone)


class CapabilityTests(MacFixture):
    def test_a_filesystem_that_clones_is_capable_and_doctor_probes_the_pool_filesystem(self):
        ok, note = s.capability(self.root)
        self.assertTrue(ok, note)
        self.assertEqual(note, "APFS clones work; immutable original with private copy-on-write builds")
        self.assertEqual(list(self.root.glob("tauceti-clone-probe-*")), [])
        # `tauceti doctor` passes no directory: the probe runs beside the pool, creating neither.
        with patch.dict(os.environ, TAUCETI_SHARED_MATHLIB_ROOT=str(self.root / "absent/pool")):
            ok, note = s.capability()
        self.assertTrue(ok, note)
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(list(self.root.glob("tauceti-clone-probe-*")), [])

    @unittest.skipIf(REAL, "needs a filesystem without clones")
    def test_a_filesystem_without_clones_is_named_and_blocks_before_any_download(self):
        self.clone_error = OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        ok, note = s.capability(self.root)
        self.assertFalse(ok)
        self.assertIn(f"the filesystem holding {self.root} cannot clone files", note)
        self.assertIn("one APFS volume", note)
        self.assert_blocked("cannot clone files")
        self.assertEqual(self.downloads, 0)
        self.assertFalse((self.pool / "safety-stop.json").exists())

    @unittest.skipIf(REAL, "needs a failing primitive")
    def test_a_failing_probe_is_reported_and_blocks(self):
        self.clone_error = OSError(errno.EIO, "probe I/O error")
        ok, note = s.capability(self.root)
        self.assertFalse(ok)
        self.assertRegex(note, r"^APFS clones unavailable: .*probe I/O error")
        self.assert_blocked("APFS clones unavailable")
        self.assertEqual(self.downloads, 0)

    @unittest.skipIf(REAL, "needs a primitive that aliases instead of cloning")
    def test_a_clone_that_aliases_its_original_is_refused_without_writing_to_it(self):
        with patch.object(s, "clone_file", side_effect=os.link):
            ok, note = s.capability(self.root)
        self.assertFalse(ok)
        self.assertIn("is not a private copy of its original", note)


class PublishTests(MacFixture):
    def test_a_build_is_extracted_into_clones_of_the_source_and_published_read_only(self):
        key, spec, entry, hit, _, _ = self.published()
        self.assertFalse(hit)
        self.assertEqual(spec["system"], "Darwin")
        self.assertEqual((entry / "build/mathlib/lib/lean/Mathlib.olean").read_bytes(), self.artifact)
        self.assertEqual((entry / "build/batteries/lib/lean/Batteries.olean").read_bytes(), b"batteries artifact")
        # No read-only mount protects the entry on macOS: every write bit is gone instead.
        self.assertEqual(writable_bits(entry / "build"), [])
        self.assertEqual(writable_bits(self.source), [])
        self.assertEqual(list((self.source / "mathlib/.lake/build").iterdir()), [])
        self.assertEqual(list((self.P / "staging").iterdir()), [])
        self.assertTrue(s.ready_entry(entry, key, spec))
        with s.key_lock(self.P, key):
            self.assertTrue(s.publish(self.P, key, spec, *self.attach(), self.env, self.root / "logs", self.extract)[1])
        self.assertEqual(self.downloads, 1)

    def test_a_failed_extraction_publishes_nothing_and_leaves_no_staging(self):
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)
        with s.key_lock(self.P, key), self.assertRaisesRegex(s.Unavailable, "extraction failed"):
            s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", lambda *args: 7)
        self.assertFalse((self.P / "entries" / key).exists())
        self.assertEqual(list((self.P / "staging").iterdir()), [])

    def test_outputs_hard_linked_into_the_lake_cache_are_published_as_files_of_their_own(self):
        """Lake's artifact cache links a built executable into LAKE_CACHE_DIR (and restores by link)."""
        cache = self.root / "lake-cache/artifacts/cache-executable"
        cache.parent.mkdir(parents=True)

        def extract(command, cwd, env, output, timeout):
            rc = self.extract(command, cwd, env, output, timeout)
            binary = cwd / ".lake/packages/mathlib/.lake/build/bin/cache"
            binary.parent.mkdir()
            binary.write_bytes(b"cache executable")
            os.chmod(binary, 0o555)  # as Lake leaves a cached artifact
            os.utime(binary, ns=(1_000_000_000, 1_000_000_000))
            os.link(binary, cache)
            return rc

        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)
        with s.key_lock(self.P, key):
            entry, hit = s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", extract)
        self.assertFalse(hit)
        published = entry / "build/mathlib/bin/cache"
        self.assertEqual(published.read_bytes(), b"cache executable")
        self.assertEqual(published.stat().st_nlink, 1)
        self.assertEqual(published.stat().st_mode & 0o777, 0o555)
        self.assertEqual(published.stat().st_mtime_ns, 1_000_000_000)
        # The cache keeps its own file; nothing outside the pool names a published inode.
        self.assertEqual(cache.read_bytes(), b"cache executable")
        self.assertEqual(cache.stat().st_nlink, 1)
        self.assertNotEqual(cache.stat().st_ino, published.stat().st_ino)
        self.assertEqual([p.name for p in published.parent.iterdir()], ["cache"])  # no temporary left
        self.assertTrue(s.ready_entry(entry, key, spec))
        # The check itself is unchanged: an alias made later is still refused.
        os.link(published, self.root / "alias")
        with self.assertRaisesRegex(s.IntegrityFailure, "hardlink alias: mathlib/bin/cache"):
            s.ready_entry(entry, key, spec)

    def test_a_file_that_cannot_be_given_its_own_inode_blocks_publication(self):
        def extract(command, cwd, env, output, timeout):
            rc = self.extract(command, cwd, env, output, timeout)
            os.link(cwd / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean", self.root / "alias")
            self.clone_error = OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
            return rc

        if REAL:
            self.skipTest("needs a failing primitive")
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)
        with s.key_lock(self.P, key), self.assertRaises(OSError):
            s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", extract)
        self.assertFalse((self.P / "entries" / key).exists())
        self.assertEqual(list((self.P / "staging").iterdir()), [])

    def test_an_unused_entry_is_collected_like_any_other(self):
        key, _, entry, _, _, _ = self.published()
        self.assertEqual(s.collect(self.pool, max_age=0, now=time.time() + 10), [str(entry), str(self.source)])
        self.assertFalse(entry.exists())
        self.assertEqual(list((self.P / "trash").iterdir()), [])


class IntegrityTests(MacFixture):
    """macOS was seen updating the ctime, and nothing else, of published files: content decides."""

    NAME = "mathlib/lib/lean/Mathlib.olean"

    def setUp(self):
        super().setUp()
        self.key, self.spec, self.entry, _, _, _ = self.published()
        self.file = self.entry / "build" / self.NAME
        self.record = read_json(self.entry / "ready.json")

    def touch(self, path=None):
        """Change a file's ctime and nothing else."""
        path = path or self.file
        before = path.stat()
        time.sleep(0.01)
        os.chmod(path, before.st_mode & 0o7777)
        after = path.stat()
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(s._fields(after)[:5], s._fields(before)[:5])

    def rewrite(self, content, *, keep_mtime=True):
        """Overwrite the published file in place, as its owner can."""
        before = self.file.stat()
        os.chmod(self.file, 0o644)
        with self.file.open("r+b") as out:
            out.write(content)
            out.truncate()
        if keep_mtime:
            os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.chmod(self.file, before.st_mode & 0o7777)

    def assert_refused(self, pattern="snapshot integrity failure"):
        with patch.object(s, "_content", side_effect=AssertionError("content must not decide this")):
            with self.assertRaisesRegex(s.IntegrityFailure, pattern):
                s.ready_entry(self.entry, self.key, self.spec)
        self.assertEqual(read_json(self.entry / "ready.json"), self.record)  # never refreshed

    def test_publication_records_a_digest_of_every_file(self):
        self.assertEqual(self.record["digest"], "sha256")
        self.assertEqual(self.record["digests"].keys(), self.record["inventory"].keys())
        self.assertEqual(self.record["inventory"], s.inventory(self.entry / "build"))
        import hashlib

        self.assertEqual(self.record["digests"][self.NAME], hashlib.sha256(self.artifact).hexdigest())

    def test_an_untouched_entry_is_reused_without_reading_any_content(self):
        with patch.object(s, "_content", side_effect=AssertionError("no file should be read")):
            self.assertTrue(s.ready_entry(self.entry, self.key, self.spec))

    def test_a_ctime_only_change_with_intact_content_is_accepted_and_paid_for_once(self):
        self.touch()
        other = self.entry / "build/batteries/lib/lean/Batteries.olean"
        self.touch(other)
        with patch.object(s, "_content", wraps=s._content) as content:
            self.assertTrue(s.ready_entry(self.entry, self.key, self.spec))
        self.assertEqual(sorted(call.args[0] for call in content.call_args_list), sorted([self.file, other]))
        refreshed = read_json(self.entry / "ready.json")
        self.assertEqual(refreshed["inventory"], s.inventory(self.entry / "build"))
        self.assertEqual(refreshed["digests"], self.record["digests"])
        self.assertEqual((refreshed["key"], refreshed["spec"]), (self.key, self.spec))
        # The new ctimes are recorded: the next reuse reads nothing.
        with patch.object(s, "_content", side_effect=AssertionError("no file should be read again")):
            self.assertTrue(s.ready_entry(self.entry, self.key, self.spec))
        self.assertFalse((self.pool / "safety-stop.json").exists())

    def test_a_round_starts_after_a_ctime_only_change(self):
        for path in (self.entry / "build").rglob("*"):
            if path.is_file():
                self.touch(path)
        with self.view():
            self.assertEqual((self.link() / ".lake/build/lib/lean/Mathlib.olean").read_bytes(), self.artifact)
        self.assertEqual(self.downloads, 1)
        self.assertFalse((self.pool / "safety-stop.json").exists())

    def test_the_round_start_validation_accepts_a_ctime_only_change_too(self):
        self.touch()
        env = {**self.env, "TAUCETI_SHARED_MATHLIB_KEY": self.key}
        self.assertEqual(
            round_storage.validate_shared_views(env, enforce=True), dict(shared_mathlib="matches ready.json")
        )
        self.rewrite(b"X" * len(self.artifact))
        with self.assertRaisesRegex(round_storage.SharedViewMismatch, "differs from its published digest"):
            round_storage.validate_shared_views(env, enforce=True)

    def test_changed_content_of_the_same_size_and_mtime_is_refused(self):
        before = s._fields(self.file.stat())
        self.rewrite(b"X" * len(self.artifact))
        after = s._fields(self.file.stat())
        self.assertEqual(after[:5], before[:5])  # only the ctime tells, and macOS moves that by itself
        self.assertNotEqual(after[5], before[5])
        with self.assertRaisesRegex(s.IntegrityFailure, f"content of {self.NAME} differs from its published digest"):
            s.ready_entry(self.entry, self.key, self.spec)
        self.assertEqual(read_json(self.entry / "ready.json"), self.record)
        with self.assertRaisesRegex(s.Unavailable, "integrity failure"):
            with self.view():
                self.fail("round started on a changed entry")
        self.assertTrue((self.pool / "safety-stop.json").exists())

    def test_a_changed_mtime_is_refused_as_before(self):
        self.rewrite(self.artifact, keep_mtime=False)  # even with the content intact
        self.assert_refused()

    def test_a_changed_size_is_refused_as_before(self):
        self.rewrite(self.artifact + b"!")
        self.assert_refused()

    def test_a_changed_mode_is_refused_as_before(self):
        os.chmod(self.file, 0o644)
        self.assert_refused()

    def test_a_replaced_file_is_refused_as_before(self):
        before = self.file.stat()
        os.chmod(self.file.parent, 0o755)
        replacement = self.file.with_name("replacement")
        shutil.copy2(self.file, replacement)  # same content, mode, size and mtime; another inode
        os.replace(replacement, self.file)
        os.chmod(self.file.parent, 0o555)
        self.assertNotEqual(self.file.stat().st_ino, before.st_ino)
        self.assertEqual(s._fields(self.file.stat())[1:5], s._fields(before)[1:5])
        self.assert_refused()

    def test_a_second_link_is_refused_as_before(self):
        os.link(self.file, self.root / "alias")
        self.assert_refused("hardlink alias")

    def test_a_missing_or_additional_file_is_refused_as_before(self):
        self.touch()
        os.chmod(self.file.parent, 0o755)
        (self.file.parent / "Extra.olean").write_bytes(b"extra")
        self.assert_refused()
        (self.file.parent / "Extra.olean").unlink()
        (self.file.parent / "Mathlib.ilean").unlink()
        self.assert_refused()

    def test_an_entry_published_without_digests_is_refused_on_a_ctime_change(self):
        legacy = {name: value for name, value in self.record.items() if name not in ("digest", "digests")}
        (self.entry / "ready.json").write_text(json.dumps(legacy))
        self.record = legacy
        self.assertTrue(s.ready_entry(self.entry, self.key, self.spec))
        self.touch()
        self.assert_refused()

    def test_linux_never_accepts_a_ctime_change_whatever_the_record_holds(self):
        self.touch()
        with patch.object(s.platform, "system", return_value="Linux"):
            self.assert_refused()

    def test_a_file_that_keeps_changing_while_it_is_read_is_refused(self):
        self.touch()
        real = os.fstat
        ticks = iter(range(1, 100))

        def moving(fd):
            info = list(real(fd))
            return os.stat_result([*info[:9], info[9] + next(ticks)])  # st_ctime in whole seconds

        def fields(info):
            return [info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime, info.st_ctime]

        with patch.object(s.os, "fstat", side_effect=moving), patch.object(s, "_fields", side_effect=fields):
            with self.assertRaisesRegex(s.Unavailable, "keeps changing while it is read"):
                s._content(self.file)


class ViewTests(MacFixture):
    def test_a_view_is_a_private_writable_clone_and_the_shared_state_stays_untouched(self):
        with self.view() as prefix:
            self.assertTrue(prefix)  # callers and the lake guard know a view by its non-empty prefix
            key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
            marker = s.view_marker(self.cwd)
            self.assertEqual((marker["backend"], marker["rev"], marker["key"]), ("clone", self.rev, key))
            generation = self.cwd / ".lake" / s.META / marker["generation"]
            # The link targets are the same in every round; only `current` names the generation.
            self.assertEqual(os.readlink(self.current), generation.name)
            for name in ("mathlib", "batteries"):
                self.assertEqual(os.readlink(self.link(name)), str(self.current / name))
                self.assertEqual(self.link(name).resolve(), generation / name)
            entry = self.P / "entries" / key
            lower = entry / "build/mathlib/lib/lean/Mathlib.olean"
            file = self.link() / ".lake/build/lib/lean/Mathlib.olean"
            self.assertEqual(s.upper_usage(generation), dict(upper_files=0, upper_bytes=0))
            time.sleep(0.05)  # a coarse filesystem clock must tell the round's writes from the clone
            # Commands run with the prefix see the clone through the checkout's own package path.
            p = self.run_in(prefix, "sh", "-c", 'cat "$1"; printf copyup > "$1"', "sh", str(file))
            self.assertEqual((p.returncode, p.stdout), (0, self.artifact), p.stderr)
            self.assertEqual(file.read_bytes(), b"copyup")
            self.assertEqual(lower.read_bytes(), self.artifact)
            self.assertNotEqual(file.stat().st_ino, lower.stat().st_ino)
            # Lake compares times: an untouched clone is exactly as old as the published file.
            other = "lib/lean/Mathlib/Test.olean"
            self.assertEqual(
                (self.link() / ".lake/build" / other).stat().st_mtime_ns,
                (entry / "build/mathlib" / other).stat().st_mtime_ns,
            )
            named = self.run_in(prefix, "sh", "-c", 'printf %s "$TAUCETI_SHARED_MATHLIB_CLONE"')
            self.assertEqual(named.stdout.decode(), str(generation))
            # Lake's `.hash` files beside inputs, and new build outputs, stay in the round.
            (self.link("batteries") / "widget/package-lock.json.hash").write_text("123")
            (self.link() / ".lake/build/lib/lean/New.olean").write_bytes(b"new")
            self.assertFalse((self.source / "batteries/widget/package-lock.json.hash").exists())
            self.assertFalse((entry / "build/mathlib/lib/lean/New.olean").exists())
            self.assertEqual(s.upper_usage(generation), dict(upper_files=3, upper_bytes=len(b"copyup") + 3 + 3))
            # The clone is the pinned checkout Lake expects, and Git cannot move or refetch it.
            self.assertEqual(git(self.link(), "rev-parse", "HEAD"), self.rev)
            self.assertEqual(git(self.link(), "remote", "get-url", "origin"), str(self.mathlib_repo))
            self.assertEqual(subprocess.run(["git", "-C", str(self.link()), "diff", "--quiet", "HEAD"]).returncode, 0)
            self.assertEqual(writable_bits(generation / "mathlib/.git"), [])
            self.assertNotEqual(
                subprocess.run(
                    ["git", "-C", str(self.link()), "fetch", "-q", str(self.mathlib_repo)], capture_output=True
                ).returncode,
                0,
            )
            # Source files keep their read-only mode, as the lower of an overlay does.
            self.assertFalse((self.link() / "Mathlib/Test.lean").stat().st_mode & 0o222)
            # The version stays leased for the whole round, as in an overlay view.
            for lock in (key, "source-" + self.rev):
                with self.assertRaises(BlockingIOError), s.key_lock(self.P, lock, nonblocking=True):
                    pass
            self.assertEqual(len(s.lease_fds(self.env)), 3)
            self.assertEqual(s.collect(self.pool, max_age=0, now=time.time() + 10), [])
            self.assertTrue(s.ready_entry(entry, key, read_json(entry / "ready.json")["spec"]))
        # Outside the view nothing usable remains, and the shared state is as published.
        self.assert_detached()
        self.assertEqual(self.generations(), [])
        self.assertEqual(lower.read_bytes(), self.artifact)
        self.assertEqual(writable_bits(entry / "build"), [])
        self.assertEqual(writable_bits(self.source), [])
        self.assertEqual(git(self.source / "mathlib", "status", "--porcelain"), "")
        self.assertFalse((self.pool / "safety-stop.json").exists())
        receipts = [json.loads(line) for line in (self.pool / "results.jsonl").read_text().splitlines()]
        self.assertEqual((receipts[-1]["phase"], receipts[-1]["upper_files"]), ("cleanup", 3))

    def test_the_next_round_starts_from_the_published_build_again(self):
        with self.view():
            build = self.link() / ".lake/build/lib/lean"
            (build / "Mathlib/Test.ilean").unlink()
            (build / "Mathlib.olean").write_bytes(b"private")
            first = s.view_marker(self.cwd)["generation"]
        self.assertEqual(self.generations(), [])
        with self.view():
            marker = s.view_marker(self.cwd)
            self.assertNotEqual(marker["generation"], first)
            self.assertEqual(self.generations(), [marker["generation"]])
            for name in ("Mathlib.olean", "Mathlib/Test.ilean"):
                self.assertEqual((self.link() / ".lake/build/lib/lean" / name).read_bytes(), self.artifact)
        self.assertEqual(self.downloads, 1)  # one stored build, cloned twice
        self.assertFalse((self.pool / "safety-stop.json").exists())
        self.assertEqual(s.summary(self.env)["private"], 0)

    def test_a_surviving_process_keeps_its_generation_but_not_the_checkout_links(self):
        with self.view() as prefix:
            first = self.cwd / ".lake" / s.META / s.view_marker(self.cwd)["generation"]
            survivor = subprocess.Popen(
                prefix + ["sh", "-c", "echo ready; read line"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                pass_fds=s.lease_fds(self.env),
            )
            self.assertEqual(survivor.stdout.readline().strip(), "ready")
        try:
            self.assert_detached()
            self.assertTrue((first / "mathlib/.lake/build/lib/lean/Mathlib.olean").exists())
            with self.view():
                self.assertTrue(first.exists())
                self.assertNotEqual(self.link().resolve(), first / "mathlib")
        finally:
            survivor.stdin.close()
            survivor.wait(timeout=5)
            survivor.stdout.close()
        deadline = time.monotonic() + 5
        while first.exists() and time.monotonic() < deadline:
            s.discard_private_state(self.cwd)
            time.sleep(0.05)
        self.assertFalse(first.exists())

    def test_a_killed_round_is_repaired_by_the_next_one(self):
        # A worker killed inside a view runs no cleanup: the links still name its clones.
        with self.view():
            stale = self.cwd / ".lake" / s.META / s.view_marker(self.cwd)["generation"]
            kept = self.root / "kept"
            os.rename(stale, kept)
        os.rename(kept, stale)
        s._point(self.current, stale.name)
        for name in ("mathlib", "batteries"):
            s._point(self.link(name), self.current / name)
        self.assertEqual((self.link() / ".lake/build/lib/lean/Mathlib.olean").read_bytes(), self.artifact)
        with self.view():
            self.assertFalse(stale.exists())
            generation = self.cwd / ".lake" / s.META / s.view_marker(self.cwd)["generation"]
            self.assertEqual(self.link().resolve(), generation / "mathlib")
        self.assert_detached()
        self.assertEqual(self.generations(), [])

    def test_a_body_failure_still_leaves_the_view(self):
        with self.assertRaisesRegex(RuntimeError, "agent failed"):
            with self.view():
                raise RuntimeError("agent failed")
        self.assert_detached()
        self.assertEqual(self.generations(), [])
        with s.key_lock(self.P, "source-" + self.rev, nonblocking=True):
            pass


class RefusalTests(MacFixture):
    """NoFallback: where a clone cannot be made the round is refused; nothing is copied or built."""

    def assert_no_private_mathlib(self):
        self.assert_detached()
        self.assertEqual(self.generations(), [])
        self.assertFalse((self.pool / "safety-stop.json").exists())

    def test_a_pool_on_another_volume_is_refused_with_the_reason(self):
        self.published()
        clones = self.clones
        with patch.object(s, "_same_volume", return_value=False):
            self.assert_blocked("different volumes; clonefile cannot cross volumes")
        self.assert_no_private_mathlib()
        if not REAL:
            # Only the two capability probes cloned anything: no package was copied.
            self.assertEqual(self.clones - clones, 2)

    @unittest.skipIf(REAL, "needs a failing primitive")
    def test_cloning_that_fails_midway_is_refused_and_leaves_no_partial_mathlib(self):
        key, spec, entry, _, _, _ = self.published()
        calls = []

        def failing(source, destination):
            calls.append(source)
            if "entries" in Path(source).parts:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            copy(source, destination)

        with patch.object(s, "clone_file", side_effect=failing):
            self.assert_blocked("Invalid cross-device link|Cross-device link")
        self.assertTrue(any("entries" in Path(source).parts for source in calls))
        self.assert_no_private_mathlib()
        self.assertTrue(s.ready_entry(entry, key, spec))
        self.assertEqual(self.downloads, 1)

    @unittest.skipIf(REAL, "needs a filesystem without clones")
    def test_no_clone_support_never_degrades_into_a_copy_or_a_private_build(self):
        self.published()
        self.clone_error = OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        self.assert_blocked("cannot clone files")
        self.assert_no_private_mathlib()
        self.assertEqual(self.downloads, 1)

    def test_switching_sharing_off_blocks_as_on_linux(self):
        self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "off"
        with self.assertRaises(s.Disabled):
            with self.view():
                self.fail("round started without shared Mathlib")
        self.assertFalse(self.link().exists())


if __name__ == "__main__":
    unittest.main()

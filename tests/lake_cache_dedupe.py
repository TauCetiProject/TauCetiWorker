"""Kernel-range sharing preserves live files, and never trusts a filename as proof."""

import errno
import importlib.machinery
import importlib.util
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dedupe-lake-caches"
loader = importlib.machinery.SourceFileLoader("lake_cache_dedupe", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


class Dedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def artifact(self, parent, contents=b"a" * 8192, *, name="a" * 16 + ".olean", mode=0o444):
        path = self.root / parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(mode)
        return path

    @staticmethod
    def kernel(src_fd, operation, request, mutate):
        assert operation == module.FIDEDUPERANGE
        off, length, count, _, _, dst_fd, dst_off, _, _, _ = struct.unpack("=QQHHIqQQiI", request)
        assert count == 1
        same = os.pread(src_fd, length, off) == os.pread(dst_fd, length, dst_off)
        request[:] = struct.pack(
            "=QQHHIqQQiI", off, length, 1, 0, 0, dst_fd, dst_off, length if same else 0, 0 if same else 1, 0
        )

    def test_equal_contents_keep_names_modes_inodes_and_hardlinks(self):
        a, b = self.artifact("a"), self.artifact("b")
        alias = b.with_name("alias")
        os.link(b, alias)
        before = [(p.stat().st_ino, p.stat().st_mode, p.read_bytes()) for p in (a, b, alias)]
        with patch.object(module.fcntl, "ioctl", self.kernel):
            self.assertEqual(
                module.dedupe(a, b, module.identity(a.stat()), module.identity(b.stat())), (8192, "matched")
            )
        self.assertEqual(before, [(p.stat().st_ino, p.stat().st_mode, p.read_bytes()) for p in (a, b, alias)])

    def test_same_name_is_not_proof_of_equal_contents(self):
        a, b = self.artifact("a"), self.artifact("b", b"b" * 8192)
        with patch.object(module.fcntl, "ioctl", self.kernel):
            self.assertEqual(
                module.dedupe(a, b, module.identity(a.stat()), module.identity(b.stat())), (0, "different")
            )
        self.assertEqual(b.read_bytes(), b"b" * 8192)

    def test_symlinks_replaced_inodes_and_writable_files_are_skipped(self):
        a, b = self.artifact("a"), self.artifact("b")
        expected = module.identity(b.stat())
        b.unlink()
        b.symlink_to(a)
        with self.assertRaises(OSError):
            module.dedupe(a, b, module.identity(a.stat()), expected)
        b.unlink()
        b = self.artifact("b", mode=0o644)
        with patch.object(module.fcntl, "ioctl") as ioctl:
            self.assertEqual(module.dedupe(a, b, module.identity(a.stat()), expected), (0, "changed"))
            ioctl.assert_not_called()

    def test_deadline_and_index_bound_do_not_delete_files(self):
        a, b = self.artifact("a"), self.artifact("b")
        with patch.object(module.fcntl, "ioctl", self.kernel):
            result = module.run([a.parent, b.parent], seconds=10, max_keys=1)
            self.assertEqual(result["indexed"], 1)
            self.assertEqual(result["matched_range_bytes"], 8192)
            self.assertEqual(
                module.dedupe(
                    a, b, module.identity(a.stat()), module.identity(b.stat()), deadline=time.monotonic() - 1
                ),
                (0, "deadline"),
            )
        self.assertTrue(a.exists() and b.exists())

    def test_unsupported_filesystem_stops_without_changing_data(self):
        a, b = self.artifact("a"), self.artifact("b")
        with patch.object(module.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            result = module.run([a.parent, b.parent])
        self.assertEqual(result["stopped_early"], 1)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_discovery_rejects_cache_roots_outside_fleet(self):
        base = self.root / "fleet"
        cache = (
            base
            / "worker-runtimes/rev/lib/python3.13/site-packages/tauceti_worker/state/worker/home/.cache/lake/artifacts"
        )
        cache.mkdir(parents=True)
        self.assertEqual(module.roots(base), [cache])
        cache.rmdir()
        outside = self.root / "operator-cache"
        outside.mkdir()
        cache.symlink_to(outside, target_is_directory=True)
        self.assertEqual(module.roots(base), [])

    def test_real_kernel_keeps_future_writes_private(self):
        a, b = self.artifact("a"), self.artifact("b")
        different = self.artifact("different", b"b" * 8192)
        alias = b.with_name("alias")
        os.link(b, alias)
        before = [(p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in (a, b, different)]
        try:
            result = module.dedupe(a, b, module.identity(a.stat()), module.identity(b.stat()))
        except OSError as error:
            if error.errno in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL):
                self.skipTest("fixture filesystem does not support extent deduplication")
            raise
        self.assertEqual(result, (8192, "matched"))
        self.assertEqual(
            module.dedupe(a, different, module.identity(a.stat()), module.identity(different.stat())),
            (0, "different"),
        )
        self.assertEqual(
            before,
            [(p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in (a, b, different)],
        )
        b.chmod(0o644)
        with b.open("r+b") as stream:
            stream.write(b"changed")
        self.assertEqual(a.read_bytes(), b"a" * 8192)
        self.assertEqual(alias.read_bytes(), b.read_bytes())


if __name__ == "__main__":
    unittest.main()

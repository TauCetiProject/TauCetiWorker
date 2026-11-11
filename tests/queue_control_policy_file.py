#!/usr/bin/env python3
"""Private queue credentials are loaded from one checked source-bound snapshot."""

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_source_authority as fixtures

from tauceti_queue import control_policy_file
from tauceti_queue.control_policy_file import MAX_POLICY_BYTES, ControlPolicyError
from tauceti_queue.source_authority import NativeQueueAuthority, QueueControlPolicy, SourceAccessDenied


@unittest.skipUnless(os.name == "posix", "native private policy requires POSIX ownership checks")
class PolicyFile(unittest.TestCase):
    def setUp(self):
        self.native = fixtures.SourceAuthority()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.source = self.native.identity
        self.policy = self.native.authority._control_policy
        temporary = tempfile.TemporaryDirectory(prefix="queue-control-policy-")
        self.addCleanup(temporary.cleanup)
        # Canonicalize our newly created fixture, never an untrusted policy
        # path. macOS /var and /tmp are symlinks into /private.
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "control.json"
        self.write(self.policy.record())

    def write(self, row, *, path=None):
        path = path or self.path
        path.write_text(json.dumps(row))
        path.chmod(0o600)

    def load(self, path=None):
        return QueueControlPolicy.load(path or self.path, source=self.source)

    def test_checked_private_file_round_trip_and_readonly_mode(self):
        self.assertEqual(self.load(), self.policy)
        self.path.chmod(0o400)
        before = self.path.stat()
        self.assertEqual(self.load(), self.policy)
        after = self.path.stat()
        self.assertEqual(
            (before.st_ino, before.st_size, before.st_mtime_ns), (after.st_ino, after.st_size, after.st_mtime_ns)
        )

    def test_the_full_supported_1024_credential_policy_can_be_loaded(self):
        maximum = QueueControlPolicy(self.source, frozenset(f"{number:064x}" for number in range(1, 1025)))
        self.write(maximum.record())
        self.assertGreater(self.path.stat().st_size, 60 * 1024)
        self.assertEqual(self.load(), maximum)

    def test_exact_schema_and_source_origin_name_and_id_are_required(self):
        rows = []
        original = self.policy.record()
        for field, value in (
            ("schema", "older"),
            ("fingerprints", []),
            ("fingerprints", ["bad"]),
            ("token", "private-bearer"),
        ):
            rows.append({**original, field: value})
        for key, value in (("id", self.source.native_id + 1), ("name", "other/repo")):
            changed = copy.deepcopy(original)
            changed["source"][key] = value
            rows.append(changed)
        changed = copy.deepcopy(original)
        changed["source"]["forge"]["url"] = "https://other.example"
        rows.append(changed)
        rows.append({**original, "fingerprints": [next(iter(self.policy.fingerprints))] * 2})
        rows.append({**original, "fingerprints": [["unhashable-private-bearer"]]})
        for row in rows:
            with self.subTest(row=row):
                self.write(row)
                with self.assertRaises(ControlPolicyError) as raised:
                    self.load()
                self.assertNotIn("private-bearer", str(raised.exception))
                self.assertIsNone(raised.exception.__context__)
                self.assertNotIn(next(iter(self.policy.fingerprints)), str(raised.exception))

    def test_json_duplicates_invalid_numbers_unicode_and_nesting_are_refused(self):
        for raw in (
            b'{"schema":1,"schema":2}',
            b'{"fingerprints":[NaN]}',
            b"\xff",
            b'{"private-bearer":',
            b"[" * 2000 + b"0" + b"]" * 2000,
            b'{"fingerprints":[["unhashable-private-bearer"]]}',
        ):
            with self.subTest(raw=raw[:32]):
                self.path.write_bytes(raw)
                with self.assertRaises(ControlPolicyError) as raised:
                    self.load()
                self.assertNotIn("private-bearer", str(raised.exception))
                self.assertIsNone(raised.exception.__context__)

    def test_public_group_access_and_special_file_modes_are_refused(self):
        for mode in (0o644, 0o640, 0o620, 0o604, 0o4600, 0o700, 0o200):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(ControlPolicyError):
                    self.load()

    def test_symlink_file_and_symlink_ancestor_are_never_followed(self):
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(ControlPolicyError):
            self.load(link)
        child = self.root / "child"
        child.mkdir(mode=0o700)
        self.write(self.policy.record(), path=child / "control.json")
        directory_link = self.root / "linked-directory"
        directory_link.symlink_to(child, target_is_directory=True)
        with self.assertRaises(ControlPolicyError):
            self.load(directory_link / "control.json")

    def test_multiple_links_directory_fifo_empty_and_oversized_files_are_refused(self):
        os.link(self.path, self.root / "hard-link.json")
        with self.assertRaises(ControlPolicyError):
            self.load()
        (self.root / "hard-link.json").unlink()
        with self.assertRaises(ControlPolicyError):
            self.load(self.root)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ControlPolicyError):
            self.load(fifo)  # O_NONBLOCK: an unreadable FIFO cannot hang startup.
        for payload in (b"", b" " * (MAX_POLICY_BYTES + 1)):
            self.path.write_bytes(payload)
            with self.assertRaises(ControlPolicyError):
                self.load()

    def test_foreign_file_ownership_is_refused(self):
        original = os.fstat

        def foreign(descriptor):
            info = original(descriptor)
            if stat.S_ISREG(info.st_mode):
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        with patch.object(control_policy_file.os, "fstat", side_effect=foreign):
            with self.assertRaises(ControlPolicyError):
                self.load()

    def test_a_directory_target_does_not_leak_its_open_descriptor(self):
        target = self.root / "not-a-policy"
        target.mkdir(mode=0o700)
        original = os.open
        descriptors = []

        def capture(path, flags, *args, **kwargs):
            descriptor = original(path, flags, *args, **kwargs)
            if path == target.name:
                descriptors.append(descriptor)
            return descriptor

        with patch.object(control_policy_file.os, "open", side_effect=capture):
            with self.assertRaises(ControlPolicyError):
                self.load(target)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_a_foreign_directory_owner_is_refused(self):
        original = os.fstat
        inode = self.root.stat().st_ino

        def foreign(descriptor):
            info = original(descriptor)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == inode:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        with patch.object(control_policy_file.os, "fstat", side_effect=foreign):
            with self.assertRaises(ControlPolicyError):
                self.load()

    def test_a_private_fixture_under_a_root_owned_sticky_ancestor_is_supported(self):
        temporary_root = Path("/tmp").resolve()
        info = temporary_root.stat()
        if info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            self.skipTest("host has no root-owned sticky temporary ancestor")
        with tempfile.TemporaryDirectory(prefix="queue-sticky-policy-", dir=temporary_root) as temporary:
            path = Path(temporary).resolve() / "control.json"
            self.write(self.policy.record(), path=path)
            self.assertEqual(self.load(path), self.policy)

    def test_untrusted_directory_write_access_and_relative_traversal_are_refused(self):
        for mode in (0o770, 0o707, 0o755):
            self.root.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(ControlPolicyError):
                self.load()
        self.root.chmod(0o700)
        outer = self.root / "shared"
        outer.mkdir(mode=0o700)
        inner = outer / "private"
        inner.mkdir(mode=0o700)
        self.write(self.policy.record(), path=inner / "control.json")
        outer.chmod(0o770)
        with self.assertRaises(ControlPolicyError):
            self.load(inner / "control.json")
        for path in (Path("relative.json"), self.root / ".." / self.root.name / "control.json"):
            with self.subTest(path=path), self.assertRaises(ControlPolicyError):
                self.load(path)

    def test_inplace_change_during_read_is_refused(self):
        original = os.fstat
        regular_reads = []

        def mutate(descriptor):
            info = original(descriptor)
            if stat.S_ISREG(info.st_mode):
                regular_reads.append(info)
                if len(regular_reads) == 2:
                    with self.path.open("ab") as output:
                        output.write(b" ")
                    return original(descriptor)
            return info

        with patch.object(control_policy_file.os, "fstat", side_effect=mutate):
            with self.assertRaisesRegex(ControlPolicyError, "changed while"):
                self.load()

    def test_path_replacement_cannot_redirect_an_already_open_policy_descriptor(self):
        original = os.open
        replacement = QueueControlPolicy(self.source, frozenset({"f" * 64}))
        swapped = []

        def swap(path, flags, *args, **kwargs):
            descriptor = original(path, flags, *args, **kwargs)
            if path == self.path.name and not swapped:
                swapped.append(True)
                self.path.rename(self.root / "opened-policy.json")
                self.write(replacement.record())
            return descriptor

        with patch.object(control_policy_file.os, "open", side_effect=swap):
            self.assertEqual(self.load(), self.policy)
        self.assertEqual(self.load(), replacement)

    def test_factory_uses_snapshot_but_never_caches_successful_native_acl(self):
        authority = NativeQueueAuthority.from_control_file(
            self.source, self.path, client_factory=self.native.authority._client_factory
        )
        self.assertEqual(self.native.clients, [])
        authority.require_admin(authority.validate(authority.authenticate("token")))
        self.native.repo["permissions"]["admin"] = False
        with self.assertRaises(SourceAccessDenied):
            authority.require_admin(authority.validate(authority.authenticate("token")))
        self.assertEqual(len(self.native.calls), 2)
        self.native.repo["permissions"]["admin"] = True
        self.write(QueueControlPolicy(self.source, frozenset({"f" * 64})).record())
        replacement = NativeQueueAuthority.from_control_file(
            self.source, self.path, client_factory=self.native.authority._client_factory
        )
        before = len(self.native.calls)
        with self.assertRaises(SourceAccessDenied):
            replacement.authenticate("token")
        self.assertEqual(len(self.native.calls), before)


if __name__ == "__main__":
    unittest.main()

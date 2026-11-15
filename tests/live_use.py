#!/usr/bin/env python3
"""Process ownership and live-use observations that protect trees from cleanup (Linux and macOS)."""

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import live_use as lu  # noqa: E402
from tauceti_worker import retention  # noqa: E402


class LiveUse(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_unavailable_process_discovery_never_authorizes_deletion(self):
        tree = self.root / "tree"
        tree.mkdir()
        with patch.object(lu, "process_references", return_value=None):
            self.assertTrue(lu.live_users(tree))
            with patch.object(sys, "platform", "darwin"):
                self.assertTrue(lu.live_users(tree))
        with patch.object(lu, "process_references", return_value=[]):
            self.assertEqual(lu.live_users(tree), [])

    @unittest.skipUnless(sys.platform == "linux", "procfs observations")
    def test_linux_cwd_reference_and_owner_birth(self):
        tree = self.root / "tree"
        tree.mkdir()
        code = "import json, time; from tauceti_worker.live_use import process_owner; print(json.dumps(process_owner()), flush=True); time.sleep(30)"
        with subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=tree,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                owner = json.loads(child.stdout.readline())
                self.assertFalse(lu.owner_stopped(owner))
                self.assertTrue(any(user.startswith(f"{child.pid} ") for user in lu.live_users(tree)))
            finally:
                child.terminate()
                child.wait(timeout=5)
        self.assertTrue(lu.owner_stopped(owner))
        self.assertEqual(lu.live_users(tree), [])

    def test_checkout_kind(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        self.assertEqual(lu.checkout_kind(repo), "standalone")
        tree = self.root / "tree"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree)], check=True)
        self.assertEqual(lu.checkout_kind(tree), "worktree")
        self.assertEqual(lu.common_dir(tree), (repo / ".git").resolve())
        self.assertEqual(lu.rev(tree, "HEAD"), lu.rev(repo, "HEAD"))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(lu.checkout_kind(plain))

    def test_darwin_process_references_protect_cwd_files_and_session(self):
        class ProcessError(Exception):
            pass

        class Gone(ProcessError):
            pass

        class Denied(ProcessError):
            pass

        def process(pid, cwd="/", files=()):
            value = Mock(pid=pid)
            value.status.return_value = "running"
            value.uids.return_value = SimpleNamespace(real=os.getuid(), effective=os.geteuid())
            value.cwd.return_value = cwd
            value.open_files.return_value = [SimpleNamespace(path=path) for path in files]
            return value

        tree = self.root / "retained"
        tree.mkdir()
        alias = self.root / "retained-alias"
        alias.symlink_to(tree, target_is_directory=True)
        own = process(os.getpid(), str(tree))
        own.ppid.return_value = 101
        parent = process(101, str(tree))
        parent.ppid.return_value = 1
        cwd = process(102, str(tree / "nested"))
        opened = process(103, "/", [str(tree / "Draft.lean")])
        member = process(104)
        sibling = process(105, str(tree) + "-other")
        foreign = process(106)
        foreign.uids.return_value = SimpleNamespace(real=os.getuid() + 1, effective=os.geteuid() + 1)
        zombie = process(107, str(tree))
        zombie.status.return_value = "zombie"
        procs = {p.pid: p for p in (own, parent, cwd, opened, member, sibling, foreign, zombie)}

        def lookup(pid=None):
            if pid == 108:
                raise Gone()
            return procs[os.getpid() if pid is None else pid]

        api = SimpleNamespace(
            Process=Mock(side_effect=lookup),
            pids=lambda: [*procs, 108],
            Error=ProcessError,
            NoSuchProcess=Gone,
            AccessDenied=Denied,
            STATUS_ZOMBIE="zombie",
            STATUS_DEAD="dead",
        )
        with (
            patch.dict(sys.modules, psutil=api),
            patch.object(sys, "platform", "darwin"),
            patch.object(os, "geteuid", return_value=501),
            patch.object(os, "getuid", return_value=501),
            patch.object(os, "getsid", side_effect=lambda pid: 104 if pid == 104 else 1),
            patch.object(Path, "read_text", side_effect=AssertionError("Darwin must not read procfs")),
        ):
            # Fixture UIDs were initialized before the host UID mocks.
            for p in (cwd, opened, member, sibling):
                p.uids.return_value = SimpleNamespace(real=501, effective=501)
            foreign.uids.return_value = SimpleNamespace(real=502, effective=502)
            with patch.object(os.path, "realpath", side_effect=AssertionError("do not resolve observed paths")):
                refs = lu.process_references()
            self.assertEqual(lu.referenced_users(alias, refs), ["102 (process)", "103 (process)"])
            self.assertIn("104 (owner session)", lu.referenced_users(tree, refs, session=104))
            parent.cwd.assert_not_called()
            foreign.cwd.assert_not_called()
            cwd.open_files.side_effect = OSError("fixture unreadable private descriptors")
            self.assertIn("102 (process)", lu.referenced_users(tree, lu.process_references()))
            member.cwd.side_effect = ProcessError("fixture unreadable private cwd")
            member.open_files.side_effect = Denied("fixture inaccessible private descriptors")
            self.assertIn("104 (owner session)", lu.referenced_users(tree, lu.process_references(), session=104))
            api.pids = Mock(side_effect=ProcessError("fixture unavailable inventory"))
            self.assertIsNone(lu.process_references())
            self.assertTrue(lu.live_users(tree))

    def test_darwin_missing_psutil_defers_cleanup_and_launchers_declare_it(self):
        repo = Path(__file__).resolve().parents[1]
        header = (repo / "tauceti").read_text().split("# /// script\n", 1)[1].split("# ///", 1)[0]
        shim = tomllib.loads("\n".join(line.removeprefix("# ") for line in header.splitlines()))
        project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
        for metadata in (shim, project):
            self.assertIn("psutil>=7; sys_platform == 'darwin'", metadata["dependencies"])
        with patch.dict(sys.modules, psutil=None), patch.object(sys, "platform", "darwin"):
            self.assertTrue(lu.live_users(self.root))
            self.assertIsNone(retention.mount_points())

    @unittest.skipUnless(sys.platform == "darwin", "native macOS process observations")
    def test_native_darwin_process_references_and_owner_birth(self):
        import select

        import psutil

        tree = self.root / "native-checkout"
        tree.mkdir()
        source = tree / "Draft.lean"
        source.write_text("keep live source")
        code = (
            "import json, sys, time; from tauceti_worker.live_use import process_owner; "
            "held = open(sys.argv[1]); print(json.dumps(process_owner()), flush=True); time.sleep(30)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code, str(source)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "fixture child did not start")
                owner = json.loads(child.stdout.readline())
                self.assertEqual(owner["session"], child.pid)
                self.assertFalse(lu.owner_stopped(owner))
                # Constrain discovery to this fixture; use real native calls for it.
                with patch.object(psutil, "pids", return_value=[child.pid]):
                    self.assertEqual(lu.live_users(tree), [f"{child.pid} (process)"])
            finally:
                child.terminate()
                child.wait(timeout=5)
        self.assertTrue(lu.owner_stopped(owner))
        with patch.object(psutil, "pids", return_value=[child.pid]):
            self.assertEqual(lu.live_users(tree), [])
        self.assertTrue(retention.mount_points())

        alias = self.root / "native-checkout-alias"
        alias.symlink_to(tree, target_is_directory=True)
        with subprocess.Popen(["sleep", "30"], cwd=alias) as child:
            try:
                with patch.object(psutil, "pids", return_value=[child.pid]):
                    self.assertEqual(lu.live_users(alias), [f"{child.pid} (process)"])
                    other_case = tree.with_name(tree.name.upper())
                    if other_case.exists():  # Exercise case-insensitive APFS without assuming volume settings.
                        self.assertEqual(lu.live_users(other_case), [f"{child.pid} (process)"])
            finally:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

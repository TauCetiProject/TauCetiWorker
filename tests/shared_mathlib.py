"""Fresh-source publication, leases, fallback and real rootless copy-on-write tests."""

import concurrent.futures
import contextlib
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
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import containment
from tauceti_worker import shared_mathlib as s
from tauceti_worker.runtime_status import atomic_json


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(path, files):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for name, value in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    git(path, "add", ".")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text("")
        git_env = patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1")
        git_env.start()
        self.addCleanup(git_env.stop)
        self.cwd = self.root / "checkout"
        self.mathlib = self.cwd / ".lake/packages/mathlib"
        repository(
            self.cwd,
            {
                "lean-toolchain": "leanprover/lean4:v4.29.0",
                "lakefile.toml": "name = 'fixture'\n",
                ".gitignore": ".lake/\n",
            },
        )
        repository(
            self.mathlib,
            {
                "Mathlib.lean": "import Mathlib.Test\n",
                "Mathlib/Test.lean": "def test := 1\n",
                "lake-manifest.json": '{"packages":[]}',
                ".gitignore": ".lake/\n",
            },
        )
        self.rev = git(self.mathlib, "rev-parse", "HEAD")
        (self.cwd / "lake-manifest.json").write_text(
            json.dumps(dict(packages=[dict(name="mathlib", type="git", rev=self.rev)]))
        )
        git(self.cwd, "add", "lake-manifest.json")
        git(
            self.cwd, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "manifest"
        )
        self.pool = self.root / "pool"
        self.env = {
            **os.environ,
            "TAUCETI_SHARED_MATHLIB_ROOT": str(self.pool),
            "TAUCETI_SHARED_MATHLIB_BUILDS": "auto",
            "TAUCETI_WORKER_ID": "fixture",
        }
        self.library = self.root / "toolchain/lib/lean"
        self.library.mkdir(parents=True)
        (self.library / "Init.olean").write_text("fixture ABI")
        real_run, real_contained = subprocess.run, containment.run

        def fake_lake(command):
            if command == ["lake", "env", "lean", "--version"]:
                return subprocess.CompletedProcess(command, 0, "Lean fixture ABI\n", "")
            if command == ["lake", "env", "lean", "--print-prefix"]:
                return subprocess.CompletedProcess(command, 0, str(self.library.parents[1]) + "\n", "")
            return None

        def run(command, *args, **kwargs):
            return fake_lake(command) or real_run(command, *args, **kwargs)

        def contained(command, *args, **kwargs):
            return fake_lake(command) or real_contained(command, *args, **kwargs)

        for patcher in (
            patch.object(s.subprocess, "run", side_effect=run),
            patch.object(containment, "run", side_effect=contained),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.downloads = 0
        disk = patch.object(
            s.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100_000_000, 20_000_000, 80_000_000)
        )
        disk.start()
        self.addCleanup(disk.stop)

    def extract(self, command, cwd, env, output, timeout, *, prefix=()):
        self.downloads += 1
        self.assertEqual(command, ["lake", "exe", "cache", "get"])
        self.assertNotEqual(cwd, self.cwd)
        self.assertFalse((cwd / ".lake/packages/mathlib/.lake/build/poison").exists())
        build = cwd / ".lake/packages/mathlib/.lake/build/lib/lean"
        for module in ("Mathlib", "Mathlib/Test"):
            for suffix in (".olean", ".ilean"):
                target = build / (module + suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"verified artifact")
        return 0

    def test_signature_includes_revisions_config_toolchain_and_abi(self):
        key, spec = s.signature(self.cwd, self.env)
        self.assertEqual(spec["mathlib"], self.rev)
        (self.cwd / "lean-toolchain").write_text("changed")
        self.assertNotEqual(key, s.signature(self.cwd, self.env)[0])
        (self.mathlib / "Mathlib/Test.lean").write_text("changed")
        with self.assertRaises(s.Unavailable):
            s.signature(self.cwd, self.env)

    def test_signature_binds_external_lake_config_by_content_and_cache_selection(self):
        config = self.root / "lake.toml"
        config.write_text("[cache]\n")
        self.env["LAKE_CONFIG"] = str(config)
        key, _ = s.signature(self.cwd, self.env)
        other = self.root / "another-lake.toml"
        other.write_text(config.read_text())
        self.env["LAKE_CONFIG"] = str(other)
        self.assertEqual(key, s.signature(self.cwd, self.env)[0])
        other.write_text("[cache]\nservice = 'different'\n")
        self.assertNotEqual(key, s.signature(self.cwd, self.env)[0])
        key, _ = s.signature(self.cwd, self.env)
        self.env["MATHLIB_CACHE_GET_URL"] = "https://secret-fixture@cache.invalid"
        changed, spec = s.signature(self.cwd, self.env)
        self.assertNotEqual(key, changed)
        self.assertNotIn("secret-fixture", json.dumps(spec))

    def test_signature_binds_resolved_toolchain_contents(self):
        key, _ = s.signature(self.cwd, self.env)
        (self.library / "Init.olean").write_text("different toolchain, same version output")
        self.assertNotEqual(key, s.signature(self.cwd, self.env)[0])

    def test_changed_dependency_pin_selects_new_snapshot(self):
        key, _ = s.signature(self.cwd, self.env)
        (self.mathlib / "Mathlib/Test.lean").write_text("def test := 2\n")
        git(self.mathlib, "add", ".")
        git(
            self.mathlib,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "new pin",
        )
        manifest = json.loads((self.cwd / "lake-manifest.json").read_text())
        manifest["packages"][0]["rev"] = git(self.mathlib, "rev-parse", "HEAD")
        (self.cwd / "lake-manifest.json").write_text(json.dumps(manifest))
        self.assertNotEqual(key, s.signature(self.cwd, self.env)[0])

    def test_damaged_ready_snapshot_is_rejected_and_preserved(self):
        self.require_overlay()
        for damage in ("remove", "overwrite", "symlink"):
            with self.subTest(damage=damage):
                # Simulate operator acceptance before testing the next failure.
                (self.pool / "policy.json").unlink(missing_ok=True)
                (self.pool / "safety-stop.json").unlink(missing_ok=True)
                (self.cwd / "lean-toolchain").write_text(damage)
                key, _ = s.signature(self.cwd, self.env)
                with s.view(self.cwd, self.env, self.root / "logs", self.extract):
                    pass
                artifact = self.pool / "entries" / key / "build/lib/lean/Mathlib/Test.ilean"
                if damage == "overwrite":
                    os.chmod(artifact, 0o644)
                    artifact.write_bytes(b"corrupt artifact!")
                else:
                    artifact.unlink()
                    if damage == "symlink":
                        artifact.symlink_to(self.library / "Init.olean")
                before = self.downloads
                with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                    self.assertEqual(prefix, [])
                self.assertIn("integrity", " ".join(s.summary(self.env)["reasons"]))
                self.assertEqual(self.downloads, before)
                self.assertTrue(artifact.parent.exists())
                self.assertEqual(s.mode(self.env), "off")
                self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
                with self.assertRaisesRegex(s.Unavailable, "integrity"):
                    with s.view(self.cwd, self.env, self.root / "logs", self.extract):
                        self.fail("required sharing started with a corrupt snapshot")
                self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "auto"

    def test_local_clone_preserves_fork_cache_identity_and_disables_push(self):
        url = "https://github.com/TauCetiProject/mathlib4.git"
        git(self.mathlib, "remote", "add", "origin", url)
        target = self.root / "fresh-mathlib"
        s.clone(self.mathlib, target, self.rev)
        self.assertEqual(git(target, "remote", "get-url", "origin"), git(self.mathlib, "remote", "get-url", "origin"))
        self.assertEqual(git(target, "remote", "get-url", "--push", "origin"), "no_push")
        self.assertEqual(git(target, "rev-parse", "HEAD"), self.rev)

    def test_clone_preserves_tracking_remote_and_named_branch(self):
        git(self.mathlib, "remote", "add", "origin", "https://github.com/user/mathlib4.git")
        git(self.mathlib, "remote", "add", "upstream", "https://github.com/TauCetiProject/mathlib4.git")
        branch = git(self.mathlib, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.mathlib, "config", f"branch.{branch}.remote", "upstream")
        target = self.root / "fresh"
        s.clone(self.mathlib, target, self.rev)
        self.assertEqual(s.cache_identity(s.cache_context(target)), s.cache_identity(s.cache_context(self.mathlib)))
        self.assertEqual(git(target, "remote", "get-url", "--push", "upstream"), "no_push")

    def test_clone_does_not_apply_global_url_rewrites_twice(self):
        self.gitconfig.write_text(
            '[url "https://mirror.invalid/"]\n insteadOf = https://github.com/\n[url "https://second.invalid/"]\n insteadOf = https://mirror.invalid/\n'
        )
        url = "https://github.com/TauCetiProject/mathlib4.git"
        git(self.mathlib, "remote", "add", "origin", url)
        target = self.root / "fresh"
        s.clone(self.mathlib, target, self.rev)
        self.assertEqual(git(target, "config", "--get", "remote.origin.url"), url)
        self.assertEqual(git(target, "remote", "get-url", "origin"), git(self.mathlib, "remote", "get-url", "origin"))

    def test_cache_identity_changes_key_without_persisting_git_credentials(self):
        key, _ = s.signature(self.cwd, self.env)
        git(self.mathlib, "remote", "add", "origin", "https://secret-fixture@github.com/owner/mathlib4.git")
        changed, spec = s.signature(self.cwd, self.env)
        self.assertNotEqual(key, changed)
        self.assertNotIn("secret-fixture", json.dumps(spec))
        git(self.mathlib, "remote", "set-url", "origin", "https://github.com/other/mathlib4.git")
        self.assertNotEqual(changed, s.signature(self.cwd, self.env)[0])

    def test_downstream_pr_branch_and_fork_do_not_split_mathlib_key(self):
        key, _ = s.signature(self.cwd, self.env)
        git(self.cwd, "checkout", "-qb", "different-pr")
        git(self.cwd, "remote", "add", "fork", "https://github.com/fixture/project.git")
        git(self.cwd, "config", "branch.different-pr.remote", "fork")
        self.assertEqual(key, s.signature(self.cwd, self.env)[0])

    def test_clone_preserves_detached_nightly_tag_context(self):
        git(self.mathlib, "checkout", "--detach", "--quiet", self.rev)
        git(self.mathlib, "tag", "nightly-testing-fixture")
        target = self.root / "fresh"
        s.clone(self.mathlib, target, self.rev)
        self.assertEqual(s.cache_identity(s.cache_context(target)), s.cache_identity(s.cache_context(self.mathlib)))
        self.assertEqual(git(target, "remote"), "")

    def test_publish_from_fresh_sources_and_reuse_without_agent_outputs(self):
        poison = self.mathlib / ".lake/build/poison"
        poison.parent.mkdir(parents=True)
        poison.write_text("never publish this")
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)
        with s.key_lock(self.pool, key):
            entry, hit = s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)
            self.assertFalse(hit)
            self.assertFalse((entry / "build/poison").exists())
            self.assertGreater(json.loads((entry / "ready.json").read_text())["files_verified"], 0)
            self.assertTrue(s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)[1])
        self.assertEqual(self.downloads, 1)
        self.assertTrue(poison.exists())

    def test_failed_or_partial_extraction_never_publishes(self):
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)
        for fetch in (lambda *a, **kw: 1, lambda *a, **kw: 0):
            with self.assertRaises(s.Unavailable):
                s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", fetch)
            self.assertFalse((self.pool / "entries" / key).exists())
        self.assertEqual(list((self.pool / "staging").iterdir()), [])

    def test_gc_never_removes_leased_snapshot(self):
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)
        with s.key_lock(self.pool, key):
            entry, _ = s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        with s.key_lock(self.pool, key, exclusive=False):
            self.assertEqual(s.collect(self.pool), [])
            self.assertTrue(entry.exists())
        self.assertEqual(s.collect(self.pool), [key])

    def test_concurrent_builders_publish_once(self):
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)

        def build():
            with s.key_lock(self.pool, key):
                return s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)[1]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(lambda _: build(), range(2))), [False, True])
        self.assertEqual(self.downloads, 1)

    def test_simultaneous_first_views_enter_before_either_reader_exits(self):
        self.require_overlay()
        second = self.root / "second-checkout"
        shutil.copytree(self.cwd, second)
        rendezvous = threading.Barrier(2)
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        local = threading.local()
        real_ready = s.ready_entry

        def ready(entry, key, spec):
            if not getattr(local, "checked", False):
                local.checked = True
                self.assertFalse(entry.exists())
                rendezvous.wait(timeout=5)
                return False
            return real_ready(entry, key, spec)

        def reader(n):
            env = {**self.env, "TAUCETI_WORKER_ID": f"reader{n}"}
            with s.view((self.cwd, second)[n], env, self.root / f"logs{n}", self.extract) as prefix:
                self.assertTrue(prefix)
                entered[n].set()
                self.assertTrue(release.wait(timeout=10))

        with patch.object(s, "ready_entry", side_effect=ready), concurrent.futures.ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(reader, n) for n in range(2)]
            try:
                self.assertTrue(entered[0].wait(timeout=5))
                self.assertTrue(entered[1].wait(timeout=5), "second first-user waited for a live reader to exit")
            finally:
                release.set()
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.downloads, 1)

    def test_insufficient_disk_fails_private_or_required_without_publication(self):
        self.require_overlay()
        with patch.object(s.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 95, 5)):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
            self.assertIn("10%", " ".join(s.summary(self.env)["reasons"]))
            self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
            with self.assertRaisesRegex(s.Unavailable, "10%"):
                with s.view(self.cwd, self.env, self.root / "logs", self.extract):
                    self.fail("insufficient disk started inference")
        self.assertEqual(self.downloads, 0)
        self.assertEqual(list((self.pool / "entries").iterdir()), [])

    def test_symlinked_extraction_directory_never_publishes(self):
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)

        def extract(*args, **kwargs):
            self.extract(*args, **kwargs)
            build = args[1] / ".lake/packages/mathlib/.lake/build"
            (build / "external").symlink_to(self.root, target_is_directory=True)
            return 0

        with self.assertRaisesRegex(s.Unavailable, "unsupported snapshot file"):
            s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", extract)
        self.assertFalse((self.pool / "entries" / key).exists())

    def test_interrupted_gc_hides_entry_before_removing_any_files(self):
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)
        with s.key_lock(self.pool, key):
            entry, _ = s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        with patch.object(s, "remove_tree", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            s.collect(self.pool)
        self.assertFalse(entry.exists())
        self.assertEqual(len(list((self.pool / "trash").iterdir())), 1)
        with s.key_lock(self.pool, key):
            entry, hit = s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)
        self.assertFalse(hit)
        self.assertTrue(s.ready_entry(entry, key, spec))
        self.assertEqual(list((self.pool / "trash").iterdir()), [])

    def test_inventory_ignores_remounted_device_number_and_refuses_hardlinks(self):
        build = self.root / "build"
        build.mkdir()
        artifact = build / "artifact"
        artifact.write_text("artifact")
        before = s.inventory(build)
        real_stat = Path.stat

        def remounted(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            return SimpleNamespace(
                **{name: getattr(info, name) for name in dir(info) if name.startswith("st_") and name != "st_dev"},
                st_dev=info.st_dev + 99,
            )

        with patch.object(Path, "stat", remounted), patch.object(Path, "lstat", remounted):
            self.assertEqual(s.inventory(build), before)
        os.link(artifact, self.root / "writable-alias")
        with self.assertRaisesRegex(s.Unavailable, "hardlink alias"):
            s.inventory(build)

    def test_private_namespace_cannot_write_pool_or_policy(self):
        self.require_overlay()
        self.pool.mkdir()
        artifact = self.pool / "artifact"
        artifact.write_text("immutable")
        atomic_json(self.pool / "policy.json", dict(mode="off"))
        code = "from pathlib import Path; import sys\nfor name in ('artifact','policy.json','new-file'):\n try: (Path(sys.argv[1])/name).write_text('poison')\n except OSError: pass\n else: raise AssertionError('writable pool')"
        result = containment.run(
            [sys.executable, "-c", code, str(self.pool)], env=self.env, label="private-fixture", capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(artifact.read_text(), "immutable")

    def test_publisher_namespace_only_writes_its_fresh_staging_project(self):
        self.require_overlay()
        from tauceti_worker import agents

        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)

        def extract(command, cwd, env, output, timeout, *, prefix):
            self.extract(command, cwd, env, output, timeout)
            code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); (p/'staging-write').write_text('ok')\ntry: Path(sys.argv[2]).write_text('poison')\nexcept OSError: pass\nelse: raise AssertionError('writable policy')"
            return agents._cache_fetch(
                [sys.executable, "-c", code, str(cwd), str(self.pool / "policy.json")],
                cwd,
                env,
                output,
                timeout,
                prefix=prefix,
            )

        with s.key_lock(self.pool, key):
            entry, _ = s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", extract)
        self.assertTrue(s.ready_entry(entry, key, spec))
        self.assertFalse((self.pool / "policy.json").exists())

    def test_missing_corrupt_policy_and_independent_safety_latch(self):
        self.env.pop("TAUCETI_SHARED_MATHLIB_BUILDS")
        self.assertEqual(s.mode(self.env), "off")
        self.pool.mkdir()
        (self.pool / "policy.json").write_text("{")
        self.assertEqual(s.mode(self.env), "off")
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"]))
        self.assertEqual(s.mode(self.env), "auto")
        s.stop_sharing(self.pool, "fixture integrity failure")
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"]))
        self.assertEqual(s.mode(self.env), "off")
        self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
        with self.assertRaisesRegex(s.Unavailable, "safety stop"):
            s.mode(self.env)
        self.env["TAUCETI_WORKER_ID"] = "not-allowlisted"
        self.assertEqual(s.mode(self.env), "off")

    def test_reused_upper_whiteout_recovers_without_global_safety_stop(self):
        self.require_overlay()
        artifact = self.mathlib / ".lake/build/lib/lean/Mathlib.olean"
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            subprocess.run(prefix + ["rm", str(artifact)], check=True, pass_fds=s.lease_fds(self.env))
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            result = subprocess.run(
                prefix + ["cat", str(artifact)], capture_output=True, pass_fds=s.lease_fds(self.env)
            )
            self.assertEqual(result.stdout, b"verified artifact")
        self.assertFalse((self.pool / "safety-stop.json").exists())
        self.assertEqual(self.downloads, 1)

    def test_mount_setup_failure_does_not_latch_integrity_stop(self):
        self.require_overlay()
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if 'test -f "$1/lib/lean/Mathlib.olean" || exit 42' in command:
                return subprocess.CompletedProcess(command, 1, b"", b"fixture mount failure")
            return real_run(command, *args, **kwargs)

        with patch.object(s.subprocess, "run", side_effect=run):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
        self.assertFalse((self.pool / "safety-stop.json").exists())

    def test_verified_lower_missing_in_fresh_mount_latches_stop(self):
        self.require_overlay()
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if 'test -f "$1/lib/lean/Mathlib.olean" || exit 42' in command:
                return subprocess.CompletedProcess(command, 42, b"", b"")
            return real_run(command, *args, **kwargs)

        with patch.object(s.subprocess, "run", side_effect=run):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
        self.assertEqual(s.mode(self.env), "off")

    def test_lease_failure_latches_stop_without_deleting_lower(self):
        self.require_overlay()
        with s.view(self.cwd, self.env, self.root / "logs", self.extract):
            pass
        real_lock = s.lease_lock
        failed = False

        def lock(stream, flags):
            nonlocal failed
            if not failed and Path(stream.name).stem != "policy":
                failed = True
                raise s.IntegrityFailure("fixture lease failure")
            real_lock(stream, flags)

        with patch.object(s, "lease_lock", side_effect=lock):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
        self.assertEqual(s.mode(self.env), "off")
        self.assertEqual(len(list((self.pool / "entries").iterdir())), 1)

    def test_cleanup_failure_preserves_invocation_result_and_records_failure(self):
        self.require_overlay()
        build = self.mathlib / ".lake/build"
        build.mkdir(parents=True)
        (build / "old").write_text("private")
        real_remove = s.remove_tree

        def remove(path):
            if path.name == "build-before-sharing":
                raise OSError("fixture cleanup failure")
            real_remove(path)

        with patch.object(s, "remove_tree", side_effect=remove):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertTrue(prefix)
                s.record_result(self.env, 0)
        receipts = [json.loads(line) for line in (self.pool / "results.jsonl").read_text().splitlines()]
        self.assertEqual(receipts[-2]["exit_code"], 0)
        self.assertEqual(receipts[-1]["cleanup_error"], "fixture cleanup failure")
        self.assertFalse(receipts[-1]["cleanup_success"])

    def test_auto_fallback_and_required_fail_before_body(self):
        with patch.object(s, "capability", return_value=(False, "enable user namespaces")):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
            self.assertIn("enable user namespaces", s.summary(self.env)["reasons"])
            self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
            with (
                self.assertRaisesRegex(s.Unavailable, "enable user namespaces"),
                s.view(self.cwd, self.env, self.root / "logs", self.extract),
            ):
                self.fail("paid work started")
        self.assertEqual(self.downloads, 0)

    def test_body_failure_is_not_caught_or_retried(self):
        with patch.object(s, "capability", return_value=(False, "unsupported")):
            with (
                self.assertRaisesRegex(ValueError, "body"),
                s.view(self.cwd, self.env, self.root / "logs", self.extract),
            ):
                raise ValueError("body")

    def test_canary_allowlist_and_private_rollback(self):
        self.env["TAUCETI_SHARED_MATHLIB_WORKERS"] = "one,two"
        self.assertEqual(s.mode(self.env), "off")
        self.env["TAUCETI_WORKER_ID"] = "one"
        self.assertEqual(s.mode(self.env), "auto")
        self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "off"
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertEqual(prefix, [])
        self.assertEqual(self.downloads, 0)

    def require_overlay(self):
        ok, reason = s.capability(self.root)
        if not ok:
            self.skipTest(reason)

    def test_real_overlay_copyup_empty_upper_and_shared_read_leases(self):
        self.require_overlay()
        old = self.mathlib / ".lake/build"
        old.mkdir(parents=True)
        (old / "duplicate").write_text("old private tree")
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            self.assertFalse((old / "duplicate").exists())
            file = old / "lib/lean/Mathlib.olean"
            p = subprocess.run(
                prefix + ["sh", "-c", 'cat "$1"; printf copyup > "$1"', "sh", str(file)],
                capture_output=True,
                pass_fds=s.lease_fds(self.env),
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, b"verified artifact")
            key = s.signature(self.cwd, self.env)[0]
            self.assertEqual(
                (self.pool / "entries" / key / "build/lib/lean/Mathlib.olean").read_bytes(), b"verified artifact"
            )
            # A second reader can enter immediately while the first round owns
            # its shared lease. No exclusive key lock on the hit path.
            with s.key_lock(self.pool, key, exclusive=False, nonblocking=True):
                pass
        self.assertEqual(file.read_bytes(), b"copyup")
        self.assertFalse(old.with_name("build-before-sharing").exists())

    def test_two_workers_write_private_outputs_and_cannot_poison_lower(self):
        self.require_overlay()
        second = self.root / "second-checkout"
        shutil.copytree(self.cwd, second)
        env = {**self.env, "TAUCETI_WORKER_ID": "second"}
        with (
            s.view(self.cwd, self.env, self.root / "logs1", self.extract) as one,
            s.view(second, env, self.root / "logs2", self.extract) as two,
        ):
            key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
            self.assertEqual(key, env["TAUCETI_SHARED_MATHLIB_KEY"])
            lower = self.pool / "entries" / key / "build/lib/lean/Mathlib.olean"
            for n, (cwd, prefix, worker_env) in enumerate(((self.cwd, one, self.env), (second, two, env))):
                artifact = cwd / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"
                code = (
                    "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); lower=Path(sys.argv[2]); "
                    "assert p.read_bytes()==b'verified artifact'; "
                    "print(lower.stat().st_dev,lower.stat().st_ino); p.write_text(sys.argv[3]); "
                    "assert p.read_text()==sys.argv[3]; "
                    "\\nfor action in (lambda: lower.write_text('poison'), lambda: lower.chmod(0o777)):\\n"
                    " try: action()\\n except OSError: pass\\n else: raise AssertionError('writable lower')"
                ).replace("\\n", "\n")
                result = subprocess.run(
                    prefix + [sys.executable, "-c", code, str(artifact), str(lower), f"private-{n}"],
                    capture_output=True,
                    text=True,
                    pass_fds=s.lease_fds(worker_env),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"{lower.stat().st_dev} {lower.stat().st_ino}")
            self.assertEqual(lower.read_bytes(), b"verified artifact")
        self.assertEqual((self.mathlib / ".lake/build/lib/lean/Mathlib.olean").read_text(), "private-0")
        self.assertEqual(
            (second / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean").read_text(), "private-1"
        )
        self.assertEqual(self.downloads, 1)

    def test_sandbox_retains_lease_after_parent_descriptor_closes(self):
        self.require_overlay()
        entry = self.pool / "entries/leased"
        lower = entry / "build"
        lower.mkdir(parents=True)
        (lower / "artifact").write_text("shared")
        upper, work = self.root / "upper", self.root / "work"
        upper.mkdir()
        work.mkdir()
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        child = None
        try:
            with s.key_lock(self.pool, "leased", exclusive=False) as lease:
                prefix = s.overlay_command(lower, upper, work, upper, self.pool, lease_fd=lease.fileno())
                child = subprocess.Popen(
                    prefix + ["sh", "-c", "echo ready; read line"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                    pass_fds=(lease.fileno(),),
                )
                self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(s.collect(self.pool), [])
        finally:
            if child:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
        # bwrap's namespace monitor can close its retained descriptor just
        # after the outer process exits. GC skips it until that cleanup finishes.
        deadline = time.monotonic() + 5
        while not (removed := s.collect(self.pool)) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(removed, ["leased"])

    def test_toolchain_split_artifacts_must_all_be_present(self):
        (self.library / "Init.olean.server").write_bytes(b"toolchain format")
        self.pool.mkdir()
        key, spec = s.signature(self.cwd, self.env)
        self.assertIn(".olean.server", spec["artifacts"])
        with self.assertRaisesRegex(s.Unavailable, "olean.server"):
            s.publish(self.pool, key, spec, self.cwd, self.env, self.root / "logs", self.extract)
        self.assertFalse((self.pool / "entries" / key).exists())

    def test_bounded_canary_expires_without_signalling_active_rounds(self):
        self.env.pop("TAUCETI_SHARED_MATHLIB_BUILDS")
        self.pool.mkdir()
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"], expires_at=time.time() + 60))
        self.assertEqual(s.mode(self.env), "auto")
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"], expires_at=time.time() - 1))
        self.assertEqual(s.mode(self.env), "off")

    def test_real_overlay_remounts_after_key_change(self):
        self.require_overlay()
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            first = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
        (self.cwd / "lean-toolchain").write_text("changed toolchain key")
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            self.assertNotEqual(first, self.env["TAUCETI_SHARED_MATHLIB_KEY"])
            result = subprocess.run(
                prefix + ["cat", str(self.mathlib / ".lake/build/lib/lean/Mathlib.olean")],
                capture_output=True,
                pass_fds=s.lease_fds(self.env),
            )
            self.assertEqual(result.stdout, b"verified artifact")
        self.assertEqual(self.downloads, 2)

    def test_off_moves_sparse_delta_aside_before_private_warming(self):
        self.require_overlay()
        upper = self.mathlib / ".lake/build"
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            subprocess.run(
                prefix + ["sh", "-c", 'echo private > "$1/lib/lean/Mathlib.olean"', "sh", str(upper)],
                check=True,
                pass_fds=s.lease_fds(self.env),
            )
        self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "off"
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertEqual(prefix, [])
            self.assertEqual(list(upper.iterdir()), [])
            self.assertTrue((upper.with_name("build-shared-delta") / "lib/lean/Mathlib.olean").exists())

    def test_auto_fallback_releases_snapshot_lock_before_private_round(self):
        self.require_overlay()
        key, _ = s.signature(self.cwd, self.env)
        with patch.object(s, "publish", side_effect=s.Unavailable("download interrupted")):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
                with s.key_lock(self.pool, key, nonblocking=True):
                    pass
                self.assertNotIn("TAUCETI_SHARED_MATHLIB_LEASE_FD", self.env)

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_killed_cache_parent_terminates_detached_downloader_group(self):
        # A contained downloader's own pid is namespace-local; identify it by a unique argv token.
        for policy in ("legacy", "auto"):
            with self.subTest(policy=policy):
                if policy == "auto" and not containment.probe()[0]:
                    continue  # auto falls back to the legacy sweep, which a setsid escapes by design
                token = f"tauceti-downloader-{os.getpid()}-{policy}"
                ready = self.root / f"downloader-{policy}.ready"
                # Only a namespace can catch a downloader that escapes its session.
                childcode = 'import os,time,sys; sys.argv[3] == "auto" and os.setsid(); open(sys.argv[1],"w").write("up"); time.sleep(100)'
                parentcode = 'from pathlib import Path; import os,sys; from tauceti_worker.agents import _cache_fetch; f=open(sys.argv[2],"wb"); _cache_fetch([sys.executable,"-c",sys.argv[3],sys.argv[1],sys.argv[4],sys.argv[5]],Path.cwd(),dict(os.environ),f,100)'
                parent = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        parentcode,
                        str(ready),
                        str(self.root / "cache.log"),
                        childcode,
                        token,
                        policy,
                    ],
                    env={**os.environ, "TAUCETI_PROCESS_CONTAINMENT": policy},
                )

                def tagged(token=token):
                    found = []
                    for entry in Path("/proc").iterdir():
                        if entry.name.isdigit():
                            try:
                                args = (entry / "cmdline").read_bytes().split(b"\0")
                                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                            except OSError:
                                continue
                            if token.encode() in args and state != "Z":
                                found.append(int(entry.name))
                    return found

                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    self.assertTrue(tagged())
                    parent.kill()
                    parent.wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while tagged() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(tagged(), "detached cache downloader survived its killed parent")
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.wait()
                    for pid in tagged():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, 9)

    def test_gc_reclaims_killed_builder_staging_and_skips_live_builder(self):
        key = "a" * 64
        old = self.pool / "staging" / (key + "-orphan")
        old.mkdir(parents=True)
        (old / "partial").write_text("incomplete")
        at = time.time() - 5 * 3600
        os.utime(old, (at, at))
        with s.key_lock(self.pool, key):
            s.collect(self.pool)
            self.assertTrue(old.exists())
        s.collect(self.pool)
        self.assertFalse(old.exists())

    def test_parent_bound_cancellation_wrapper_preserves_sandbox_lease(self):
        self.require_overlay()
        from tauceti_worker import agents

        logs = self.root / "agent-logs"
        logs.mkdir()
        with s.view(self.cwd, self.env, logs, self.extract) as prefix:
            code = 'import json; print(json.dumps(dict(type="system",subtype="init"))); print(json.dumps(dict(type="result",subtype="success",total_cost_usd=0)))'
            rc = agents.run_agent_proc(
                prefix + [sys.executable, "-c", code],
                env=self.env,
                logdir=logs,
                label="offline",
                provider="claude",
                cwd=self.cwd,
                cancel_event=threading.Event(),
            )
            self.assertEqual(rc, 0)

    def test_pid_namespace_proc_matches_process_and_toolchain_lookup(self):
        self.require_overlay()
        lower, upper, work = (self.root / name for name in ("lower", "upper", "work"))
        for path in (lower, upper, work):
            path.mkdir()
        code = 'import os,pathlib; assert int(pathlib.Path("/proc/self/stat").read_text().split()[0])==os.getpid(); assert pathlib.Path("/proc",str(os.getpid()),"exe").exists()'
        result = subprocess.run(
            s.overlay_command(lower, upper, work, upper, lower) + [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_telemetry_does_not_claim_readiness_before_warmers_pass(self):
        self.require_overlay()
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            self.assertEqual(s.summary(self.env)["shared"], 0)
            self.assertEqual(s.summary(self.env)["preparing"], 1)
            s.preparation_status(self.env, error="Lake installation unavailable")
            self.assertEqual(s.summary(self.env)["blocked"], 1)
            self.assertIn("Lake installation unavailable", s.summary(self.env)["reasons"])
            s.preparation_status(self.env)
            self.assertEqual(s.summary(self.env)["shared"], 1)
        self.assertEqual(s.summary(self.env)["shared"], 0)
        self.assertEqual(s.summary(self.env)["idle"], 1)
        receipts = [json.loads(line) for line in (self.pool / "results.jsonl").read_text().splitlines()]
        self.assertEqual(receipts[-1]["phase"], "cleanup")
        self.assertEqual(receipts[-1]["key"], s.signature(self.cwd, self.env)[0])

    def test_prepared_summary_does_not_count_a_dead_reader_as_current(self):
        s.report(self.pool, self.env, mode="shared", key="a" * 64)
        with s.key_lock(self.pool, "a" * 64, exclusive=False):
            self.assertEqual(s.summary(self.env)["shared"], 1)
        self.assertEqual(s.summary(self.env)["shared"], 0)
        self.assertEqual(s.summary(self.env)["idle"], 1)

    def test_dead_supervisor_does_not_count_as_live_when_another_worker_leases_key(self):
        key = "b" * 64
        s.report(self.pool, self.env, mode="shared", key=key)
        path = next((self.pool / "status").glob("*.json"))
        row = json.loads(path.read_text())
        row["supervisor"]["start"] = "dead-process"
        atomic_json(path, row)
        with s.key_lock(self.pool, key, exclusive=False):
            self.assertEqual(s.summary(self.env)["shared"], 0)
            self.assertEqual(s.summary(self.env)["idle"], 1)
        s.report(self.pool, self.env, mode="preparing")
        row = json.loads(path.read_text())
        row["supervisor"]["start"] = "dead-process"
        atomic_json(path, row)
        self.assertEqual(s.summary(self.env)["preparing"], 0)

    def test_summary_rejects_invalid_lock_key(self):
        s.report(self.pool, self.env, mode="shared", key="../unexpected")
        self.assertEqual(s.summary(self.env)["blocked"], 1)
        self.assertFalse((self.pool / "unexpected.lock").exists())

    def test_two_real_readers_share_physical_mmap_pages(self):
        self.require_overlay()
        lower = self.root / "lower"
        lower.mkdir()
        (lower / "mapped-artifact").write_bytes(b"x" * (4 * 1024**2))
        readers = []
        try:
            for n in range(2):
                upper, work = self.root / f"upper{n}", self.root / f"work{n}"
                upper.mkdir()
                work.mkdir()
                code = "import mmap,sys,os; f=open(sys.argv[1],'rb'); m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ); sum(m[::4096]); print(os.getpid(),flush=True); sys.stdin.readline()"
                p = subprocess.Popen(
                    s.overlay_command(lower, upper, work, upper, lower)
                    + [sys.executable, "-c", code, str(upper / "mapped-artifact")],
                    stdout=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                )
                self.assertTrue(p.stdout.readline().strip())
                pending, pid = [p.pid], None
                while pending:
                    candidate = pending.pop()
                    try:
                        if "mapped-artifact" in Path(f"/proc/{candidate}/smaps").read_text():
                            pid = candidate
                            break
                        pending.extend(
                            int(v) for v in Path(f"/proc/{candidate}/task/{candidate}/children").read_text().split()
                        )
                    except FileNotFoundError:
                        pass
                if pid is None:
                    p.stdin.close()
                    p.wait(timeout=5)
                    p.stdout.close()
                    self.fail("cannot find mapped reader in the host PID namespace")
                readers.append((p, pid))
            for _p, pid in readers:
                blocks = Path(f"/proc/{pid}/smaps").read_text().splitlines()
                values = {}
                active = False
                for line in blocks:
                    if "-" in line.split()[0]:
                        active = line.endswith("mapped-artifact")
                    elif active and line.startswith(("Pss:", "Shared_Clean:", "Shared_Dirty:")):
                        values[line.split(":")[0]] = int(line.split()[1])
                self.assertGreaterEqual(values["Shared_Clean"] + values["Shared_Dirty"], 4096)
                self.assertLessEqual(values["Pss"], 2100)
        finally:
            for p, _ in readers:
                p.stdin.close()
                p.wait(timeout=5)
                p.stdout.close()


if __name__ == "__main__":
    unittest.main()

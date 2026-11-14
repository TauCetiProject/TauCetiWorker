"""Shared Mathlib: one read-only source tree per revision, one build per key, per-round views."""

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
from tauceti_worker import shared_mathlib as s
from tauceti_worker.runtime_status import atomic_json, read_json


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, message="fixture"):
    for name, value in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def repository(path, files):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return commit(path, files)


def package(name, repo, rev, *, inherited=True):
    return dict(name=name, type="git", url=str(repo), rev=rev, inputRev="main", inherited=inherited, scope="")


def writable_bits(tree):
    """Paths under ``tree`` (not following links) that carry any write bit."""
    found = []
    for here, directories, files in os.walk(tree):
        for name in [*directories, *files]:
            path = Path(here) / name
            if not path.is_symlink() and path.lstat().st_mode & 0o222:
                found.append(path)
    if Path(tree).lstat().st_mode & 0o222:
        found.append(Path(tree))
    return found


class Fixture(unittest.TestCase):
    """A checkout pinning a local Mathlib whose manifest requires a local Batteries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text("")
        git_env = patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1")
        git_env.start()
        self.addCleanup(git_env.stop)
        upstream = self.root / "upstream"
        self.batteries_repo = upstream / "batteries"
        self.batteries_rev = repository(
            self.batteries_repo, {"Batteries.lean": "def b := 1\n", ".gitignore": ".lake/\n"}
        )
        self.mathlib_repo = upstream / "mathlib"
        self.rev = repository(
            self.mathlib_repo,
            {
                "Mathlib.lean": "import Mathlib.Test\n",
                "Mathlib/Test.lean": "def test := 1\n",
                "lake-manifest.json": json.dumps(
                    dict(version=7, packages=[package("batteries", self.batteries_repo, self.batteries_rev)])
                ),
                ".gitignore": ".lake/\n",
            },
        )
        self.cwd = self.root / "checkout"
        repository(
            self.cwd,
            {
                "lean-toolchain": "leanprover/lean4:v4.29.0",
                "lakefile.toml": "name = 'fixture'\n",
                ".gitignore": ".lake/\n",
            },
        )
        self.pin(self.cwd, self.rev)
        self.pool = self.root / "pool"
        self.P = s.layout(self.pool)
        # The source tree is read-only by design; restore write bits before the temporary cleanup.
        self.addCleanup(lambda: s.remove_tree(self.pool))
        self.env = {**os.environ, "TAUCETI_SHARED_MATHLIB_ROOT": str(self.pool), "TAUCETI_WORKER_ID": "fixture"}
        self.env.pop("TAUCETI_SHARED_MATHLIB_BUILDS", None)
        self.env.pop("TAUCETI_SHARED_MATHLIB_WORKERS", None)
        self.library = self.root / "toolchain/lib/lean"
        self.library.mkdir(parents=True)
        (self.library / "Init.olean").write_text("fixture ABI")
        self.abi = "Lean fixture ABI"
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if command == ["lake", "env", "lean", "--version"]:
                return subprocess.CompletedProcess(command, 0, self.abi + "\n", "")
            if command == ["lake", "env", "lean", "--print-prefix"]:
                return subprocess.CompletedProcess(command, 0, str(self.library.parents[1]) + "\n", "")
            return real_run(command, *args, **kwargs)

        patcher = patch.object(s, "run_process", side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.downloads = 0
        self.artifact = b"verified artifact"
        disk = patch.object(
            s.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100_000_000, 20_000_000, 80_000_000)
        )
        disk.start()
        self.addCleanup(disk.stop)

    def pin(self, checkout, rev, *, batteries=None, url=None, extra=()):
        packages = [
            package("mathlib", url or self.mathlib_repo, rev, inherited=False),
            package("batteries", self.batteries_repo, batteries or self.batteries_rev),
            *extra,
        ]
        return commit(
            checkout,
            {"lake-manifest.json": json.dumps(dict(version=7, packagesDir=".lake/packages", packages=packages))},
            "pin",
        )

    @property
    def source(self):
        return s.source_dir(self.P, self.rev)

    @property
    def build(self):
        """Mathlib's build directory as Lake sees it, through the checkout's link."""
        return self.source / "mathlib/.lake/build"

    def extract(self, command, cwd, env, output, timeout):
        self.downloads += 1
        self.assertEqual(command[-4:], ["lake", "exe", "cache", "get"])
        self.assertIn("--ro-bind", command)
        self.assertNotIn("TAUCETI_SHARED_MATHLIB_LEASE_FD", env)
        self.assertNotIn("--unshare-pid", command)
        self.assertEqual(cwd.name, "root")  # a staging workspace, never a checkout
        self.assertFalse((cwd / ".git").exists())
        rev = next(
            p["rev"] for p in json.loads((cwd / "lake-manifest.json").read_text())["packages"] if p["name"] == "mathlib"
        )
        source = s.source_dir(self.P, rev)
        binds = {
            command[i + 2]: command[i + 1]
            for i, a in enumerate(command)
            if a == "--bind" and command[i + 1] != command[i + 2]
        }
        for name in ("mathlib", "batteries"):
            self.assertEqual(os.readlink(cwd / ".lake/packages" / name), str(source / name))
            self.assertEqual(binds[str(source / name / ".lake/build")], str(cwd.parent / "build" / name))
        build = cwd.parent / "build"
        for module in ("Mathlib", "Mathlib/Test"):
            for suffix in (".olean", ".ilean"):
                target = build / "mathlib/lib/lean" / (module + suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.artifact)
        (build / "batteries/lib/lean").mkdir(parents=True)
        (build / "batteries/lib/lean/Batteries.olean").write_bytes(b"batteries artifact")
        return 0

    def require_overlay(self):
        ok, reason = s.capability(self.root)
        if not ok:
            self.skipTest(reason)

    def graph(self, checkout=None):
        return s.resolve(checkout or self.cwd, self.env)

    def attach(self, checkout=None):
        """Publish the source tree and attach the checkout without a namespace."""
        checkout = checkout or self.cwd
        graph = self.graph(checkout)
        self.P.mkdir(parents=True, exist_ok=True)
        closure = s.ensure_source(self.P, self.pool, checkout, graph, None, self.env)
        s.attach(checkout, self.P, graph["rev"], closure)
        return graph, closure

    def published(self, checkout=None):
        graph, closure = self.attach(checkout)
        key, spec = s.signature(checkout or self.cwd, self.env, closure)
        with s.key_lock(self.P, key):
            entry, hit = s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", self.extract)
        return key, spec, entry, hit, graph, closure

    def run_in(self, prefix, *argv, env=None, **kwargs):
        return subprocess.run(prefix + list(argv), capture_output=True, pass_fds=s.lease_fds(env or self.env), **kwargs)

    def assert_blocked(self, pattern, checkout=None):
        """The round stops before its body: status blocked, leases released, no private Mathlib."""
        with self.assertRaisesRegex(s.Unavailable, pattern):
            with s.view(checkout or self.cwd, self.env, self.root / "logs", self.extract):
                self.fail("round started without shared Mathlib")
        summary = s.summary(self.env)
        self.assertEqual(summary["blocked"], 1)
        self.assertRegex(" ".join(summary["reasons_by_mode"]["blocked"]), pattern)
        self.assertNotIn("private", summary["reasons_by_mode"])
        for name in ("LEASE_FD", "KEY", "PREP_STARTED"):
            self.assertNotIn("TAUCETI_SHARED_MATHLIB_" + name, self.env)
        mathlib = (checkout or self.cwd) / ".lake/packages/mathlib"
        self.assertFalse(mathlib.exists() and not mathlib.is_symlink(), "a private Mathlib appeared")


class AttachTests(Fixture):
    def test_a_fresh_checkout_attaches_before_any_lake_run_and_never_reports_absent(self):
        self.require_overlay()
        self.assertFalse((self.cwd / ".lake").exists())
        lake_runs = []
        real = s.run_process.side_effect

        def run(command, *args, **kwargs):
            lake_runs.append(((self.cwd / ".lake/packages/mathlib").is_symlink(), command))
            return real(command, *args, **kwargs)

        with patch.object(s, "run_process", side_effect=run):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertTrue(prefix)
                result = self.run_in(
                    prefix, "cat", str(self.cwd / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean")
                )
                self.assertEqual(result.stdout, self.artifact)
                batteries = self.cwd / ".lake/packages/batteries/.lake/build/lib/lean/Batteries.olean"
                self.assertEqual(self.run_in(prefix, "cat", str(batteries)).stdout, b"batteries artifact")
        self.assertTrue(lake_runs)
        self.assertTrue(all(linked for linked, _ in lake_runs), lake_runs)  # Lake never saw a missing Mathlib
        for name in ("mathlib", "batteries"):
            self.assertEqual(os.readlink(self.cwd / ".lake/packages" / name), str(self.source / name))
        rows = [read_json(p) for p in (self.pool / "status").glob("*.json")]
        self.assertNotIn("absent", json.dumps(rows))
        self.assertEqual(s.summary(self.env)["private"], 0)

    def test_source_tree_is_read_only_detached_at_the_pin_with_the_manifest_url(self):
        self.attach()
        meta = read_json(self.source / "source.json")
        self.assertEqual(set(meta["packages"]), {"mathlib", "batteries"})
        self.assertEqual(writable_bits(self.source), [])
        for name, repo, rev in (
            ("mathlib", self.mathlib_repo, self.rev),
            ("batteries", self.batteries_repo, self.batteries_rev),
        ):
            tree = self.source / name
            self.assertEqual(git(tree, "rev-parse", "HEAD"), rev)
            self.assertEqual(git(tree, "remote", "get-url", "origin"), str(repo))
            self.assertNotEqual(subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"]).returncode, 0)
            self.assertTrue((tree / ".lake/build").is_dir())
            self.assertEqual(list((tree / ".lake/build").iterdir()), [])
            # Lake's own check on a matching pin: the index is fresh, so git needs no write.
            index = (tree / ".git/index").stat()
            self.assertEqual(subprocess.run(["git", "-C", str(tree), "diff", "--quiet", "HEAD"]).returncode, 0)
            self.assertEqual((tree / ".git/index").stat().st_mtime_ns, index.st_mtime_ns)
            self.assertFalse((tree / ".git/index.lock").exists())

    def test_outside_a_view_the_shared_build_directory_refuses_writes(self):
        self.attach()
        build = self.cwd / ".lake/packages/mathlib/.lake/build"
        with self.assertRaises(PermissionError):
            (build / "Mathlib.olean").write_bytes(b"private copy")
        with self.assertRaises(PermissionError):
            (self.cwd / ".lake/packages/mathlib/Mathlib/Test.lean").write_text("edited")

    def test_a_private_package_is_retired_and_seeds_the_source_without_the_network(self):
        # An older round left a private Mathlib checkout with a 700 MB build. Its objects seed the
        # shared source even though the manifest's URL is unreachable; the directory itself goes.
        url = "https://invalid.example/mathlib4.git"
        self.pin(self.cwd, self.rev, url=url)
        private = self.cwd / ".lake/packages/mathlib"
        subprocess.run(["git", "clone", "-q", str(self.mathlib_repo), str(private)], check=True)
        (private / ".lake/build").mkdir(parents=True)
        (private / ".lake/build/old.olean").write_text("private build")
        self.attach()
        self.assertTrue(private.is_symlink())
        self.assertEqual(git(self.source / "mathlib", "remote", "get-url", "origin"), url)
        retired = list((self.cwd / ".lake" / s.META).glob("retired-*"))
        self.assertEqual(len(retired), 1)
        s.discard_private_state(self.cwd)
        self.assertFalse(retired[0].exists())

    def test_a_link_to_another_revision_is_replaced_and_stale_links_are_dropped(self):
        self.attach()
        packages = self.cwd / ".lake/packages"
        (packages / "aesop").symlink_to(self.source / "batteries")  # a closure package of an older version
        new_rev = commit(self.mathlib_repo, {"Mathlib/Test.lean": "def test := 2\n"})
        self.pin(self.cwd, new_rev)
        self.attach()
        self.assertEqual(os.readlink(packages / "mathlib"), str(s.source_dir(self.P, new_rev) / "mathlib"))
        self.assertFalse((packages / "aesop").is_symlink())
        self.assertTrue(s.layout_ok(self.cwd, self.P, new_rev, ("mathlib", "batteries")))
        self.assertFalse(s.layout_ok(self.cwd, self.P, self.rev, ("mathlib", "batteries")))

    def test_only_the_exact_layout_is_accepted(self):
        self.attach()
        names = ("mathlib", "batteries")
        self.assertTrue(s.layout_ok(self.cwd, self.P, self.rev, names))
        link = self.cwd / ".lake/packages/mathlib"
        link.unlink()
        link.symlink_to(self.root / "elsewhere")
        self.assertFalse(s.layout_ok(self.cwd, self.P, self.rev, names))
        link.unlink()
        link.symlink_to(self.source / "mathlib")
        meta = self.cwd / ".lake" / s.META
        meta.rename(self.root / "moved-meta")
        meta.symlink_to(self.root / "moved-meta")
        self.assertFalse(s.layout_ok(self.cwd, self.P, self.rev, names))
        with self.assertRaisesRegex(s.Unavailable, "symlink"):
            s.attach(self.cwd, self.P, self.rev, read_json(self.source / "source.json")["packages"])

    def test_a_different_dependency_pin_or_url_fails_instead_of_falling_back(self):
        self.attach()
        other_batteries = commit(self.batteries_repo, {"Batteries.lean": "def b := 2\n"})
        self.pin(self.cwd, self.rev, batteries=other_batteries)
        with self.assertRaisesRegex(s.Unavailable, "requires"):
            self.attach()
        self.pin(self.cwd, self.rev, url="https://github.com/someone/mathlib4.git")
        with self.assertRaisesRegex(s.Unavailable, "URL"):
            self.attach()
        # A first publication on a non-canonical graph never leaves a source tree behind.
        new_rev = commit(self.mathlib_repo, {"Mathlib/Test.lean": "def test := 3\n"})
        self.pin(self.cwd, new_rev, batteries=other_batteries)
        with self.assertRaisesRegex(s.Unavailable, "requires"):
            self.attach()
        self.assertFalse(s.source_dir(self.P, new_rev).exists())
        self.assertEqual(list((self.P / "staging").iterdir()), [])

    def test_a_manifest_that_names_no_mathlib_package_blocks(self):
        commit(
            self.cwd,
            {"lake-manifest.json": json.dumps(dict(packages=[package("mathlib-fork", self.mathlib_repo, self.rev)]))},
        )
        self.assert_blocked("names no Mathlib package")
        commit(
            self.cwd, {"lake-manifest.json": json.dumps(dict(packages=[dict(name="mathlib", type="path", dir="../m")]))}
        )
        with self.assertRaisesRegex(s.Unavailable, "pinned to a full commit"):
            s.resolve(self.cwd, self.env)

    def test_url_map_selects_the_origin_lake_expects(self):
        mirror = self.root / "mirror"
        subprocess.run(["git", "clone", "-q", "--mirror", str(self.mathlib_repo), str(mirror)], check=True)
        self.env["LAKE_PKG_URL_MAP"] = json.dumps({"mathlib": str(mirror)})
        self.attach()
        self.assertEqual(git(self.source / "mathlib", "remote", "get-url", "origin"), str(mirror))
        self.env["LAKE_PKG_URL_MAP"] = "{"
        with self.assertRaisesRegex(s.Unavailable, "LAKE_PKG_URL_MAP"):
            s.expected_url("mathlib", "x", self.env)


class SignatureTests(Fixture):
    def key(self):
        _, closure = self.attach()
        return s.signature(self.cwd, self.env, closure)

    def test_key_names_mathlib_and_its_closure_but_not_tauceti_configuration(self):
        key, spec = self.key()
        self.assertEqual(spec["mathlib"], self.rev)
        self.assertEqual(spec["closure"], {"batteries": self.batteries_rev, "mathlib": self.rev})
        self.assertEqual(spec["version"], 4)
        # TauCeti's own configuration, root-only dependencies, branches and forks do not split it.
        commit(self.cwd, {"lakefile.toml": "name = 'fixture'\n[[lean_lib]]\nname = 'TauCeti'\n"})
        self.pin(self.cwd, self.rev, extra=[package("doc-gen4", self.batteries_repo, self.batteries_rev)])
        git(self.cwd, "checkout", "-qb", "different-pr")
        git(self.cwd, "remote", "add", "fork", "https://github.com/fixture/project.git")
        (self.cwd / "lean-toolchain").write_text("leanprover/lean4:v4.29.0\n")
        self.assertEqual(key, self.key()[0])

    def test_cache_transport_settings_do_not_split_the_key_or_leak(self):
        key, _ = self.key()
        config = self.root / "lake.toml"
        config.write_text("[cache]\nservice = 'different'\n")
        self.env.update(
            LAKE_CONFIG=str(config),
            MATHLIB_CACHE_GET_URL="https://secret-fixture@cache.invalid",
            LAKE_ARTIFACT_CACHE="1",
            LAKE_CACHE_DIR=str(self.root / "lake-cache"),
        )
        changed, spec = self.key()
        self.assertEqual(key, changed)
        self.assertNotIn("secret-fixture", json.dumps(spec))

    def test_toolchain_abi_platform_options_and_pins_split_the_key(self):
        key, _ = self.key()
        self.abi = "Lean other ABI"
        self.assertNotEqual(key, self.key()[0])
        self.abi = "Lean fixture ABI"
        (self.library / "Init.olean").write_text("different toolchain, same version output")
        self.assertNotEqual(key, self.key()[0])
        (self.library / "Init.olean").write_text("fixture ABI")
        self.assertEqual(key, self.key()[0])
        self.env["LEAN_CC"] = "clang-17"
        self.assertNotEqual(key, self.key()[0])
        del self.env["LEAN_CC"]
        with patch.object(s.platform, "machine", return_value="riscv64"):
            self.assertNotEqual(key, self.key()[0])
        new_rev = commit(self.mathlib_repo, {"Mathlib/Test.lean": "def test := 2\n"})
        self.pin(self.cwd, new_rev)
        self.assertNotEqual(key, self.key()[0])

    def test_lean_failure_is_unavailable(self):
        _, closure = self.attach()
        with patch.object(s, "run_process", return_value=subprocess.CompletedProcess([], 1, "", "no elan")):
            with self.assertRaisesRegex(s.Unavailable, "no elan"):
                s.signature(self.cwd, self.env, closure)


class PublishTests(Fixture):
    def test_publish_the_whole_closure_from_the_canonical_source(self):
        poison = self.root / "agent-output"
        poison.write_text("never publish this")
        key, spec, entry, hit, graph, closure = self.published()
        self.assertFalse(hit)
        self.assertEqual((entry / "build/mathlib/lib/lean/Mathlib.olean").read_bytes(), self.artifact)
        self.assertEqual((entry / "build/batteries/lib/lean/Batteries.olean").read_bytes(), b"batteries artifact")
        ready = read_json(entry / "ready.json")
        self.assertGreater(ready["files_verified"], 0)
        self.assertEqual(ready["spec"], spec)
        with s.key_lock(self.P, key):
            self.assertTrue(s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", self.extract)[1])
        self.assertEqual(self.downloads, 1)
        self.assertEqual(list((self.P / "staging").iterdir()), [])
        self.assertEqual(writable_bits(self.source), [])  # extraction never wrote into the source

    def test_failed_or_partial_extraction_never_publishes(self):
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)
        for fetch in (lambda *a, **kw: 1, lambda *a, **kw: 0):
            with self.assertRaises(s.Unavailable):
                s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", fetch)
            self.assertFalse((self.P / "entries" / key).exists())
        self.assertEqual([p for p in (self.P / "staging").iterdir() if not p.name.startswith("source-")], [])

    def test_symlinked_extraction_directory_never_publishes(self):
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)

        def extract(command, cwd, *args):
            self.extract(command, cwd, *args)
            (cwd.parent / "build/mathlib/external").symlink_to(self.root, target_is_directory=True)
            return 0

        with self.assertRaisesRegex(s.Unavailable, "unsupported snapshot file"):
            s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", extract)
        self.assertFalse((self.P / "entries" / key).exists())

    def test_toolchain_split_artifacts_must_all_be_present(self):
        (self.library / "Init.olean.server").write_bytes(b"toolchain format")
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)
        self.assertIn(".olean.server", spec["artifacts"])
        with self.assertRaisesRegex(s.Unavailable, "olean.server"):
            s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", self.extract)
        self.assertFalse((self.P / "entries" / key).exists())

    def test_concurrent_builders_publish_once(self):
        graph, closure = self.attach()
        key, spec = s.signature(self.cwd, self.env, closure)

        def build():
            with s.key_lock(self.P, key):
                return s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", self.extract)[1]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(lambda _: build(), range(2))), [False, True])
        self.assertEqual(self.downloads, 1)

    def test_gc_never_removes_a_leased_snapshot(self):
        key, _spec, entry, *_ = self.published()
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        with s.key_lock(self.P, key, exclusive=False):
            self.assertEqual(s.collect(self.pool), [])
            self.assertTrue(entry.exists())
        self.assertEqual(s.collect(self.pool), [str(entry)])

    def test_unbuilt_sources_age_out_but_never_under_a_reader(self):
        self.attach()
        old = time.time() - 8 * 86400
        os.chmod(self.source, 0o755)
        os.utime(self.source, (old, old))
        os.chmod(self.source, 0o555)
        with s.key_lock(self.P, "source-" + self.rev, exclusive=False):
            self.assertEqual(s.collect(self.pool), [])
        self.assertEqual(s.collect(self.pool), [str(self.source)])
        self.assertFalse(self.source.exists())

    def test_interrupted_gc_hides_entry_before_removing_any_files(self):
        key, spec, entry, _hit, graph, closure = self.published()
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        with patch.object(s, "remove_tree", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            s.collect(self.pool)
        self.assertFalse(entry.exists())
        self.assertEqual(len(list((self.P / "trash").iterdir())), 1)
        with s.key_lock(self.P, key):
            entry, hit = s.publish(self.P, key, spec, graph, closure, self.env, self.root / "logs", self.extract)
        self.assertFalse(hit)
        self.assertTrue(s.ready_entry(entry, key, spec))
        self.assertEqual(list((self.P / "trash").iterdir()), [])

    def test_gc_reclaims_killed_builder_staging_and_skips_live_builder(self):
        key = "a" * 64
        stages = [
            (self.P / "staging" / f"{key}-{self.rev}-orphan", key),
            (self.P / "staging" / f"source-{self.rev}-orphan", f"source-{self.rev}-publisher"),
        ]
        at = time.time() - 5 * 3600
        for stage, _ in stages:
            stage.mkdir(parents=True)
            (stage / "partial").write_text("incomplete")
            os.utime(stage, (at, at))
        with contextlib.ExitStack() as locks:
            for _, lock in stages:
                locks.enter_context(s.key_lock(self.P, lock))
            s.collect(self.pool)
            for stage, _ in stages:
                self.assertTrue(stage.exists())
        s.collect(self.pool)
        for stage, _ in stages:
            self.assertFalse(stage.exists())

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

    def test_large_disk_can_publish_with_abundant_bytes_below_ten_percent(self):
        gib = 1024**3
        with patch.object(
            s.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(3500 * gib, 3326 * gib, 174 * gib)
        ):
            *_, entry, hit, _graph, _closure = self.published()
        self.assertFalse(hit)
        self.assertTrue((entry / "ready.json").is_file())


class LegacyTests(Fixture):
    def legacy(self, key, revision="0" * 40):
        entry = self.pool / "entries" / key
        (entry / "build/lib/lean").mkdir(parents=True)
        (entry / "ready.json").write_text(json.dumps({"key": key, "spec": {"version": 3, "mathlib": revision}}))
        return entry

    def test_old_layout_entries_are_deleted_not_migrated(self):
        unleased, leased = self.legacy("1" * 64), self.legacy("2" * 64)
        (self.pool / "staging" / ("3" * 64 + "-old")).mkdir(parents=True)
        with s.key_lock(self.pool, "2" * 64, exclusive=False):
            removed = s.collect(self.pool)
            self.assertIn(str(unleased), removed)
            self.assertTrue(leased.exists())  # an older worker's round still reads it
        self.assertFalse((self.pool / "staging").exists())
        s.collect(self.pool)
        self.assertFalse(leased.exists())
        self.assertFalse((self.pool / "entries").exists())

    def test_a_leased_old_layout_entry_counts_against_the_cap(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("holder discovery uses /proc")
        legacy = self.legacy("4" * 64)
        holder = VersionCapTests.hold(self, self.pool, "4" * 64)  # an older worker's round reads it
        other = commit(self.mathlib_repo, {"Mathlib/Test.lean": "def test := 9\n"})
        second = self.root / "second"
        subprocess.run(["git", "clone", "-q", str(self.cwd), str(second)], check=True)
        self.pin(second, other)
        self.attach(second)
        self.assertTrue(legacy.exists())  # one current version and one leased legacy build fit
        with patch.object(s, "EVICTION_GRACE", 5):
            self.attach()  # a second v4 version plus the leased legacy build would be three
        self.assertFalse(legacy.exists())
        self.assertIsNotNone(holder.wait(timeout=10))
        self.assertTrue(s.source_dir(self.P, other).exists())


class PolicyTests(Fixture):
    def test_sharing_is_required_by_default_and_off_blocks(self):
        self.assertEqual(s.mode(self.env), "required")  # no policy.json is not an opt-out any more
        for value in ("auto", "required", "AUTO"):
            self.assertEqual(s.mode({**self.env, "TAUCETI_SHARED_MATHLIB_BUILDS": value}), "required")
        with self.assertRaisesRegex(s.Disabled, "switched off.*rounds that need Mathlib do not run"):
            s.mode({**self.env, "TAUCETI_SHARED_MATHLIB_BUILDS": "off"})
        self.assertIsInstance(s.Disabled("x"), s.NoFallback)
        with self.assertRaisesRegex(s.Unavailable, "must be required or off"):
            s.mode({**self.env, "TAUCETI_SHARED_MATHLIB_BUILDS": "private"})

    def test_off_blocks_rounds_that_need_mathlib_and_not_others(self):
        self.env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "off"
        with patch.object(s, "capability", side_effect=AssertionError("probed while off")):
            self.assert_blocked("switched off")
        self.assertFalse((self.cwd / ".lake").exists())
        other = self.root / "other"
        other.mkdir()
        (other / "lake-manifest.json").write_text(json.dumps(dict(packages=[dict(name="batteries", url="x")])))
        with s.view(other, self.env, self.root / "logs", self.extract) as prefix:
            self.assertEqual(prefix, [])
        self.assertEqual(self.downloads, 0)

    def test_missing_corrupt_policy_and_independent_safety_latch(self):
        self.pool.mkdir()
        (self.pool / "policy.json").write_text("{")
        for build in (None, "auto", "required", "off"):
            env = {**self.env, **({"TAUCETI_SHARED_MATHLIB_BUILDS": build} if build else {})}
            with self.assertRaisesRegex(s.Unavailable, "invalid shared Mathlib policy"):
                s.mode(env)
        atomic_json(self.pool / "policy.json", dict(mode="auto", expires_at=1))
        self.assertEqual(s.mode(self.env), "required")  # an expired policy keeps the default
        atomic_json(self.pool / "policy.json", dict(mode="off", workers=["fixture"]))
        with self.assertRaises(s.Disabled):
            s.mode(self.env)
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"]))
        with self.assertRaisesRegex(s.Unavailable, "not allowlisted"):
            s.mode({**self.env, "TAUCETI_WORKER_ID": "not-allowlisted"})
        s.stop_sharing(self.pool, "fixture integrity failure")
        self.assertEqual(read_json(self.pool / "policy.json")["mode"], "auto")
        with self.assertRaisesRegex(s.Unavailable, "safety stop"):
            s.mode(self.env)
        (self.pool / "safety-stop.json").unlink()
        with self.assertRaisesRegex(s.Unavailable, "safety stop"):
            s.mode(self.env)  # the policy's own record still latches it
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"]))
        atomic_json(self.pool / "policy.json", dict(mode="auto", workers=["fixture"], expires_at=1, expiry_mode="off"))
        with self.assertRaisesRegex(s.Disabled, "expired"):
            s.mode(self.env)

    def test_safety_stop_attempts_both_receipts_independently_and_never_raises(self):
        self.pool.mkdir()
        real_atomic = s.atomic_json
        for failed_names in ({"safety-stop.json"}, {"policy.json"}, {"safety-stop.json", "policy.json"}):
            for name in ("safety-stop.json", "policy.json"):
                (self.pool / name).unlink(missing_ok=True)

            def atomic(path, value, failed=failed_names):
                if path.name in failed:
                    raise OSError("fixture full disk")
                real_atomic(path, value)

            with patch.object(s, "atomic_json", side_effect=atomic):
                self.assertEqual(s.stop_sharing(self.pool, "fixture stop"), len(failed_names) < 2)
            self.assertEqual((self.pool / "safety-stop.json").exists(), "safety-stop.json" not in failed_names)
            self.assertEqual((self.pool / "policy.json").exists(), "policy.json" not in failed_names)

    def test_project_without_mathlib_dependency_has_nothing_to_share(self):
        (self.cwd / "lake-manifest.json").write_text(json.dumps(dict(packages=[dict(name="batteries")])))
        with patch.object(s, "capability", side_effect=AssertionError("probed sharing without Mathlib")):
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                self.assertEqual(prefix, [])
        self.assertEqual(s.summary(self.env)["blocked"], 0)
        self.assertEqual(self.downloads, 0)
        rows = [read_json(p) for p in (self.pool / "status").glob("*.json")]
        self.assertEqual([r["mode"] for r in rows], ["not-applicable"])

    def test_doubtful_manifests_count_as_mathlib(self):
        for manifest in (
            dict(name="mathlib", packages=[dict(name="batteries")]),  # Mathlib is the root package
            dict(packages=[dict(name="mathlib-fork")]),
            dict(packages=[dict(name="ml", url="https://github.com/someone/mathlib4")]),
            dict(packages=[None, {}]),
            dict(packages=[{}]),
            dict(packages="nope"),
        ):
            (self.cwd / "lake-manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(s._names_mathlib(self.cwd), manifest)
        (self.cwd / "lake-manifest.json").write_text(json.dumps(dict(packages=[dict(name="batteries", url="x")])))
        self.assertFalse(s._names_mathlib(self.cwd))

    def test_capability_failure_blocks_before_body(self):
        with patch.object(s, "capability", return_value=(False, "enable user namespaces")):
            self.assert_blocked("enable user namespaces")
        self.assertEqual(self.downloads, 0)


class ViewTests(Fixture):
    def setUp(self):
        super().setUp()
        self.require_overlay()

    def test_real_overlay_copyup_lands_in_the_round_and_leaves_shared_state_alone(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
            file = self.build / "lib/lean/Mathlib.olean"
            p = self.run_in(prefix, "sh", "-c", 'cat "$1"; printf copyup > "$1"', "sh", str(file))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, self.artifact)
            self.assertEqual(self.run_in(prefix, "cat", str(file)).stdout, b"copyup")
            lower = self.P / "entries" / key / "build/mathlib/lib/lean/Mathlib.olean"
            self.assertEqual(lower.read_bytes(), self.artifact)
            # The source is read-only inside the view as well, and a second reader can enter at once.
            edit = self.run_in(prefix, "sh", "-c", 'printf x > "$1"', "sh", str(self.source / "mathlib/Mathlib.lean"))
            self.assertNotEqual(edit.returncode, 0)
            with s.key_lock(self.P, key, exclusive=False, nonblocking=True):
                pass
            generation = self.cwd / ".lake" / s.META / s.view_marker(self.cwd)["generation"]
            self.assertEqual((generation / "mathlib/upper/lib/lean/Mathlib.olean").read_bytes(), b"copyup")
        self.assertEqual(list(self.build.iterdir()), [])  # outside the view: the empty mount point
        self.assertEqual(writable_bits(self.source), [])

    def test_two_build_keys_against_one_source_revision_in_concurrent_namespaces(self):
        second = self.root / "second"
        subprocess.run(["git", "clone", "-q", str(self.cwd), str(second)], check=True)
        other = {**self.env, "TAUCETI_WORKER_ID": "second", "LEAN_CC": "clang-17"}
        self.artifact = b"key one"
        with s.view(self.cwd, self.env, self.root / "logs1", self.extract) as one:
            self.artifact = b"key two"
            with s.view(second, other, self.root / "logs2", self.extract) as two:
                keys = self.env["TAUCETI_SHARED_MATHLIB_KEY"], other["TAUCETI_SHARED_MATHLIB_KEY"]
                self.assertNotEqual(*keys)
                target = self.build / "lib/lean/Mathlib.olean"
                # A long-lived reader in the first namespace keeps seeing its own build while the
                # second namespace mounts a different build at the very same path.
                reader = subprocess.Popen(
                    one + ["sh", "-c", 'read line; cat "$1"', "sh", str(target)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    pass_fds=s.lease_fds(self.env),
                )
                try:
                    self.assertEqual(self.run_in(two, "cat", str(target), env=other).stdout, b"key two")
                    self.assertEqual(self.run_in(one, "cat", str(target)).stdout, b"key one")
                finally:
                    output, _ = reader.communicate(b"\n", timeout=10)
                self.assertEqual(output, b"key one")
                self.assertEqual(len({os.readlink(c / ".lake/packages/mathlib") for c in (self.cwd, second)}), 1)
        self.assertEqual(self.downloads, 2)
        self.assertEqual(len(list((self.P / "sources").iterdir())), 1)
        self.assertEqual(len(s._versions(self.P)), 1)  # one version, two builds

    def test_two_workers_write_private_outputs_and_cannot_poison_lower(self):
        second = self.root / "second"
        subprocess.run(["git", "clone", "-q", str(self.cwd), str(second)], check=True)
        env = {**self.env, "TAUCETI_WORKER_ID": "second"}
        with (
            s.view(self.cwd, self.env, self.root / "logs1", self.extract) as one,
            s.view(second, env, self.root / "logs2", self.extract) as two,
        ):
            key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
            self.assertEqual(key, env["TAUCETI_SHARED_MATHLIB_KEY"])
            lower = self.P / "entries" / key / "build/mathlib/lib/lean/Mathlib.olean"
            for n, (prefix, worker_env) in enumerate(((one, self.env), (two, env))):
                code = (
                    "import sys; from pathlib import Path; p=Path(sys.argv[1]); lower=Path(sys.argv[2]); "
                    "assert p.read_bytes()==b'verified artifact'; p.write_text(sys.argv[3]); "
                    "assert p.read_text()==sys.argv[3]; "
                    "\\nfor action in (lambda: lower.write_text('poison'), lambda: lower.chmod(0o777)):\\n"
                    " try: action()\\n except OSError: pass\\n else: raise AssertionError('writable lower')"
                ).replace("\\n", "\n")
                result = self.run_in(
                    prefix,
                    sys.executable,
                    "-c",
                    code,
                    str(self.build / "lib/lean/Mathlib.olean"),
                    str(lower),
                    f"private-{n}",
                    env=worker_env,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.run_in(one, "cat", str(self.build / "lib/lean/Mathlib.olean")).stdout, b"private-0")
            self.assertEqual(
                self.run_in(two, "cat", str(self.build / "lib/lean/Mathlib.olean"), env=env).stdout, b"private-1"
            )
            self.assertEqual(lower.read_bytes(), b"verified artifact")
        self.assertEqual(self.downloads, 1)

    def test_each_round_starts_from_an_empty_upper(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.run_in(
                prefix,
                "sh",
                "-c",
                'rm "$1/lib/lean/Mathlib/Test.ilean"; echo private > "$1/lib/lean/Mathlib.olean"',
                "sh",
                str(self.build),
            )
            first = s.view_marker(self.cwd)["generation"]
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            marker = s.view_marker(self.cwd)
            self.assertNotEqual(marker["generation"], first)
            self.assertFalse((self.cwd / ".lake" / s.META / first).exists())
            self.assertEqual(s.upper_usage(self.cwd / ".lake" / s.META / marker["generation"])["upper_files"], 0)
            for name in ("Mathlib.olean", "Mathlib/Test.ilean"):
                self.assertEqual(self.run_in(prefix, "cat", str(self.build / "lib/lean" / name)).stdout, self.artifact)
        self.assertEqual(self.downloads, 1)
        self.assertFalse((self.pool / "safety-stop.json").exists())

    def test_a_live_namespace_keeps_its_generation(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            first = s.view_marker(self.cwd)["generation"]
            survivor = subprocess.Popen(
                prefix + ["sh", "-c", "echo ready; read line"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                pass_fds=s.lease_fds(self.env),
            )
            self.assertEqual(survivor.stdout.readline().strip(), "ready")
        try:
            with s.view(self.cwd, self.env, self.root / "logs", self.extract):
                self.assertTrue((self.cwd / ".lake" / s.META / first).exists())
        finally:
            survivor.stdin.close()
            survivor.wait(timeout=5)
            survivor.stdout.close()
        deadline = time.monotonic() + 5
        while (self.cwd / ".lake" / s.META / first).exists() and time.monotonic() < deadline:
            s.discard_private_state(self.cwd)
            time.sleep(0.05)
        self.assertFalse((self.cwd / ".lake" / s.META / first).exists())

    def test_simultaneous_first_views_enter_before_either_reader_exits(self):
        second = self.root / "second"
        subprocess.run(["git", "clone", "-q", str(self.cwd), str(second)], check=True)
        rendezvous = threading.Barrier(2)
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        local = threading.local()
        real_ready = s.ready_entry

        def ready(entry, key, spec):
            if not getattr(local, "checked", False):
                local.checked = True
                rendezvous.wait(timeout=10)
                return False
            return real_ready(entry, key, spec)

        def reader(n):
            env = {**self.env, "TAUCETI_WORKER_ID": f"reader{n}"}
            with s.view((self.cwd, second)[n], env, self.root / f"logs{n}", self.extract) as prefix:
                self.assertTrue(prefix)
                entered[n].set()
                self.assertTrue(release.wait(timeout=20))

        with patch.object(s, "ready_entry", side_effect=ready), concurrent.futures.ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(reader, n) for n in range(2)]
            try:
                self.assertTrue(entered[0].wait(timeout=20))
                self.assertTrue(entered[1].wait(timeout=20), "second first-user waited for a live reader to exit")
            finally:
                release.set()
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(self.downloads, 1)

    def test_damaged_ready_snapshot_is_rejected_and_preserved(self):
        for damage in ("remove", "overwrite", "symlink"):
            with self.subTest(damage=damage):
                (self.pool / "policy.json").unlink(missing_ok=True)
                (self.pool / "safety-stop.json").unlink(missing_ok=True)
                self.abi = "Lean fixture " + damage  # a fresh key per damage
                with s.view(self.cwd, self.env, self.root / "logs", self.extract):
                    key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
                artifact = self.P / "entries" / key / "build/mathlib/lib/lean/Mathlib/Test.ilean"
                if damage == "overwrite":
                    artifact.write_bytes(b"corrupt artifact!")
                else:
                    artifact.unlink()
                    if damage == "symlink":
                        artifact.symlink_to(self.library / "Init.olean")
                before = self.downloads
                self.assert_blocked("integrity")
                self.assertEqual(self.downloads, before)
                self.assertTrue(artifact.parent.exists())
                with self.assertRaisesRegex(s.Unavailable, "safety stop"):
                    s.mode(self.env)

    def test_insufficient_disk_blocks_without_publication(self):
        with patch.object(s.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 95, 5)):
            self.assert_blocked("10%")
        self.assertEqual(self.downloads, 0)
        self.assertFalse((self.P / "entries").exists())

    def test_busy_exclusive_lease_blocks_after_bounded_wait(self):
        _, closure = self.attach()
        key, _ = s.signature(self.cwd, self.env, closure)
        with s.key_lock(self.P, key), patch.object(s, "LOCK_WAIT_SECONDS", 0.05):
            self.assert_blocked("lock wait timed out")
        with s.key_lock(self.P, key, nonblocking=True):
            pass
        self.assertEqual(self.downloads, 0)

    def test_mount_setup_failure_does_not_latch_integrity_stop(self):
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if 'test -f "$1/lib/lean/Mathlib.olean" || exit 42' in command:
                return subprocess.CompletedProcess(command, 1, b"", b"fixture mount failure")
            return real_run(command, *args, **kwargs)

        with patch.object(s.subprocess, "run", side_effect=run):
            self.assert_blocked("fixture mount failure")
        self.assertFalse((self.pool / "safety-stop.json").exists())
        self.assertEqual(s.mode(self.env), "required")

    def test_verified_lower_missing_in_fresh_mount_latches_stop(self):
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if 'test -f "$1/lib/lean/Mathlib.olean" || exit 42' in command:
                return subprocess.CompletedProcess(command, 42, b"", b"")
            return real_run(command, *args, **kwargs)

        with patch.object(s.subprocess, "run", side_effect=run):
            self.assert_blocked("verified lower missing")
        with self.assertRaisesRegex(s.Unavailable, "safety stop"):
            s.mode(self.env)

    def test_lease_failure_latches_stop_without_deleting_lower(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract):
            pass
        real_lock = s.lease_lock
        failed = False

        def lock(stream, flags, *, timeout=None):
            nonlocal failed
            if (
                not failed
                and Path(stream.name).stem not in ("policy", "versions")
                and not Path(stream.name).stem.startswith("source-")
            ):
                failed = True
                raise s.IntegrityFailure("fixture lease failure")
            real_lock(stream, flags, timeout=timeout)

        with patch.object(s, "lease_lock", side_effect=lock):
            self.assert_blocked("fixture lease failure")
        with self.assertRaisesRegex(s.Unavailable, "safety stop"):
            s.mode(self.env)
        self.assertEqual(len(list((self.P / "entries").iterdir())), 1)

    def test_failed_publication_blocks_and_releases_snapshot_lock(self):
        with patch.object(s, "publish", side_effect=s.Unavailable("download interrupted")):
            self.assert_blocked("download interrupted")

    def test_body_failure_is_not_caught_or_retried(self):
        runs = 0
        with self.assertRaisesRegex(ValueError, "body"), s.view(self.cwd, self.env, self.root / "logs", self.extract):
            runs += 1
            raise ValueError("body")
        self.assertEqual(runs, 1)
        receipts = [json.loads(line) for line in (self.pool / "results.jsonl").read_text().splitlines()]
        self.assertEqual(receipts[-1]["body_error"], "ValueError")
        self.assertFalse(receipts[-1]["cleanup_success"])

    def test_sandbox_retains_lease_after_parent_descriptor_closes(self):
        key, _spec, entry, *_ = self.published()
        old = time.time() - 8 * 86400
        os.utime(entry, (old, old))
        child = None
        try:
            with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
                child = subprocess.Popen(
                    prefix + ["sh", "-c", "echo ready; read line"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                    pass_fds=s.lease_fds(self.env),
                )
                self.assertEqual(child.stdout.readline().strip(), "ready")
            os.utime(entry, (old, old))
            self.assertEqual(s.collect(self.pool), [])
        finally:
            if child:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
        deadline = time.monotonic() + 5
        while not (removed := s.collect(self.pool)) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(removed, [str(entry)])

    def test_parent_bound_cancellation_wrapper_preserves_sandbox_lease(self):
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

    def test_disposable_overlay_is_volatile_without_leaking_the_mount_helper(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            code = "import os,sys;from pathlib import Path;assert 'overlay-mount' not in os.environ.get('LD_PRELOAD','');line=next(x for x in Path('/proc/self/mountinfo').read_text().splitlines() if x.split()[4]==sys.argv[1]);assert any(x in ('volatile','fsync=volatile') for x in line.split()[-1].split(','))"
            result = self.run_in(prefix, sys.executable, "-c", code, str(self.build))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_telemetry_does_not_claim_readiness_before_warmers_pass(self):
        with s.view(self.cwd, self.env, self.root / "logs", self.extract) as prefix:
            self.assertTrue(prefix)
            key = self.env["TAUCETI_SHARED_MATHLIB_KEY"]
            self.assertEqual(s.summary(self.env)["shared"], 0)
            self.assertEqual(s.summary(self.env)["preparing"], 1)
            s.preparation_status(self.env, error="Lake installation unavailable")
            self.assertEqual(s.summary(self.env)["blocked"], 1)
            s.preparation_status(self.env)
            self.assertEqual(s.summary(self.env)["shared"], 1)
        self.assertEqual(s.summary(self.env)["idle"], 1)
        receipts = [json.loads(line) for line in (self.pool / "results.jsonl").read_text().splitlines()]
        self.assertEqual(receipts[-1]["phase"], "cleanup")
        self.assertEqual(receipts[-1]["key"], key)

    def test_a_third_version_evicts_the_oldest_before_its_source_is_written(self):
        revisions = [self.rev]
        for n in (2, 3):
            revisions.append(commit(self.mathlib_repo, {"Mathlib/Test.lean": f"def test := {n}\n"}))
        checkouts = []
        for n, revision in enumerate(revisions):
            checkout = self.root / f"checkout-{n}"
            subprocess.run(["git", "clone", "-q", str(self.cwd), str(checkout)], check=True)
            self.pin(checkout, revision)
            checkouts.append(checkout)
        seen = []
        real = s._publish_source

        def publish_source(P, cwd, graph, env):
            seen.append(sorted(s._versions(P)))
            return real(P, cwd, graph, env)

        # The clones' origin/main pins the first revision; this exercises the cap, not refusal.
        with (
            patch.object(s, "_publish_source", side_effect=publish_source),
            patch.object(s, "allowed_revisions", return_value=None),
        ):
            for n, checkout in enumerate(checkouts):
                with s.view(checkout, {**self.env, "TAUCETI_WORKER_ID": f"w{n}"}, self.root / "logs", self.extract):
                    pass
                os.utime(
                    s.source_dir(self.P, revisions[n]),
                    (time.time() - 100 + n, time.time() - 100 + n),
                    follow_symlinks=False,
                )
                for entry in s._entries(self.P):
                    if s._entry_revision(entry) == revisions[n]:
                        os.utime(entry, (time.time() - 100 + n,) * 2)
        self.assertEqual(seen[2], sorted(revisions[1:2]))  # evicted before the third source existed
        self.assertEqual(set(s._versions(self.P)), set(revisions[1:]))
        self.assertFalse(s.source_dir(self.P, revisions[0]).exists())

    def test_view_proc_matches_process_and_keeps_the_host_pid_namespace(self):
        lower, upper, work = (self.root / name for name in ("lower", "upper", "work"))
        for path in (lower, upper, work):
            path.mkdir()
        command = s.overlay_command(lower, upper, work, upper, lower)
        self.assertNotIn("--unshare-pid", command)
        code = 'import os,pathlib; assert int(pathlib.Path("/proc/self/stat").read_text().split()[0])==os.getpid(); print(os.readlink("/proc/self/ns/pid"))'
        result = subprocess.run(command + [sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), os.readlink("/proc/self/ns/pid"))

    def test_two_real_readers_share_physical_mmap_pages(self):
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
                values = {}
                active = False
                for line in Path(f"/proc/{pid}/smaps").read_text().splitlines():
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


class SnapshotTests(Fixture):
    def snapshot(self, name, **kwargs):
        from tauceti_worker.verification import inventory
        from tauceti_worker.verification_jobs import snapshot

        target = self.root / "state/verification-candidates" / (name * 32)
        target.parent.mkdir(parents=True, exist_ok=True)
        with patch.dict(
            os.environ,
            {
                **self.env,
                "TAUCETI_VERIFY_STORAGE_DIR": str(self.root / "verify-ledger"),
                "TAUCETI_VERIFY_MIN_FREE_BYTES": "1",
                "TAUCETI_VERIFY_MIN_FREE_PERCENT": "0",
            },
        ):
            snapshot(self.cwd, target, inventory(self.cwd, git(self.cwd, "rev-parse", "HEAD")), **kwargs)
        return target

    def test_snapshot_keeps_shared_links_and_copies_no_mathlib(self):
        self.attach()
        meta = self.cwd / ".lake" / s.META
        (meta / ("f" * 32) / "mathlib/upper").mkdir(parents=True)
        (meta / ("f" * 32) / "mathlib/upper/private.olean").write_text("round output")
        (self.cwd / ".lake/build").mkdir()
        (self.cwd / ".lake/build/TauCeti.olean").write_text("tauceti output")
        target = self.snapshot("a")
        for name in ("mathlib", "batteries"):
            link = target / ".lake/packages" / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(self.source / name))
        self.assertFalse((target / ".lake" / s.META).exists())
        self.assertEqual((target / ".lake/build/TauCeti.olean").read_text(), "tauceti output")

    def test_snapshot_of_an_older_layout_copies_sources_only(self):
        private = self.cwd / ".lake/packages/mathlib"
        subprocess.run(["git", "clone", "-q", str(self.mathlib_repo), str(private)], check=True)
        (private / ".lake/build").mkdir(parents=True)
        (private / ".lake/build/sparse.olean").write_text("incomplete old upper")
        atomic_json(private / ".lake/tauceti-shared-mathlib.json", dict(key="1" * 64, pool=str(self.pool)))
        target = self.snapshot("b")
        copied = target / ".lake/packages/mathlib"
        self.assertTrue((copied / "Mathlib/Test.lean").is_file())
        self.assertFalse((copied / ".lake/build").exists())
        self.assertFalse((copied / ".lake/tauceti-shared-mathlib.json").exists())

    def test_snapshot_storage_pressure_remains_retryable(self):
        from tauceti_worker.verification import inventory
        from tauceti_worker.verification_jobs import snapshot
        from tauceti_worker.verification_storage import Pressure

        self.attach()
        target = self.root / "state/verification-candidates" / ("c" * 32)
        target.parent.mkdir(parents=True)
        with patch.dict(os.environ, {**self.env, "TAUCETI_VERIFY_MIN_FREE_BYTES": str(10**30)}):
            with self.assertRaises(Pressure) as raised:
                snapshot(self.cwd, target, inventory(self.cwd, git(self.cwd, "rev-parse", "HEAD")))
        self.assertEqual(raised.exception.reason, "filesystem-headroom")
        self.assertFalse(target.exists())

    def test_a_continuation_runs_inside_a_shared_view(self):
        from tauceti_worker import verification_jobs as vj
        from tauceti_worker.verification_storage import Pressure

        prefixes = []

        @contextlib.contextmanager
        def view(cwd, env, logdir, fetch):
            prefixes.append(cwd)
            env["TAUCETI_SHARED_MATHLIB_LEASE_FD"] = "7,8"
            yield ["bwrap", "fixture"]

        with contextlib.ExitStack() as stack, patch.object(s, "view", view):
            env = {}
            self.assertEqual(vj._mathlib_view(stack, self.cwd, env, self.root / "state"), ["bwrap", "fixture"])
            self.assertEqual(json.loads(env["TAUCETI_SHARED_MATHLIB_VIEW"]), ["bwrap", "fixture"])
            self.assertEqual(s.lease_fds(env), (7, 8))
        self.assertEqual(prefixes, [self.cwd])

        @contextlib.contextmanager
        def busy(cwd, env, logdir, fetch):
            raise s.Unavailable("lock wait timed out")
            yield

        with contextlib.ExitStack() as stack, patch.object(s, "view", busy), self.assertRaises(Pressure) as raised:
            vj._mathlib_view(stack, self.cwd, {}, self.root / "state")
        self.assertEqual(raised.exception.reason, "cache")


class SummaryTests(Fixture):
    def test_prepared_summary_does_not_count_a_dead_reader_as_current(self):
        s.report(self.pool, self.env, mode="shared", key="a" * 64)
        with s.key_lock(self.P, "a" * 64, exclusive=False):
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
        with s.key_lock(self.P, key, exclusive=False):
            self.assertEqual(s.summary(self.env)["shared"], 0)
            self.assertEqual(s.summary(self.env)["idle"], 1)

    def test_summary_keeps_worker_states_with_reasons(self):
        for worker, mode, message in (
            ("idle", "idle", "shared view released; awaiting next natural round"),
            ("blocked", "blocked", "shared Mathlib is switched off on this host"),
            ("preparing", "overlay-ready", "warming caches"),
        ):
            s.report(self.pool, {**self.env, "TAUCETI_WORKER_ID": worker}, mode=mode, reason=message)
        summary = s.summary(self.env)
        self.assertEqual(summary["private"], 0)
        self.assertEqual(
            summary["reasons_by_mode"],
            {
                "idle": ["shared view released; awaiting next natural round"],
                "blocked": ["shared Mathlib is switched off on this host"],
                "preparing": ["warming caches"],
            },
        )

    def test_summary_rejects_invalid_lock_key(self):
        s.report(self.pool, self.env, mode="shared", key="../unexpected")
        self.assertEqual(s.summary(self.env)["blocked"], 1)
        self.assertFalse((self.pool / "unexpected.lock").exists())

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_killed_cache_parent_terminates_detached_downloader_group(self):
        pidfile = self.root / "downloader.pid"
        childcode = 'import os,time,sys; open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(100)'
        parentcode = 'from pathlib import Path; import os,sys; from tauceti_worker.agents import _cache_fetch; f=open(sys.argv[2],"wb"); _cache_fetch([sys.executable,"-c",sys.argv[3],sys.argv[1]],Path.cwd(),dict(os.environ),f,100)'
        parent = subprocess.Popen(
            [sys.executable, "-c", parentcode, str(pidfile), str(self.root / "cache.log"), childcode],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        pid = None
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("detached cache downloader survived its killed parent")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)


class VersionCapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pool = self.root / "pool"
        self.P = s.layout(self.pool)
        self.addCleanup(lambda: s.remove_tree(self.pool))

    def manifest(self, repo, revision):
        (repo / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": revision}]}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", revision], check=True)

    def entry(self, key, revision, age):
        entry = self.P / "entries" / key
        (entry / "build").mkdir(parents=True)
        (entry / "ready.json").write_text(json.dumps({"key": key, "spec": {"mathlib": revision}}))
        os.utime(entry, (time.time() - age, time.time() - age))
        return entry

    def source(self, revision, age):
        source = s.source_dir(self.P, revision)
        (source / "mathlib").mkdir(parents=True)
        os.chmod(source, 0o555)
        os.utime(source, (time.time() - age, time.time() - age))
        return source

    def hold(self, base, name):
        (base / "locks").mkdir(parents=True, exist_ok=True)
        lock = base / "locks" / (name + ".lock")
        script = "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_SH); print('ok', flush=True); time.sleep(120)"
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        self.addCleanup(lambda: (process.kill(), process.wait(), process.stdout.close()))
        self.assertEqual(process.stdout.readline().strip(), "ok")
        return process

    def test_allowed_revisions_are_current_and_previous_main_pins(self):
        repo = self.root / "checkout"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        for args in (["user.name", "t"], ["user.email", "t@example.com"]):
            subprocess.run(["git", "-C", str(repo), "config", *args], check=True)
        self.assertIsNone(s.allowed_revisions(repo))  # no origin/main: unknown
        for revision in ("aaa", "bbb", "bbb2"):
            self.manifest(repo, revision)
        (repo / "README").write_text("unrelated")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "docs"], check=True)
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True)
        self.assertEqual(s.allowed_revisions(repo), {"bbb2", "bbb"})

    def repo_with_main(self, main_pin):
        repo = self.root / "checkout"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        for args in (["user.name", "t"], ["user.email", "t@example.com"], ["commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", str(repo), "config", *args], check=True)
        (repo / "lean-toolchain").write_text("old-toolchain\n")
        self.manifest(repo, "old" * 13 + "o")
        subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "branch"], check=True)
        subprocess.run(["git", "-C", str(repo), "switch", "-q", "main"], check=True)
        (repo / "lean-toolchain").write_text("main-toolchain\n")
        (repo / "lakefile.toml").write_text('name = "main"\n')
        self.manifest(repo, main_pin)
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "switch", "-q", "branch"], check=True)
        return repo

    def test_rebase_round_prepares_mains_mathlib_and_restores_the_branch(self):
        main_pin = "c" * 40
        repo = self.repo_with_main(main_pin)
        branch_manifest = (repo / "lake-manifest.json").read_bytes()
        self.addCleanup(s.set_round_purpose, repo)
        with s.rebase_target(repo, {}) as restore:
            self.assertEqual((repo / "lake-manifest.json").read_bytes(), branch_manifest)
            restore()
        s.set_round_purpose(repo, rebase=True)
        with s.rebase_target(repo, {}) as restore:
            self.assertEqual(s._manifest_pin((repo / "lake-manifest.json").read_text()), main_pin)
            self.assertEqual((repo / "lean-toolchain").read_text(), "main-toolchain\n")
            self.assertTrue((repo / "lakefile.toml").is_file())
            restore()
            self.assertEqual((repo / "lake-manifest.json").read_bytes(), branch_manifest)
            self.assertEqual((repo / "lean-toolchain").read_text(), "old-toolchain\n")
            self.assertFalse((repo / "lakefile.toml").exists())
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status.stdout, "")
        with self.assertRaises(RuntimeError):
            with s.rebase_target(repo, {}):
                raise RuntimeError("preparation failed")
        self.assertEqual((repo / "lake-manifest.json").read_bytes(), branch_manifest)
        other = self.root / "other"
        other.mkdir()
        (other / "lake-manifest.json").write_bytes(branch_manifest)
        with s.rebase_target(other, {}):
            self.assertEqual((other / "lake-manifest.json").read_bytes(), branch_manifest)
        s.set_round_purpose(repo)
        self.assertEqual(s._purpose(repo), {})

    def test_repair_round_moves_to_mains_mathlib_only_when_its_pin_is_refused(self):
        main_pin = "c" * 40
        repo = self.repo_with_main(main_pin)
        self.addCleanup(s.set_round_purpose, None)
        s.set_round_purpose(repo, repair=True)
        allowed_branch = (repo / "lake-manifest.json").read_bytes()
        with s.rebase_target(repo, {"TAUCETI_SHARED_MATHLIB_ROOT": str(self.pool)}):
            self.assertEqual((repo / "lake-manifest.json").read_bytes(), allowed_branch)
        self.manifest(repo, "f" * 40)
        with s.rebase_target(repo, {"TAUCETI_SHARED_MATHLIB_ROOT": str(self.pool)}) as restore:
            self.assertEqual(s._manifest_pin((repo / "lake-manifest.json").read_text()), main_pin)
            restore()
            self.assertEqual(s._manifest_pin((repo / "lake-manifest.json").read_text()), "f" * 40)

    def test_bump_pins_are_allowed_for_a_week(self):
        self.assertEqual(s.bump_pins(self.pool), set())
        (self.pool / "bump-pins").mkdir(parents=True)
        (self.pool / "bump-pins" / ("d" * 40)).touch()
        old = self.pool / "bump-pins" / ("e" * 40)
        old.touch()
        os.utime(old, (time.time() - s.BUMP_PIN_SECONDS - 1,) * 2)
        self.assertEqual(s.bump_pins(self.pool), {"d" * 40})

    def test_cap_counts_versions_and_evicts_disallowed_ones_first_killing_their_rounds(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("holder discovery uses /proc")
        old_rev, current_rev, new_rev = "1" * 40, "2" * 40, "3" * 40
        old_entry = self.entry("k-old", old_rev, age=100)
        old_source = self.source(old_rev, age=100)
        current = [self.entry("k-current", current_rev, age=500), self.entry("k-current-2", current_rev, age=500)]
        using_old = self.hold(self.P, "source-" + old_rev)
        using_current = self.hold(self.P, "k-current")
        self.assertEqual(set(s._versions(self.P)), {old_rev, current_rev})  # two builds, one version
        with patch.object(s, "EVICTION_GRACE", 5):
            s.enforce_version_cap(self.P, new_rev, {current_rev, new_rev}, {})
        self.assertFalse(old_entry.exists())
        self.assertFalse(old_source.exists())
        self.assertTrue(all(e.exists() for e in current))
        self.assertIsNotNone(using_old.wait(timeout=10))  # its round was killed
        self.assertIsNone(using_current.poll())
        s.enforce_version_cap(self.P, current_rev, {current_rev}, {})  # within the cap
        self.assertTrue(all(e.exists() for e in current))

    def test_round_detection_matches_the_real_invocation_only(self):
        self.assertTrue(s.is_round_argv(["/rt/bin/python", "-m", "tauceti_worker", "_round", "--worker-id", "w"]))
        self.assertTrue(s.is_round_argv(["python3", "-I", "-m", "tauceti_worker", "_round"]))
        self.assertFalse(s.is_round_argv(["python", "-c", "x", "tauceti_worker", "_round"]))
        self.assertFalse(s.is_round_argv(["python", "-m", "tauceti_worker", "work", "--loop"]))

    def test_eviction_kills_the_whole_round_that_holds_a_version(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("holder discovery uses /proc")
        old = self.entry("k-old", "1" * 40, age=100)
        self.entry("k-current", "2" * 40, age=500)
        (self.P / "locks").mkdir(parents=True, exist_ok=True)
        lock = self.P / "locks/k-old.lock"
        child = (
            "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_SH); "
            "print('ok', flush=True); time.sleep(120)"
        )
        parent = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(120)"
        round_ = subprocess.Popen([sys.executable, "-c", parent, child, str(lock)], start_new_session=True)
        self.addCleanup(lambda: (round_.kill(), round_.wait()))
        deadline = time.time() + 10
        while not s._holders(lock) and time.time() < deadline:
            time.sleep(0.1)
        with (
            patch.object(s, "EVICTION_GRACE", 5),
            patch.object(s, "_owning_round", side_effect=lambda pid: round_.pid),
        ):
            s.enforce_version_cap(self.P, "3" * 40, {"2" * 40}, {})
        self.assertFalse(old.exists())
        self.assertIsNotNone(round_.wait(timeout=10))  # the round itself, not only the holder

    def test_unfetchable_main_leaves_pins_unknown(self):
        repo = self.root / "checkout"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        for args in (["user.name", "t"], ["user.email", "t@example.com"]):
            subprocess.run(["git", "-C", str(repo), "config", *args], check=True)
        self.manifest(repo, "aaa")
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(self.root / "missing")], check=True)
        self.assertIsNone(s.allowed_revisions(repo))

    def test_shallow_history_leaves_the_previous_pin_unknown(self):
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-q", "-b", "main"], check=True)
        for args in (["user.name", "t"], ["user.email", "t@example.com"]):
            subprocess.run(["git", "-C", str(source), "config", *args], check=True)
        for revision in ("aaa", "bbb"):
            self.manifest(source, revision)
        shallow = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{source}", str(shallow)], check=True)
        self.assertIsNone(s.allowed_revisions(shallow))

    def test_an_unkillable_holder_stops_the_round_instead_of_a_private_build(self):
        victim = self.entry("k-old", "1" * 40, age=100)
        self.entry("k-current", "2" * 40, age=500)
        holder = self.hold(self.P, "k-old")
        with (
            patch.object(s, "_kill_holders"),  # a holder this process cannot end
            patch.object(s, "EVICTION_WAIT", 0.2),
            self.assertRaises(s.CapContention),
        ):
            s.enforce_version_cap(self.P, "3" * 40, {"2" * 40}, {})
        self.assertFalse(victim.exists())  # hidden at once, so no new reader can lease it
        self.assertIsInstance(s.CapContention("x"), s.NoFallback)
        self.assertIsNone(holder.poll())

    def test_collect_keeps_trash_whose_lock_is_still_leased(self):
        trash = self.P / "trash" / ("source-" + "1" * 40 + "-" + "a" * 32)
        (trash / "mathlib").mkdir(parents=True)
        holder = self.hold(self.P, "source-" + "1" * 40)
        s.collect(self.pool, max_age=10**9)
        self.assertTrue(trash.exists())  # a round that outlived eviction still mounts it
        holder.kill()
        holder.wait()
        s.collect(self.pool, max_age=10**9)
        self.assertFalse(trash.exists())

    def test_cap_evicts_the_oldest_allowed_version_when_all_are_allowed(self):
        older = self.entry("k-a", "1" * 40, age=900)
        newer = self.entry("k-b", "2" * 40, age=10)
        s.enforce_version_cap(self.P, "3" * 40, {"1" * 40, "2" * 40}, {})
        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())

    def test_old_layout_builds_are_evicted_before_any_current_version(self):
        legacy = self.pool / "entries" / ("9" * 64)
        (legacy / "build").mkdir(parents=True)
        current = self.entry("k-current", "2" * 40, age=10**6)
        holder = self.hold(self.pool, "9" * 64)
        with patch.object(s, "EVICTION_GRACE", 5):
            s.enforce_version_cap(self.P, "3" * 40, None, {})
        self.assertFalse(legacy.exists())
        self.assertTrue(current.exists())
        self.assertIsNotNone(holder.wait(timeout=10))


if __name__ == "__main__":
    unittest.main()

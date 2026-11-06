"""Machine Lake configuration and optional reclamation use disposable fixture roots."""

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
from tauceti_worker import machine_lake_cache as cache


class MachineCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "shared"
        self.env = {"TAUCETI_MACHINE_LAKE_CACHE_ROOT": str(self.root)}
        cache.configure(self.home, self.home / "worker", self.env)

    def artifact(self, name="a" * 16 + ".olean", toolchain="b" * 16):
        path = self.root / "toolchains" / toolchain / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 8192)
        return path

    def test_configuration_retains_private_path_for_unfixed_lake(self):
        self.assertEqual(self.env["LAKE_CACHE_DIR"], str(self.home / "worker/.cache/lake"))
        self.assertEqual(self.env["LAKE_SHARED_CACHE_ROOT"], str(self.root))
        self.assertEqual(self.env["LAKE_SHARED_CACHE_FALLBACK"], self.env["LAKE_CACHE_DIR"])

    def test_size_target_also_reclaims_native_metadata(self):
        path = self.artifact()
        metadata = path.parent.parent / "outputs/Example" / ("c" * 16 + ".json")
        metadata.parent.mkdir(parents=True)
        metadata.write_text('{"data": "optional"}')
        result = cache.collect(env=dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0"))
        self.assertFalse(metadata.exists())
        self.assertEqual(result["retained_bytes"], 0)

    def test_invalid_optional_limit_does_not_delete_or_block(self):
        path = self.artifact()
        result = cache.collect(env=dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="nan"))
        self.assertTrue(path.exists())
        self.assertTrue(result["errors"])

    def test_malformed_staging_owner_does_not_break_other_collection(self):
        payload = self.artifact()
        staging = payload.parent / (".lake-cache-" + "9" * 70 + "-1-.tmp")
        staging.write_text("unknown owner")
        cache.collect(env=dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0"))
        self.assertFalse(payload.exists())
        self.assertTrue(staging.exists())

    def test_explicit_cache_override_wins(self):
        env = {"LAKE_CACHE_DIR": "/operator/cache"}
        cache.configure(self.home, self.home / "worker", env)
        self.assertEqual(env, {"LAKE_CACHE_DIR": "/operator/cache"})

    def test_disable_shared_cache_does_not_reject_work(self):
        env = {"TAUCETI_MACHINE_LAKE_CACHE": "0"}
        cache.configure(self.home, self.home / "worker", env)
        self.assertNotIn("LAKE_SHARED_CACHE_ROOT", env)

    def test_opt_out_clears_an_inherited_shared_selection(self):
        self.env["TAUCETI_MACHINE_LAKE_CACHE"] = "0"
        cache.configure(self.home, self.home / "worker", self.env)
        self.assertNotIn("LAKE_SHARED_CACHE_ROOT", self.env)

    def test_no_selected_limits_does_not_evict(self):
        path = self.artifact()
        result = cache.collect(env=self.env)
        self.assertTrue(path.exists())
        self.assertEqual(result["removed_files"], 0)
        self.assertIsNone(result["max_bytes"])
        self.assertIsNone(result["max_age_seconds"])
        self.assertGreater(result["retained_bytes"], 0)

    def test_abandoned_staging_is_removed_without_retention_limits(self):
        payload = self.artifact()
        abandoned = payload.parent / ".lake-cache-123456789-1.tmp"
        abandoned.write_text("partial")
        live = payload.parent / f".lake-cache-{os.getpid()}-1.tmp"
        live.write_text("in progress")
        result = cache.collect(env=self.env)
        self.assertFalse(abandoned.exists())
        self.assertTrue(payload.exists())
        self.assertTrue(live.exists())
        self.assertEqual(result["removed_files"], 1)

    def test_killed_publisher_staging_does_not_double_subtract_completed_payload(self):
        payload = self.artifact()
        amount = payload.stat().st_blocks * 512
        staging = payload.parent / ".lake-cache-123456789-1.tmp"
        os.link(payload, staging)
        result = cache.collect(env=self.env)
        self.assertFalse(staging.exists())
        self.assertEqual(payload.read_bytes(), b"x" * 8192)
        self.assertEqual(result["retained_bytes"], amount)
        self.assertEqual(result["pinned_bytes"], 0)
        self.assertEqual(result["removed_files"], 1)
        self.assertEqual(result["removed_allocated_bytes"], 0)

    def test_staging_with_only_an_external_alias_does_not_report_reclaimed_blocks(self):
        payload = self.artifact(".lake-cache-123456789-1.tmp")
        workspace = self.home / "workspace.olean"
        os.link(payload, workspace)
        result = cache.collect(env=self.env)
        self.assertFalse(payload.exists())
        self.assertEqual(workspace.read_bytes(), b"x" * 8192)
        self.assertEqual(result["retained_bytes"], 0)
        self.assertEqual(result["pinned_bytes"], 0)
        self.assertEqual(result["removed_allocated_bytes"], 0)

    def test_staging_alias_removal_does_not_prematurely_satisfy_size_target(self):
        winner = self.artifact()
        staging = winner.parent / ".lake-cache-123456789-1.tmp"
        os.link(winner, staging)
        other = self.artifact("c" * 16 + ".olean")
        amount = winner.stat().st_blocks * 512
        result = cache.collect(env=dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB=str(amount / 1024**3)))
        self.assertTrue(winner.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(other.exists())
        self.assertEqual(result["retained_bytes"], amount)
        self.assertEqual(result["removed_allocated_bytes"], amount)

    def test_configuration_recovers_a_cleared_native_root(self):
        self.artifact()
        (self.root / "owner.json").unlink()
        cache.configure(self.home, self.home / "worker", self.env)
        self.assertEqual(json.loads((self.root / "owner.json").read_text()), dict(version=1, uid=os.getuid()))

    def test_configuration_does_not_adopt_an_unrelated_root(self):
        self.artifact()
        (self.root / "owner.json").unlink()
        (self.root / "unrelated").write_text("keep")
        env = dict(self.env)
        cache.configure(self.home, self.home / "worker", env)
        self.assertFalse((self.root / "owner.json").exists())
        self.assertNotIn("LAKE_SHARED_CACHE_ROOT", env)

    def test_preset_root_still_installs_router_path(self):
        router = self.home / "runtime/bin"
        router.mkdir(parents=True)
        (router / "lake").touch()
        self.env["TAUCETI_MACHINE_LAKE_RUNTIME_ROOT"] = str(router.parent)
        cache.configure(self.home, self.home / "worker", self.env)
        self.assertEqual(self.env["PATH"].split(os.pathsep)[0], str(router))

    def test_age_is_opt_in_and_uses_change_time(self):
        path = self.artifact()
        created = path.stat().st_ctime
        os.utime(path, (1, 1))
        env = dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_AGE_HOURS="1")
        cache.collect(env=env, now=created + 1800)
        self.assertTrue(path.exists())
        cache.collect(env=env, now=time.time() + 7200)
        self.assertFalse(path.exists())

    def test_size_target_is_host_wide_and_keeps_workspace_links(self):
        one = self.artifact()
        two = self.artifact("c" * 16 + ".olean", "d" * 16)
        workspace = self.home / "workspace.olean"
        os.link(one, workspace)
        env = dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0")
        result = cache.collect(env=env)
        self.assertTrue(one.exists())
        self.assertFalse(two.exists())
        self.assertEqual(workspace.read_bytes(), b"x" * 8192)
        self.assertGreater(result["pinned_bytes"], 0)
        self.assertGreater(result["retained_bytes"], result["max_bytes"])

    def test_native_executables_without_extensions_follow_requested_retention(self):
        unpinned = self.artifact("1" * 16)
        pinned = self.artifact("2" * 16)
        workspace = self.home / "executable"
        os.link(pinned, workspace)
        cache.collect(env=self.env)
        self.assertTrue(unpinned.exists())
        cache.collect(env=dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0"))
        self.assertFalse(unpinned.exists())
        self.assertTrue(pinned.exists())
        self.assertEqual(workspace.read_bytes(), b"x" * 8192)

    def test_dry_run_preserves_payload_and_metadata(self):
        path = self.artifact()
        env = dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = cache.collect(env=env, dry=True)
        self.assertTrue(path.exists())
        self.assertGreater(result["eligible_files"], 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_symlinked_payload_and_foreign_roots_are_untouched(self):
        outside = self.home / "outside.olean"
        outside.write_text("keep")
        path = self.artifact()
        path.unlink()
        path.symlink_to(outside)
        env = dict(self.env, TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB="0")
        cache.collect(env=env)
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(path.is_symlink())
        (self.root / "owner.json").unlink()
        result = cache.collect(env=env)
        self.assertIn("unmanaged", result["reason"])

    def test_unavailable_optional_root_keeps_private_operation(self):
        obstruction = self.home / "file"
        obstruction.write_text("keep")
        env = {"TAUCETI_MACHINE_LAKE_CACHE_ROOT": str(obstruction / "cache")}
        cache.configure(self.home, self.home / "worker", env)
        self.assertNotIn("LAKE_SHARED_CACHE_ROOT", env)
        self.assertEqual(env["LAKE_CACHE_DIR"], str(self.home / "worker/.cache/lake"))

    def test_shared_native_preparation_bypasses_private_expander(self):
        from tauceti_worker import expanded_cache

        env = {"LAKE_CACHE_DIR": "/private", "LAKE_SHARED_CACHE_ROOT": str(self.root)}
        with patch.object(cache, "selected", return_value=True):
            with expanded_cache.preparation(self.home, env) as warm:
                self.assertIs(warm, env)

    def test_stalled_capability_probe_uses_existing_deadline_and_keeps_private_mode(self):
        with patch("tauceti_worker.processes.run", side_effect=subprocess.TimeoutExpired("lake", 0)) as run:
            self.assertFalse(cache.selected(self.home, self.env, deadline=0))
        self.assertEqual(run.call_args.kwargs["timeout"], 0)


class RuntimeRouter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "original"
        self.bin.mkdir()
        self.router = self.root / "router/bin"
        self.router.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "scripts/machine-lake-router"
        shutil.copy2(source, self.router / "lake")
        self.original = self.program(self.bin / "lake", "import sys; print('private', *sys.argv[1:])")
        self.native = self.program(self.root / "native", "import sys; print('shared', *sys.argv[1:])")
        self.program(self.bin / "elan", f"print({str(self.original)!r})")
        info = self.original.stat()
        self.registry = {
            str(self.original): dict(
                executable=str(self.native),
                original_signature=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
            )
        }
        self.env = dict(
            os.environ,
            PATH=f"{self.router}:{self.bin}:{os.environ['PATH']}",
            LAKE_SHARED_CACHE_ROOT=str(self.root / "cache"),
        )
        self.save_registry()

    def program(self, path, body):
        path.write_text(f"#!{sys.executable}\n{body}\n")
        path.chmod(0o755)
        return path

    def save_registry(self):
        (self.router / "registry.json").write_text(json.dumps(self.registry))

    def run_lake(self, env=None, args=("build", "Example")):
        result = subprocess.run([str(self.router / "lake"), *args], env=env or self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_matching_runtime_is_selected_and_arguments_survive(self):
        self.assertEqual(self.run_lake(), "shared build Example")

    def test_explicit_toolchain_argument_is_handled_by_elan(self):
        self.assertEqual(self.run_lake(args=("+other", "build")), "private +other build")

    def test_unregistered_or_changed_original_keeps_normal_lake(self):
        self.registry.clear()
        self.save_registry()
        self.assertEqual(self.run_lake(), "private build Example")

    def test_updated_toolchain_invalidates_optional_runtime(self):
        with self.original.open("a") as handle:
            handle.write("# toolchain changed\n")
        self.assertEqual(self.run_lake(), "private build Example")

    def test_missing_or_unexecutable_runtime_falls_back(self):
        self.native.chmod(0o644)
        self.assertEqual(self.run_lake(), "private build Example")
        self.native.unlink()
        self.assertEqual(self.run_lake(), "private build Example")

    def test_malformed_registry_and_opt_out_keep_normal_lake(self):
        (self.router / "registry.json").write_text("broken")
        self.assertEqual(self.run_lake(), "private build Example")
        self.save_registry()
        self.assertEqual(self.run_lake(dict(self.env, LAKE_SHARED_CACHE_ROOT="")), "private build Example")


if __name__ == "__main__":
    unittest.main()

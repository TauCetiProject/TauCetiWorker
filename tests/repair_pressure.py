"""Check repair pressure assets and cgroups; opt-in compilation waits for real capacity."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import repair_sandbox


class PressureAsset(unittest.TestCase):
    def test_readonly_wheel_asset_is_copied_atomically_without_modifying_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            package = installed / "tauceti_worker"
            (package / "prompts").mkdir(parents=True)
            (package / "scripts").mkdir()
            source = package / "scripts/tauceti-lean"
            source.write_bytes(repair_sandbox._pressure_source().read_bytes())
            source.chmod(0o644)
            original = source.read_bytes()
            worktree = installed / "worktree"
            (worktree / ".lake").mkdir(parents=True)
            with (
                patch.object(repair_sandbox, "__file__", str(installed / "tauceti_queue/repair_sandbox.py")),
                patch.object(Path, "chmod", side_effect=PermissionError("read-only installation")),
            ):
                self.assertEqual(repair_sandbox._pressure_source(), source)
                executable = repair_sandbox._pressure_executable(worktree)
                self.assertEqual(repair_sandbox._pressure_executable(worktree), executable)
                executable.write_text("untrusted cache replacement")
                self.assertEqual(repair_sandbox._pressure_executable(worktree).read_bytes(), original)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.stat().st_mode & 0o777, 0o644)
            self.assertTrue(executable.is_relative_to(worktree / ".lake/pressure-cache"))
            self.assertTrue(os.access(executable, os.X_OK))
            self.assertEqual(executable.read_bytes(), original)
            self.assertEqual(list(executable.parent.glob(".wrapper-*")), [])

    def test_wheel_copy_cannot_follow_build_cache_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_text("#!/usr/bin/env python3\n")
            source.chmod(0o644)
            outside = root / "outside"
            outside.mkdir()
            worktree = root / "worktree"
            (worktree / ".lake").mkdir(parents=True)
            (worktree / ".lake/pressure-cache").symlink_to(outside, target_is_directory=True)
            with patch.object(repair_sandbox, "_pressure_source", return_value=source):
                with self.assertRaises(OSError):
                    repair_sandbox._pressure_executable(worktree)
            self.assertEqual(list(outside.iterdir()), [])


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bwrap"), "requires Linux and bubblewrap")
class RepairPressure(unittest.TestCase):
    def test_real_sandbox_retains_capped_ancestors(self):
        with tempfile.TemporaryDirectory(prefix="repair-pressure-") as directory:
            worktree = Path(directory)
            (worktree / "lakefile.toml").write_text(
                'name = "pressure_fixture"\ndefaultTargets = ["Probe"]\n[[lean_lib]]\nname = "Probe"\n'
            )
            (worktree / "Probe.lean").write_text("theorem pressure_fixture : True := trivial\n")
            (worktree / "lake-manifest.json").write_text(
                json.dumps(
                    {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [], "name": "pressure_fixture"}
                )
            )
            # A candidate's helper must never replace the trusted worker asset.
            (worktree / "scripts").mkdir()
            (worktree / "scripts/tauceti-lean").write_text("raise RuntimeError('untrusted helper')\n")
            env = {key: os.environ[key] for key in ("HOME", "PATH", "ELAN_HOME") if key in os.environ}
            probe = """
import json, os, runpy
from pathlib import Path
gate = runpy.run_path(os.environ['LEAN'])
relative = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
group, root = Path('/sys/fs/cgroup') / relative.lstrip('/'), Path('/sys/fs/cgroup')
limits = []
while group.is_relative_to(root):
    maximum = group / 'memory.max'
    if maximum.exists() and maximum.read_text().strip() != 'max':
        limits.append(int(maximum.read_text()))
    group = group.parent
print(json.dumps({'cgroup': relative, 'limits': limits, 'memory': gate['memory']()}))
"""
            command = repair_sandbox.sandbox_argv(["python3", "-c", probe], worktree, env)
            observed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(observed.returncode, 0, observed.stderr)
            telemetry = json.loads(observed.stdout)
            host_cgroup = next(
                line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
            )
            self.assertEqual(telemetry["cgroup"], host_cgroup)
            group, root = Path("/sys/fs/cgroup") / host_cgroup.lstrip("/"), Path("/sys/fs/cgroup")
            limits = []
            while group.is_relative_to(root):
                maximum = group / "memory.max"
                if maximum.exists() and maximum.read_text().strip() != "max":
                    limits.append(int(maximum.read_text()))
                group = group.parent
            self.assertEqual(telemetry["limits"], limits)
            if limits:
                self.assertLessEqual(telemetry["memory"][0], min(limits))

    @unittest.skipUnless(
        os.environ.get("TAUCETI_REPAIR_COMPILE_INTEGRATION") == "1",
        "opt-in compilation honors actual resource waits; set TAUCETI_REPAIR_COMPILE_INTEGRATION=1",
    )
    def test_real_sandbox_compiles_through_gate(self):
        if not shutil.which("lake"):
            self.skipTest("requires an installed Lean toolchain")
        with tempfile.TemporaryDirectory(prefix="repair-pressure-build-") as directory:
            worktree = Path(directory)
            (worktree / "lakefile.toml").write_text(
                'name = "pressure_fixture"\ndefaultTargets = ["Probe"]\n[[lean_lib]]\nname = "Probe"\n'
            )
            (worktree / "Probe.lean").write_text("theorem pressure_fixture : True := trivial\n")
            (worktree / "lake-manifest.json").write_text(
                json.dumps(
                    {"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [], "name": "pressure_fixture"}
                )
            )
            # A candidate's helper must never replace the trusted worker asset.
            (worktree / "scripts").mkdir()
            (worktree / "scripts/tauceti-lean").write_text("raise RuntimeError('untrusted helper')\n")
            env = {key: os.environ[key] for key in ("HOME", "PATH", "ELAN_HOME") if key in os.environ}
            lake = shutil.which("lake")
            if shutil.which("elan"):
                lake = subprocess.check_output(["elan", "which", "lake"], env=env, text=True).strip()
            wheel_source = worktree / "wheel-asset"
            wheel_source.write_bytes(repair_sandbox._pressure_source().read_bytes())
            wheel_source.chmod(0o644)
            with patch.object(repair_sandbox, "_pressure_source", return_value=wheel_source):
                build_command = repair_sandbox.sandbox_argv([lake, "-v", "build", "--iofail"], worktree, env)
                with patch.dict(os.environ, {repair_sandbox.DISABLE_VARIABLE: "0"}):
                    unsandboxed = repair_sandbox.sandboxed_lake(["true"], worktree, env)
                cached = repair_sandbox._pressure_executable(worktree)
                self.assertIn(f"LEAN={cached}", unsandboxed)
            build = subprocess.run(
                build_command,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            self.assertEqual(wheel_source.stat().st_mode & 0o777, 0o644)
            overlays = list((worktree / ".lake/pressure-cache/tauceti/lean-pressure").glob("*/bin/lean"))
            self.assertEqual(len(overlays), 1)
            self.assertEqual(overlays[0].read_bytes(), repair_sandbox._pressure_source().read_bytes())
            self.assertIn(str(overlays[0]), build.stdout + build.stderr)
            self.assertTrue((worktree / ".lake/build/lib/lean/Probe.olean").is_file())


if __name__ == "__main__":
    unittest.main()

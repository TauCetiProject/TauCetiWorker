"""Run the repair compiler gate in a real sandbox, including its cgroup view."""

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
    def test_wheel_asset_is_trusted_and_executable_without_worker_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            package = installed / "tauceti_worker"
            (package / "prompts").mkdir(parents=True)
            (package / "scripts").mkdir()
            source = package / "scripts/tauceti-lean"
            source.write_bytes(repair_sandbox._pressure_source().read_bytes())
            source.chmod(0o644)
            with patch.object(repair_sandbox, "__file__", str(installed / "tauceti_queue/repair_sandbox.py")):
                self.assertEqual(repair_sandbox._pressure_source(), source)
            self.assertTrue(os.access(source, os.X_OK))


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bwrap"), "requires Linux and bubblewrap")
class RepairPressure(unittest.TestCase):
    def test_real_sandbox_retains_capped_ancestors_and_compiles_through_gate(self):
        if not shutil.which("lake"):
            self.skipTest("requires an installed Lean toolchain")
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
            lake = shutil.which("lake")
            if shutil.which("elan"):
                lake = subprocess.check_output(["elan", "which", "lake"], env=env, text=True).strip()
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
            build = subprocess.run(
                repair_sandbox.sandbox_argv([lake, "-v", "build", "--iofail"], worktree, env),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            overlays = list((worktree / ".lake/pressure-cache/tauceti/lean-pressure").glob("*/bin/lean"))
            self.assertEqual(len(overlays), 1)
            self.assertEqual(overlays[0].read_bytes(), repair_sandbox._pressure_source().read_bytes())
            self.assertIn(str(overlays[0]), build.stdout + build.stderr)
            self.assertTrue((worktree / ".lake/build/lib/lean/Probe.olean").is_file())


if __name__ == "__main__":
    unittest.main()

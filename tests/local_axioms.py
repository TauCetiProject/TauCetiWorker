#!/usr/bin/env python3
"""Real compiled-module fixtures: cheap local checks must never load the import closure.

Set LEAN to an installed toolchain to run Lean fixtures (no downloads/network in this test).
The wrapper/dispatch tests elsewhere run without Lean. This intentionally tests local diagnostics,
not equivalence to the full transitive/global CI audit.
"""

import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts/LocalAxioms.lean"
LEAN = os.environ.get("LEAN") or shutil.which("lean")


class NativeCache(unittest.TestCase):
    def test_concurrent_build_reuse_invalidation_and_interrupted_build(self):
        wrapper = runpy.run_path(str(ROOT / "scripts/tauceti-local-axioms"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prefix = root / "toolchain"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "bin/lean").write_text("lean")
            (prefix / "bin/leanc").write_text("leanc")
            driver = root / "Driver.lean"
            driver.write_text("checker source")
            cache = root / "cache"
            cache.mkdir(mode=0o700)
            builds = []

            def compiler(argv, *, cwd, env, check):
                self.assertEqual(env["LEAN_PATH"], "")
                if argv[0].endswith("leanc"):
                    builds.append(str(cwd))
                    time.sleep(0.02)
                    (Path(cwd) / "checker").write_text("complete executable")
                    (Path(cwd) / "checker").chmod(0o755)

            with patch("subprocess.run", side_effect=compiler):
                with ThreadPoolExecutor(max_workers=20) as pool:
                    results = list(pool.map(lambda _: wrapper["checker"](driver, prefix, cache), range(20)))
                self.assertEqual(len(builds), 1)
                self.assertEqual(len(set(results)), 1)
                driver.write_text("new checker source")
                wrapper["checker"](driver, prefix, cache)
                (prefix / "bin/lean").write_text("changed toolchain")
                wrapper["checker"](driver, prefix, cache)
                self.assertEqual(len(builds), 3)
            driver.write_text("interrupted source")
            abandoned = cache / "build-killed"
            abandoned.mkdir()
            (abandoned / "partial.c").write_text("abandoned compiler output")
            with patch("subprocess.run", side_effect=subprocess.CalledProcessError(23, "compiler")):
                with self.assertRaises(subprocess.CalledProcessError):
                    wrapper["checker"](driver, prefix, cache)
            self.assertFalse(list(cache.glob("build-*")))
            with patch("subprocess.run", side_effect=compiler):
                wrapper["checker"](driver, prefix, cache)
            self.assertEqual(len(builds), 4)  # No marker/reset required after interruption.

    def test_empty_scope_needs_no_lean_and_missing_list_fails(self):
        with tempfile.TemporaryDirectory() as name:
            listed = Path(name) / "empty.txt"
            listed.write_text("")
            command = [sys.executable, str(ROOT / "scripts/tauceti-local-axioms"), "--modules", str(listed)]
            result = subprocess.run(command, env=dict(os.environ, PATH="/nonexistent"), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("checked 0 changed module(s)", result.stdout)
            listed.unlink()
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 75)
            self.assertIn("local-axioms:", result.stderr)


@unittest.skipUnless(LEAN, "set LEAN to run the real compiled-module fixtures")
class LocalAxioms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tooling = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tooling.cleanup)
        wrapper = runpy.run_path(str(ROOT / "scripts/tauceti-local-axioms"))
        prefix = Path(subprocess.check_output([LEAN, "--print-prefix"], text=True).strip())
        cls.native = wrapper["checker"](DRIVER, prefix, Path(cls.tooling.name))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, LEAN_PATH=str(self.root))

    def build(self, mod, source):
        path = self.root / (mod.replace(".", "/") + ".lean")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        result = subprocess.run(
            [LEAN, "-o", str(path.with_suffix(".olean")), str(path)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return path

    def audit(self, modules, *, root="TauCeti"):
        listed = self.root / "module list.txt"
        listed.write_text("\n".join(modules) + "\n")
        result = subprocess.run(
            [LEAN, "--run", str(DRIVER), str(listed), root, str(self.root)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        native_started = time.monotonic()
        native = subprocess.run(
            [str(self.native), str(listed), root, str(self.root)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.elapsed = time.monotonic() - native_started
        self.assertEqual(native.returncode, result.returncode, native.stdout + native.stderr)
        self.assertEqual(native.stdout, result.stdout)
        self.assertEqual(native.stderr, result.stderr)
        return result

    def test_reads_only_changed_artifact_even_when_imports_are_unavailable(self):
        dependency = self.build("TauCeti.Dependency", "module\npublic theorem prior : True := True.intro\n")
        self.build("TauCeti.Changed", "module\npublic import TauCeti.Dependency\npublic theorem now : True := prior\n")
        # If the checker follows imports at all, this must fail. Unrelated source artifacts are
        # deliberately missing too: neither a full inventory nor an imported environment can work.
        for suffix in (".olean", ".olean.server", ".olean.private"):
            dependency.with_suffix(suffix).unlink()
        for i in range(100):
            (self.root / "TauCeti" / f"Unrelated{i}.lean").write_text("module\npublic axiom unrelated : False\n")
        result = self.audit(["TauCeti.Changed"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checked 1 changed module(s), 1 declaration(s)", result.stdout)
        print(f"changed-file check with missing imports/100 unrelated files: {self.elapsed:.3f}s", flush=True)

    def test_allowed_axioms_private_opaque_and_empty_modules(self):
        self.build(
            "TauCeti.Good",
            "module\npublic section\n"
            "private theorem hidden : True := True.intro\n"
            "theorem ext (a b : Prop) (h : a ↔ b) : a = b := propext h\n"
            "noncomputable def chosen : Nat := Classical.choice inferInstance\n"
            "opaque good : True := True.intro\n",
        )
        self.build("TauCeti.Empty", "module\n")
        result = self.audit(["TauCeti.Good", "TauCeti.Empty"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checked 2 changed module(s)", result.stdout)

    def test_every_local_escape_hatch_including_private_proofs(self):
        cases = {
            "custom": ("public axiom counterfeit : False", "axiom declaration"),
            "private": ("private axiom counterfeit : False", "axiom declaration"),
            "sorry": ("public theorem bad : False := by sorry", "sorryAx"),
            "admit": ("private theorem bad : False := by admit", "sorryAx"),
            "opaque": ("public opaque bad : False := by sorry", "sorryAx"),
            "native": ("public theorem bad : 1 = 1 := by native_decide", "axiom declaration"),
            "ofReduceBool": ("public axiom Lean.ofReduceBool : False", "axiom declaration"),
            "ofReduceNat": ("public axiom Lean.ofReduceNat : False", "axiom declaration"),
            "trustCompiler": ("public axiom Lean.trustCompiler : False", "axiom declaration"),
        }
        for name, (body, message) in cases.items():
            with self.subTest(name=name):
                self.build("TauCeti.Bad", "module\npublic import Lean\n" + body + "\n")
                result = self.audit(["TauCeti.Bad"])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)

    def test_private_parts_are_required(self):
        path = self.build("TauCeti.Hidden", "module\nprivate theorem bad : False := by sorry\n")
        for suffix in (".olean.private", ".olean.server", ".olean"):
            with self.subTest(suffix=suffix):
                part = path.with_suffix(suffix)
                saved = part.read_bytes()
                part.unlink()
                result = self.audit(["TauCeti.Hidden"])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                part.write_bytes(saved)

    def test_direct_escape_hatch_references_without_loading_dependency(self):
        dependency = self.build("Dependency", "module\npublic axiom Lean.trustCompiler : False\n")
        self.build(
            "TauCeti.Use", "module\npublic import Dependency\npublic theorem bad : False := Lean.trustCompiler\n"
        )
        for suffix in (".olean", ".olean.server", ".olean.private"):
            dependency.with_suffix(suffix).unlink()
        result = self.audit(["TauCeti.Use"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("directly uses Lean.trustCompiler", result.stderr)

    def test_empty_scope_and_missing_invalid_scopes(self):
        result = self.audit([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checked 0 changed module(s)", result.stdout)
        for modules in (["TauCeti.Missing"], ["Mathlib.Algebra"], ["../TauCeti/Hidden"]):
            result = self.audit(modules)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_candidate_initializers_never_execute(self):
        marker = self.root / "initializer-ran"
        self.build(
            "TauCeti.Initializer",
            "module\npublic import Lean\n"
            f'initialize IO.FS.writeFile "{marker}" "bad"\n'
            "public theorem good : True := True.intro\n",
        )
        result = self.audit(["TauCeti.Initializer"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_nonmodule_header_does_not_read_stale_module_parts(self):
        path = self.build("TauCeti.Changed", "module\npublic theorem good : True := True.intro\n")
        self.build("TauCeti.Changed", "theorem good : True := True.intro\n")
        self.assertTrue(path.with_suffix(".olean.private").exists())
        result = self.audit(["TauCeti.Changed"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing leading module", result.stderr)

    def test_repeated_modules_teardown_and_custom_root(self):
        modules = []
        for i in range(12):
            mod = f"Other.Module{i}"
            self.build(mod, f"module\nprivate theorem hidden{i} : True := True.intro\n")
            modules.append(mod)
        result = self.audit(modules + modules, root="Other")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checked 12 changed module(s), 12 declaration(s)", result.stdout)


if __name__ == "__main__":
    unittest.main()

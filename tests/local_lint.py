#!/usr/bin/env python3
"""Scoped native lint: dependency findings stay out, changed docstrings/nolints stay in."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.machinery
import importlib.util

HELPER = Path(__file__).resolve().parents[1] / "scripts/tauceti-local-lint"
loader = importlib.machinery.SourceFileLoader("local_lint", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
lint = importlib.util.module_from_spec(spec)
loader.exec_module(lint)


class LocalLint(unittest.TestCase):
    def test_findings_and_nolints_keep_baseline_semantics(self):
        data = dict(
            findings=[dict(linter="docString", declaration="A.foo", detail="missing doc")],
            nolints=[["simpNF", "A.bar"]],
        )
        self.assertEqual(len(lint.diagnose(data, set(), set())), 2)
        self.assertEqual(lint.diagnose(data, {"docString A.foo"}, {"simpNF A.bar"}), [])

    def test_empty_scope_does_not_probe_or_compile(self):
        with tempfile.TemporaryDirectory() as t:
            modules = Path(t) / "modules.txt"
            modules.write_text("")
            with (
                patch.object(lint, "checker", side_effect=AssertionError("empty scope compiled")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(lint.main(["--modules", str(modules)]), 0)

    def test_standalone_entry_point_needs_no_worker_package(self):
        import shutil

        with tempfile.TemporaryDirectory() as t:
            directory = Path(t)
            shutil.copyfile(HELPER, directory / "tauceti-local-lint")
            modules = directory / "modules.txt"
            modules.write_text("\n")
            result = subprocess.run(
                [sys.executable, "-I", str(directory / "tauceti-local-lint"), "--modules", str(modules)],
                cwd=directory,
                env={"PATH": os.environ["PATH"]},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("0 changed modules", result.stdout)

    def test_invalid_scope_never_reaches_lake(self):
        with tempfile.TemporaryDirectory() as t:
            modules = Path(t) / "modules.txt"
            modules.write_text("../evil\n")
            with patch.object(lint, "checker", side_effect=AssertionError("invalid scope compiled")):
                with self.assertRaises(ValueError):
                    lint.main(["--modules", str(modules)])

    def test_old_checkout_script_is_not_executed_and_missing_report_fails(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "Fixture.lean").touch()
            modules = root / "modules.txt"
            modules.write_text("Fixture\n")
            (root / "scripts").mkdir()
            (root / "scripts/lint-env.sh").write_text("exit 99\n")
            before = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch.object(lint, "checker", return_value=root / "checker"),
                    patch.object(
                        lint.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "no report", "")
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaises(ValueError):
                        lint.main(["--modules", str(modules)])
            finally:
                os.chdir(before)


@unittest.skipUnless(os.environ.get("TAUCETI_LOCAL_LINT_FIXTURE"), "set a cached Lean project for native checks")
class NativeLint(unittest.TestCase):
    def test_real_scoped_documentation_and_environment_checks(self):
        project = Path(os.environ["TAUCETI_LOCAL_LINT_FIXTURE"])
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            batteries = (project / ".lake/packages/batteries").resolve()
            (root / "lean-toolchain").write_bytes((project / "lean-toolchain").read_bytes())
            (root / "lakefile.toml").write_text(
                'name = "fixture"\n[[require]]\nname = "batteries"\npath = '
                + json.dumps(str(batteries))
                + '\n[[lean_lib]]\nname = "Fixture"\n'
            )
            (root / "Fixture").mkdir()
            (root / "Fixture/Dependency.lean").write_text(
                "import Batteries.Tactic.Lint\nnamespace Foreign\ndef undocumented : Nat := 1\nend Foreign\n"
            )
            (root / "Fixture/Good.lean").write_text(
                "import Fixture.Dependency\nnamespace Foreign\n/-- Documented changed definition. -/\ndef documented : Nat := 2\nend Foreign\n"
            )
            with (root / "Fixture/Good.lean").open("a") as out:
                out.write("/-- A documented inductive. -/\ninductive Shape where | node\n")
                out.write(
                    "/-- A documented structure. -/\nstructure Record where\n  /-- Its value. -/\n  value : Nat\n"
                )
            (root / "Fixture/Bad.lean").write_text(
                "import Fixture.Dependency\nnamespace Foreign\ndef undocumentedChanged : Nat := 3\nend Foreign\n"
            )
            (root / "Fixture/Empty.lean").write_text("import Batteries.Tactic.Lint\n")
            (root / "Fixture/BadLint.lean").write_text(
                "import Batteries.Tactic.Lint\nnamespace Foreign\n/-- Documented but incorrectly named definition. -/\ndef bad_name (unused : Nat) : Nat := 4\nend Foreign\n"
            )
            (root / "Fixture/NoLint.lean").write_text(
                "import Batteries.Tactic.Lint\nnamespace Foreign\n/-- Documented definition with a new exemption. -/\n@[nolint unusedArguments] def nolintDef : Nat := 5\nend Foreign\n"
            )
            (root / "Fixture/Unrelated.lean").write_text("this file must never be imported or built\n")
            env = dict(os.environ, TAUCETI_LOCAL_LINT_CACHE=str(root / "checker-cache"))
            (root / ".lake/packages").mkdir(parents=True)
            (root / ".lake/packages/batteries").symlink_to(batteries, target_is_directory=True)
            subprocess.run(
                ["lake", "build", "Fixture.Good", "Fixture.Bad", "Fixture.Empty", "Fixture.BadLint", "Fixture.NoLint"],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
            )
            base = Path(__file__).resolve().parents[1]
            env["PYTHONPATH"] = str(base)

            def dependency_snapshot():
                return {
                    str(p.relative_to(batteries)): (p.stat().st_size, p.stat().st_mtime_ns)
                    for p in (batteries / ".lake/build").rglob("*")
                    if p.is_file()
                }

            before = dependency_snapshot()

            def run(module):
                selected = root / "modules.txt"
                selected.write_text(module + "\n")
                return subprocess.run(
                    [sys.executable, "-I", str(HELPER), "--modules", str(selected)],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            good = run("Fixture.Good")
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            self.assertNotIn("Foreign.undocumented:", good.stdout)
            bad = run("Fixture.Bad")
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn("docString Foreign.undocumentedChanged", bad.stdout)
            self.assertNotIn("docString Foreign.undocumented:", bad.stdout)
            empty = run("Fixture.Empty")
            self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
            self.assertIn("0 documentation checks", empty.stdout)
            bad_lint = run("Fixture.BadLint")
            self.assertEqual(bad_lint.returncode, 1, bad_lint.stdout + bad_lint.stderr)
            self.assertIn("unusedArguments Foreign.bad_name", bad_lint.stdout)
            nolint = run("Fixture.NoLint")
            self.assertEqual(nolint.returncode, 1, nolint.stdout + nolint.stderr)
            self.assertIn("unaccounted nolint: unusedArguments Foreign.nolintDef", nolint.stdout)
            self.assertNotIn("compiling the small checker", bad.stdout + empty.stdout)
            self.assertEqual(dependency_snapshot(), before, "checker compilation modified candidate dependencies")


if __name__ == "__main__":
    unittest.main()

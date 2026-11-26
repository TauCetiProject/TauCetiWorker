#!/usr/bin/env python3
"""A test module run directly, without tests/run-all, never writes to the operator's real state."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
# A fixture that emits pr_activity for a/b with no activity directory of its own: a direct run of it
# before tests/_isolated_state.py wrote that receipt into the real activity database.
LEAKY_FIXTURE = ["fleet_capacity.py", "FleetTests.test_events_are_structured_private_and_deduplicable"]


def bare_environment(home, scratch):
    """The environment of an operator shell: no isolation variables, real-looking HOME."""
    isolated = set(_isolated_state.settings("/unused")) | {_isolated_state.ROOT_VARIABLE}
    env = {k: v for k, v in os.environ.items() if k not in isolated}
    env.update(HOME=str(home), TMPDIR=str(scratch))
    return env


def state_files(home):
    state = Path(home) / ".local/state"
    return sorted(str(p.relative_to(home)) for p in state.rglob("*") if p.is_file()) if state.exists() else []


class DirectRunIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.scratch = Path(self.tmp.name) / "tmp"
        self.home.mkdir()
        self.scratch.mkdir()

    def test_every_module_imports_the_isolation_first(self):
        missing = []
        for path in sorted(TESTS.glob("*.py")):
            if path.name.startswith("_"):
                continue
            tree = ast.parse(path.read_text(), str(path))
            first = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
                ),
                None,
            )
            if not (isinstance(first, ast.Import) and [a.name for a in first.names] == ["_isolated_state"]):
                missing.append(path.name)
        self.assertEqual(missing, [], "these modules must start with `import _isolated_state`")

    def test_lifecycle_script_applies_the_isolation(self):
        self.assertIn("tests/_isolated_state.py --exports", (TESTS / "lifecycle.sh").read_text())

    def test_fixture_would_leak_without_the_isolation(self):
        # The control: the same receipt, emitted without the helper, lands under HOME. This is what
        # the direct-run check below would see if the isolation stopped working.
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from tauceti_worker import fleet_events;"
            "fleet_events.emit('pr_activity', repository='a/b', pr=1)"
        )
        env = bare_environment(self.home, self.scratch)
        env["TAUCETI_FLEET_EVENTS"] = str(self.scratch / "events.jsonl")
        subprocess.run([sys.executable, "-c", code, str(REPO)], env=env, check=True, timeout=120)
        self.assertTrue(any(f.startswith(".local/state/tauceti/activity/pending/") for f in state_files(self.home)))

    def test_direct_run_leaves_home_state_untouched(self):
        result = subprocess.run(
            [sys.executable, str(TESTS / LEAKY_FIXTURE[0]), LEAKY_FIXTURE[1]],
            cwd=REPO,
            env=bare_environment(self.home, self.scratch),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertEqual(state_files(self.home), [])
        # The helper's own scratch directory is gone once the module exits.
        self.assertEqual(sorted(p.name for p in self.scratch.glob("tauceti-test-state-*")), [])

    def test_exported_real_paths_are_overridden(self):
        # A shell that exports the production paths, as a worker's environment does, is overridden too.
        real = self.home / ".local/state/tauceti/activity"
        env = bare_environment(self.home, self.scratch)
        env.update(TAUCETI_ACTIVITY_DIR=str(real), TAUCETI_FLEET_EVENTS=str(real / "worker-events.jsonl"))
        result = subprocess.run(
            [sys.executable, str(TESTS / LEAKY_FIXTURE[0]), LEAKY_FIXTURE[1]],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state_files(self.home), [])

    def test_this_process_is_isolated(self):
        root = Path(os.environ[_isolated_state.ROOT_VARIABLE])
        for name, value in _isolated_state.settings(root).items():
            self.assertEqual(os.environ[name], value)
        self.assertTrue(Path(os.environ["TAUCETI_ACTIVITY_DIR"]).is_relative_to(root))


if __name__ == "__main__":
    unittest.main()

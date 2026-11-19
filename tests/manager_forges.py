#!/usr/bin/env python3
"""Managed source selection beyond the keys: refusals, status, and what the child receives."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tauceti_worker import worker_manager as wm

NATIVE_ENV = {
    "TAUCETI_FORGEJO_ORIGIN": "https://git.example.org",
    "TAUCETI_FORGEJO_REPO": "one/project",
    "TAUCETI_FORGEJO_REPOSITORY_ID": "7",
    "TAUCETI_FORGEJO_TOKEN_FILE": "/private/native-token",
    "TAUCETI_FORGEJO_FORK": "worker/project",
}


def spec(**fields):
    return wm.WorkerSpec.from_dict({"id": "native", **fields}, 0)


class ManagerForges(unittest.TestCase):
    def test_definitions_that_could_never_run_fail_at_load(self):
        for fields, message in (
            ({"forgejo": True, "env": {"TAUCETI_PR": "2"}}, "TAUCETI_PR"),
            ({"github": False, "forgejo": True, "sandbox": "bubble"}, "host only"),
        ):
            with self.subTest(fields=fields), self.assertRaises(wm.WorkersError) as raised:
                spec(**fields)
            self.assertIn(message, str(raised.exception))
        self.assertTrue(spec(forgejo=True, sandbox="bubble").forgejo)
        self.assertEqual(spec(github=False, forgejo=True, env={"TAUCETI_PR": "2"}).env, (("TAUCETI_PR", "2"),))

    def test_status_names_the_selection(self):
        def lines(worker):
            return "\n".join(wm._worker_configuration_lines({"spec": worker.as_dict()}, 100))

        self.assertNotIn("sources:", lines(spec()))
        self.assertIn("github, forgejo", lines(spec(forgejo=True, new_pr_forge="forgejo")))
        self.assertIn("new PRs: forgejo", lines(spec(github=False, forgejo=True, new_pr_forge="forgejo")))
        self.assertIn("new PRs: none", lines(spec(github=False, forgejo=True)))
        snapshot = wm.worker_snapshots([spec(github=False, forgejo=True, new_pr_forge="forgejo")])[0]
        self.assertEqual((snapshot["sources"], snapshot["new_pr_forge"]), (["forgejo"], "forgejo"))

    def test_managed_child_receives_flags_and_connection_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            capture = root / "child.json"
            child = root / "child.py"
            child.write_text(
                "import json, os, sys\n"
                "from tauceti_worker.cli import build_parser\n"
                "from tauceti_worker.worker_sources import configure\n"
                "configure(build_parser().parse_args(sys.argv[1:]))\n"
                "keep = ('TAUCETI_FORGEJO_', 'TAUCETI_WORKER_FORGES', 'TAUCETI_NEW_PR_FORGE')\n"
                "env = {k: v for k, v in os.environ.items() if k.startswith(keep)}\n"
                f"open({str(capture)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'env': env}}))\n"
            )
            worker = spec(github=False, forgejo=True, new_pr_forge="forgejo", restart="never", env=NATIVE_ENV)
            harness = root / "runner.py"
            harness.write_text(
                "import sys\n"
                "from types import SimpleNamespace\n"
                "from tauceti_worker import worker_manager as wm\n"
                f"wm.self_argv = lambda *tail: [sys.executable, {str(child)!r}, *map(str, tail)]\n"
                "raise SystemExit(wm.cmd_managed_runner("
                "SimpleNamespace(spec=sys.argv[1], state_dir=sys.argv[2], runtime_dir=sys.argv[3])))\n"
            )
            environment = {**os.environ, "PYTHONPATH": str(REPO)}
            environment.pop("TAUCETI_MANAGER_TEST_COMMAND", None)
            # Inherited selections must not leak past the definition's flags.
            environment.update(TAUCETI_WORKER_FORGES="github", TAUCETI_NEW_PR_FORGE="github")
            subprocess.run(
                [sys.executable, str(harness), wm._encode_spec(worker), str(root / "state"), str(root / "rt")],
                env=environment,
                capture_output=True,
                check=True,
                timeout=60,
            )
            seen = json.loads(capture.read_text())
            self.assertEqual(seen["argv"][:3], ["work", "--loop", "--worker-id"])
            self.assertIn("--no-github", seen["argv"])
            self.assertEqual(
                seen["env"], {**NATIVE_ENV, "TAUCETI_WORKER_FORGES": "forgejo", "TAUCETI_NEW_PR_FORGE": "forgejo"}
            )


if __name__ == "__main__":
    unittest.main()

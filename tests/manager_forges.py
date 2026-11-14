#!/usr/bin/env python3
"""Forge selection in `workers.toml`: validation, fingerprint, argv and the managed child."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tauceti_worker import worker_manager as wm
from tauceti_worker.cli import build_parser
from tauceti_worker.worker_sources import configure

NATIVE_ENV = {
    "TAUCETI_FORGEJO_ORIGIN": "https://git.example.org",
    "TAUCETI_FORGEJO_REPO": "one/project",
    "TAUCETI_FORGEJO_REPOSITORY_ID": "7",
    "TAUCETI_FORGEJO_TOKEN_FILE": "/private/native-token",
    "TAUCETI_FORGEJO_FORK": "worker/project",
}


def spec(**fields):
    return wm.WorkerSpec.from_dict({"id": "native", **fields}, 0)


def selected(worker):
    """What `tauceti work` configures from the manager's argv, starting from a hostile environment."""
    with patch.dict(os.environ, {"TAUCETI_WORKER_FORGES": "both", "TAUCETI_NEW_PR_FORGE": "forgejo"}, clear=True):
        configure(build_parser().parse_args(worker.work_argv()[3:]))
        return os.environ["TAUCETI_WORKER_FORGES"], os.environ["TAUCETI_NEW_PR_FORGE"]


class ManagerForges(unittest.TestCase):
    def test_unset_keys_keep_the_github_definition_unchanged(self):
        plain = spec()
        self.assertEqual((plain.forges, plain.new_pr_forge), (("github",), "github"))
        self.assertEqual(plain.as_dict(), {"id": "native", "enabled": True})
        self.assertEqual(plain.work_argv(), wm.WorkerSpec(id="native").work_argv())
        self.assertFalse({"--github", "--forgejo", "--new-pr-forge"} & set(plain.work_argv()))
        self.assertEqual(spec(forges=["github"], new_pr_forge="github").fingerprint(), plain.fingerprint())
        self.assertEqual(selected(plain), ("github", "github"))

    def test_keys_reach_work_as_the_flags_would(self):
        for fields, expected in (
            ({"forges": ["forgejo"]}, ("forgejo", "github")),
            ({"forges": ["forgejo"], "new_pr_forge": "forgejo"}, ("forgejo", "forgejo")),
            ({"forges": ["github", "forgejo"]}, ("both", "github")),
            ({"forges": ["forgejo", "github"], "new_pr_forge": "forgejo"}, ("both", "forgejo")),
            ({"forges": ["github"], "new_pr_forge": "github"}, ("github", "github")),
        ):
            with self.subTest(fields=fields):
                worker = spec(**fields)
                self.assertEqual(selected(worker), expected)
                self.assertEqual(wm._decode_spec(wm._encode_spec(worker)), worker)

    def test_selection_is_part_of_the_fingerprint_but_not_its_order(self):
        both = spec(forges=["github", "forgejo"])
        self.assertEqual(both.as_dict()["forges"], ["github", "forgejo"])
        self.assertEqual(both.fingerprint(), spec(forges=["forgejo", "github"]).fingerprint())
        self.assertNotEqual(both.fingerprint(), spec().fingerprint())
        self.assertNotEqual(
            both.fingerprint(), spec(forges=["github", "forgejo"], new_pr_forge="forgejo").fingerprint()
        )

    def test_definitions_the_worker_would_refuse_fail_at_load(self):
        for fields, message in (
            ({"forges": ["gitlab"]}, "unknown forge(s): gitlab"),
            ({"forges": []}, "at least one"),
            ({"forges": ["forgejo", "forgejo"]}, "more than once"),
            ({"forges": "forgejo"}, "array of strings"),
            ({"new_pr_forge": "gitlab"}, "'github' or 'forgejo'"),
            ({"new_pr_forge": "forgejo"}, "one of the forges"),
            ({"forges": ["forgejo"], "new_pr_forge": "github"}, "one of the forges"),
            ({"forges": ["github", "forgejo"], "env": {"TAUCETI_PR": "2"}}, "single forge"),
            ({"forges": ["forgejo"], "sandbox": "bubble"}, "host only"),
            ({"forges": ["github", "forgejo"], "new_pr_forge": "forgejo", "sandbox": "bubble"}, "host only"),
        ):
            with self.subTest(fields=fields), self.assertRaises(wm.WorkersError) as raised:
                spec(**fields)
            self.assertIn(message, str(raised.exception))

    def test_bubble_with_both_forges_keeps_its_github_destination(self):
        worker = spec(forges=["github", "forgejo"], sandbox="bubble")
        self.assertIn("--bubble", worker.work_argv())
        self.assertEqual(selected(worker), ("both", "github"))

    def test_status_names_the_selection(self):
        def lines(worker):
            return "\n".join(wm._worker_configuration_lines({"spec": worker.as_dict()}, 100))

        self.assertNotIn("forges:", lines(spec()))
        self.assertIn("github, forgejo", lines(spec(forges=["github", "forgejo"], new_pr_forge="forgejo")))
        self.assertIn("new PRs: forgejo", lines(spec(forges=["forgejo"], new_pr_forge="forgejo")))
        self.assertIn("new PRs: none", lines(spec(forges=["forgejo"])))
        snapshot = wm.worker_snapshots([spec(forges=["forgejo"], new_pr_forge="forgejo")])[0]
        self.assertEqual((snapshot["forges"], snapshot["new_pr_forge"]), (["forgejo"], "forgejo"))

    def test_fleet_protocol_reports_the_keys(self):
        result = subprocess.run(
            [sys.executable, "-m", "tauceti_worker", "_fleet-protocol"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        self.assertEqual(json.loads(result.stdout)["forges"], 1)

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
            worker = spec(forges=["forgejo"], new_pr_forge="forgejo", restart="never", env=NATIVE_ENV)
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

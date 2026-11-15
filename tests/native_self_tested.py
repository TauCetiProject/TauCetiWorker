#!/usr/bin/env python3
"""Native source transport exercises the common persistent self-tested worker flow."""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_forgejo_source as fixture
from tauceti_tools.forge import ForgeError

from tauceti_worker.forgejo_worker import ForgejoWorkerSource


class NativeSelfTested(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NativeSource()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.api._token = "token"
        f.api.max_bytes = 1024 * 1024
        f.api.repository.return_value = dict(id=41, full_name=f.repository.name, archived=False)
        self.source = ForgejoWorkerSource(f.identity, f.api, token_file=f.clone / "token")
        self.live = dict(
            state="open",
            draft=False,
            base=dict(ref="main"),
            head=dict(sha="a" * 40, ref="feature"),
            user=dict(id=7, login="reviewer"),
            labels=[],
        )
        self.source.live_pull = Mock(side_effect=lambda n: self.live)

        def label(number, name):
            self.live["labels"] = [dict(name=name)]
            return True

        self.source.add_label = Mock(side_effect=label)

    def test_roadmap_branch_lookup_is_fresh_owner_bound_and_unambiguous(self):
        row = dict(number=1, headRefName="roadmap/x", headRepositoryOwner=dict(login="team"))
        self.source.reader.pulls = Mock(return_value=[row])
        self.assertEqual(self.source.pull_for_head("team", "roadmap/x"), 1)
        self.assertIsNone(self.source.pull_for_head("other", "roadmap/x"))
        self.source.reader.pulls.return_value = [row, {**row, "number": 2}]
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.source.reader.pulls.side_effect = ForgeError("outage", status=503)
        self.assertIsNone(self.source.pull_for_head("team", "roadmap/x"))
        self.assertEqual(self.source.reader.pulls.call_count, 4)

    def test_native_roadmap_receipt_through_persistent_label(self):
        from types import SimpleNamespace

        from tauceti_worker import self_tested_label, verification

        root, state = self.fixture.clone / "content", self.fixture.clone / "verification-state"
        (root / "TauCeti").mkdir(parents=True)

        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.com")
        (root / "TauCeti/A.lean").write_text("-- base\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("remote", "add", "origin", self.source.repository.clone_url)
        git("update-ref", "refs/remotes/origin/main", base)
        (root / "TauCeti/A.lean").write_text("-- verified change\n")
        git("commit", "-q", "-am", "change")
        head = git("rev-parse", "HEAD")
        self.live["head"].update(sha=head, ref="roadmap/x")
        self.source.reader.pulls = Mock(
            return_value=[dict(number=1, headRefName="roadmap/x", headRepositoryOwner=dict(login="team"))]
        )
        project = self.fixture.clone / "project.toml"
        self.source.write_cli_configuration(project)
        inputs = dict(base=base, sources=verification.inventory(root, base))
        fingerprint = verification.digest(inputs)
        inputs_path = state / "inputs.json"
        inputs_path.parent.mkdir()
        inputs_path.write_text(json.dumps(inputs))
        receipt = state / "verification" / "run" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                dict(
                    schema=verification.SCHEMA,
                    status="success",
                    requested_phases=list(verification.STAGES),
                    phases={name: dict(status="success") for name in verification.STAGES},
                    scope=dict(policy="ordinary"),
                    inputs=dict(path=str(inputs_path), sha256=fingerprint),
                    fingerprint=fingerprint,
                )
            )
        )
        worker = SimpleNamespace(gh=self.source, cfg=SimpleNamespace(checkout=root, state=state))
        candidate = SimpleNamespace(
            pr=None, progress=dict(published_branches={"roadmap/x": head}, published_owner="team")
        )
        with (
            patch.dict(os.environ, TAUCETI_SELF_TESTED="1", TAUCETI_SOURCE_GH_CONF=str(project)),
            patch("tauceti_worker.worker_sources.canonical_mathematics_source", return_value=True),
        ):
            os.environ.pop("TAUCETI_CONF", None)
            result = self_tested_label.label_round_pushes(worker, candidate, "roadmap", time.time() - 5)
            self.assertNotIn("TAUCETI_CONF", os.environ)
        self.assertEqual(result, [(1, f"labelled {head[:12]}: persistent self-tested label applied")])
        self.source.add_label.assert_called_once_with(1, "self-tested")

    def test_native_checkout_separates_local_branch_from_remote_pr_branch(self):
        from tauceti_worker.source_cli import checkout

        row = dict(head=dict(sha="a" * 40, ref="remote/pr-branch", repo=dict(full_name="team/Math")))
        self.fixture.api.pull = Mock(return_value=row)
        with (
            patch("tauceti_worker.source_cli.subprocess.run") as run,
            patch("tauceti_worker.source_cli.subprocess.check_output", return_value="a" * 40 + "\n"),
        ):
            self.assertEqual(checkout(["1", "--force", "--branch", "tauceti/worker/pr-1"], self.source), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "checkout", "--force", "-B", "tauceti/worker/pr-1", "a" * 40], commands)
        self.assertIn(["git", "config", "branch.tauceti/worker/pr-1.merge", "refs/heads/remote/pr-branch"], commands)
        self.assertTrue(
            any(
                command[:2] == ["git", "fetch"] and command[-1] == "refs/heads/remote/pr-branch" for command in commands
            )
        )


class CanonicalSource(unittest.TestCase):
    def test_only_enrolled_native_content_repository_qualifies(self):
        from types import SimpleNamespace

        from tauceti_tools.ci_candidate import RepositoryIdentity
        from tauceti_tools.forge import Forge, RepositoryRef

        from tauceti_worker.agents import self_tested_guidance
        from tauceti_worker.self_tested_label import enabled

        with patch.dict(os.environ, TAUCETI_SELF_TESTED="1"):
            for origin, name, native_id, eligible in (
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 1, True),
                ("https://git.taucetiproject.org", "TauCetiProject/TauCeti", 2, False),
                ("https://elsewhere.example", "TauCetiProject/TauCeti", 1, False),
                ("https://git.taucetiproject.org", "Another/TauCeti", 1, False),
            ):
                repository = RepositoryRef(Forge("forgejo", origin), name)
                source = SimpleNamespace(repository=repository, identity=RepositoryIdentity(repository, native_id))
                with patch("tauceti_worker.worker_sources.current", return_value=source):
                    self.assertEqual(enabled() == "", eligible)
                    self.assertEqual(bool(self_tested_guidance("ordinary")), eligible)
                    self.assertFalse(self_tested_guidance("full-lint"))


if __name__ == "__main__":
    unittest.main()

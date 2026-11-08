#!/usr/bin/env python3
"""Native source Git refuses cross-binding, unsafe refs and unconfirmed publication."""

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, Forge, ForgeError, Incomplete, RepositoryRef

from tauceti_queue.forgejo_git import ForgejoGitSource


class SourceGit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clone = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True, capture_output=True)
        self.repository = RepositoryRef(Forge("forgejo", "https://source.example"), "team/Math")
        self.identity = RepositoryIdentity(self.repository, 41)
        self.api = SimpleNamespace(
            forge=self.repository.forge,
            repository=Mock(return_value={"id": 41, "full_name": "team/Math"}),
            request=Mock(return_value=[]),
        )
        self.source = ForgejoGitSource(self.identity, self.api, self.clone, token="fixture-source-token")

    def test_credentials_are_scoped_and_do_not_inherit_execution_or_trace_environment(self):
        with patch.dict(
            os.environ, GH_TOKEN="ci-only-token", GIT_TRACE="1", GIT_CONFIG_COUNT="99", GIT_SSL_NO_VERIFY="1"
        ):
            env = self.source._environment()
        self.assertFalse({"GH_TOKEN", "GIT_TRACE", "GIT_SSL_NO_VERIFY"} & env.keys())
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://source.example/team/Math.git.extraHeader")
        self.assertIn(base64.b64encode(b"x-access-token:fixture-source-token").decode(), env["GIT_CONFIG_VALUE_1"])
        self.assertNotIn("fixture-source-token", " ".join(self.source.command))
        self.assertIn("http.followRedirects=false", self.source.command)
        self.assertIn("http.sslVerify=true", self.source.command)

    def test_cross_origin_client_or_unqualified_source_is_refused(self):
        for identity, api in (
            (self.repository, self.api),
            (self.identity, SimpleNamespace(forge=Forge("forgejo", "https://other.example"))),
        ):
            with self.assertRaises(ValueError):
                ForgejoGitSource(identity, api, self.clone, token="token")

    def test_bad_tokens_and_timeouts_are_refused(self):
        for token in ("", None, "bad\nheader", "a" * 4097):
            with self.assertRaises(ValueError):
                ForgejoGitSource(self.identity, self.api, self.clone, token=token)
        for timeout in (0, -1, 601, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                ForgejoGitSource(self.identity, self.api, self.clone, token="token", timeout=timeout)

    def test_refspec_injection_is_refused_before_api_or_network(self):
        for refs in (
            [],
            ["--upload-pack=evil"],
            ["refs/heads/main"],
            ["+refs/heads/*:refs/remotes/*"],
            ["+refs/heads/main:../../escape"],
            ["0" * 40],
            ["+refs/heads/main:refs/a\nrefs/b"],
        ):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.source.fetch_refs("team/Math", refs)
        self.api.repository.assert_not_called()

    def test_wrong_binding_and_changed_native_id_are_not_empty_successes(self):
        with self.assertRaises(ValueError):
            self.source.fetch_refs("other/Math", ["a" * 40])
        self.api.repository.return_value = {"id": 42, "full_name": "team/Math"}
        with patch.object(self.source, "_git") as git:
            with self.assertRaises(Incomplete):
                self.source.fetch_refs("team/Math", ["a" * 40])
            git.assert_not_called()

    def test_timeouts_preserve_write_uncertainty_without_leaking_exception_data(self):
        with patch("tauceti_queue.forgejo_git.subprocess.Popen", side_effect=OSError("SECRET")) as run:
            with self.assertRaises(AmbiguousWrite) as error:
                self.source._git("push", write=True)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn("fixture-source-token", str(run.call_args.args))
            with self.assertRaises(ForgeError) as error:
                self.source._git("fetch")
            self.assertNotIsInstance(error.exception, AmbiguousWrite)
            self.assertEqual(run.call_count, 2)

    def test_interrupted_caller_terminates_and_reaps_the_git_group(self):
        process = Mock(args=[], pid=12345)
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
        with (
            patch("tauceti_queue.forgejo_git.subprocess.Popen", return_value=process),
            patch("tauceti_queue.forgejo_git.os.killpg") as kill,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.source._git("push", write=True)
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0], 12345)
            self.assertEqual(process.communicate.call_count, 2)

    def test_failed_publication_does_not_surface_server_supplied_secrets(self):
        process = Mock(args=[], returncode=1)
        process.communicate.return_value = (b"", b"SECRET fixture-source-token")
        with patch("tauceti_queue.forgejo_git.subprocess.Popen", return_value=process):
            with self.assertRaises(AmbiguousWrite) as error:
                self.source._git("push", write=True)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn("fixture-source-token", str(error.exception))

    def test_targeted_ref_lookup_ignores_extra_prefix_matches_and_rejects_duplicates(self):
        exact = {"ref": "refs/heads/main", "object": {"sha": "a" * 40}}
        self.api.request.return_value = [{"ref": "refs/heads/x/refs/heads/main", "object": {"sha": "b" * 40}}, exact]
        self.assertEqual(self.source.ref_sha("team/Math", "heads/main"), "a" * 40)
        self.api.request.assert_called_once_with("GET", "repos/team/Math/git/refs/heads/main")
        self.api.request.return_value = [exact, exact]
        with self.assertRaises(Incomplete):
            self.source.ref_sha("team/Math", "heads/main")

    def test_custom_local_queue_refs_resolve_to_protected_remote_branches(self):
        self.api.request.return_value = [{"ref": "refs/heads/tauceti-ci/queue/input/1", "object": {"sha": "a" * 40}}]
        self.assertEqual(self.source.ref_sha("team/Math", "refs/tauceti-queue/input/1"), "a" * 40)
        self.api.request.assert_called_once_with("GET", "repos/team/Math/git/refs/heads/tauceti-ci/queue/input/1")
        self.api.repository.return_value = {"id": 41, "full_name": "TEAM/math"}
        self.source._bound("team/Math")

    def promotion(self, current, output, code, *, initial="a" * 40):
        self.source._commit = Mock()
        self.source.ref_sha = Mock(side_effect=[initial, current])

        def git(*args, **kwargs):
            return subprocess.CompletedProcess(
                [], code if args[0] == "push" else 0, output if args[0] == "push" else b"", b""
            )

        self.source._git = Mock(side_effect=git)
        return self.source.fast_forward("team/Math", "main", "a" * 40, "b" * 40)

    def test_lost_acknowledgement_reconciles_an_exact_landed_tip(self):
        self.assertTrue(self.promotion("b" * 40, b"", 1).ok)

    def test_successful_server_ack_does_not_depend_on_a_later_tip_read(self):
        self.assertTrue(self.promotion("c" * 40, b" \t" + b"b" * 40 + b":refs/heads/main\told..new\n", 0).ok)
        self.source.ref_sha.assert_called_once()

    def test_unacknowledged_unknown_publication_stays_uncertain(self):
        with self.assertRaises(AmbiguousWrite):
            self.promotion("c" * 40, b"", 1)
        self.assertFalse(
            self.promotion("c" * 40, b"!\t" + b"b" * 40 + b":refs/heads/main\t[rejected] (stale info)\n", 1).ok
        )

    def test_an_already_visible_tip_is_reconciled_without_another_push(self):
        self.assertTrue(self.promotion(None, b"", 1, initial="b" * 40).ok)
        self.assertFalse(any(call.args[0] == "push" for call in self.source._git.call_args_list))

    @unittest.skipUnless(Path("/proc").exists(), "Linux process-group observation")
    def test_real_timeout_terminates_git_helpers_in_the_same_process_group(self):
        import signal

        pidfile = self.clone / "child.pid"
        program = (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        self.source.command = [sys.executable, "-c", program]
        self.source.timeout = 2.0
        with self.assertRaises(AmbiguousWrite):
            self.source._git("push", write=True)
        pid = int(pidfile.read_text())
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            state = "gone"
        if state not in {"gone", "Z"}:
            os.kill(pid, signal.SIGKILL)
        self.assertIn(state, {"gone", "Z"})

    def test_retention_cannot_modify_canonical_or_arbitrary_refs(self):
        for ref in ("heads/main", "refs/heads/main", "refs/tags/release", "refs/tauceti-queue/../escape"):
            with self.assertRaises(ValueError):
                self.source.update_ref("team/Math", ref, "a" * 40)
        self.api.repository.assert_not_called()


if __name__ == "__main__":
    unittest.main()

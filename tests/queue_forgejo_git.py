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
            forge=self.repository.forge, repository=Mock(return_value={"id": 41, "full_name": "team/Math"})
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
        with patch(
            "tauceti_queue.forgejo_git.subprocess.run", side_effect=subprocess.TimeoutExpired("SECRET", 1)
        ) as run:
            with self.assertRaises(AmbiguousWrite) as error:
                self.source._git("push", write=True)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn("fixture-source-token", str(run.call_args.args))
            with self.assertRaises(ForgeError) as error:
                self.source._git("fetch")
            self.assertNotIsInstance(error.exception, AmbiguousWrite)
            self.assertEqual(run.call_count, 2)

    def test_failed_publication_does_not_surface_server_supplied_secrets(self):
        result = subprocess.CompletedProcess([], 1, b"", b"SECRET fixture-source-token")
        with patch("tauceti_queue.forgejo_git.subprocess.run", return_value=result):
            with self.assertRaises(AmbiguousWrite) as error:
                self.source._git("push", write=True)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertNotIn("fixture-source-token", str(error.exception))

    def test_retention_cannot_modify_canonical_or_arbitrary_refs(self):
        for ref in ("heads/main", "refs/heads/main", "refs/tags/release", "refs/tauceti-queue/../escape"):
            with self.assertRaises(ValueError):
                self.source.update_ref("team/Math", ref, "a" * 40)
        self.api.repository.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline controls for the macOS configured-file route's metadata check."""

import hashlib
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, quota
from tauceti_worker.config import NoProgress


class FileKeychain(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": "/tmp/worker agent", "TAUCETI_CLAUDE_USAGE_CREDENTIALS": "/tmp/usage.json"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.platform = patch.object(sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def test_absent_checks_metadata_only_with_bound(self):
        with patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=44)) as run:
            self.assertIsNone(quota.claude_file_keychain_problem(os.environ.get("CLAUDE_CONFIG_DIR")))
        service = "Claude Code-credentials-" + hashlib.sha256(b"/tmp/worker agent").hexdigest()[:8]
        run.assert_called_once_with(
            ["/usr/bin/security", "find-generic-password", "-s", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )

    def test_existing_item_refuses_without_reading_secret(self):
        with patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)):
            problem = quota.claude_file_keychain_problem(os.environ.get("CLAUDE_CONFIG_DIR"))
        self.assertIn("shadows", problem)
        self.assertIn("dedicated", problem)

    def test_unknown_or_unavailable_is_not_absence(self):
        for code in (1, 36, 50):
            with self.subTest(code=code):
                with patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=code)):
                    self.assertIn(
                        "cannot verify", quota.claude_file_keychain_problem(os.environ.get("CLAUDE_CONFIG_DIR"))
                    )
        for error in (OSError("missing"), subprocess.TimeoutExpired("security", 5)):
            with self.subTest(error=type(error).__name__):
                with patch.object(quota.subprocess, "run", side_effect=error):
                    self.assertIn(
                        "cannot verify", quota.claude_file_keychain_problem(os.environ.get("CLAUDE_CONFIG_DIR"))
                    )

    def test_unset_feature_and_linux_do_not_query_keychain(self):
        for platform, usage in (("linux", "/tmp/usage.json"), ("darwin", "")):
            with (
                patch.object(sys, "platform", platform),
                patch.dict(os.environ, TAUCETI_CLAUDE_USAGE_CREDENTIALS=usage),
                patch.object(quota.subprocess, "run") as run,
            ):
                self.assertIsNone(quota.claude_file_keychain_problem(os.environ.get("CLAUDE_CONFIG_DIR")))
                run.assert_not_called()

    def test_private_file_launch_does_not_query_host_keychain(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))
        blob = {"claudeAiOauth": {"accessToken": "synthetic-agent", "expiresAt": (time.time() + 3600) * 1000}}
        with (
            patch.dict(os.environ, TAUCETI_CLAUDE_AGENT_CREDENTIAL="file", CLAUDE_CODE_OAUTH_TOKEN="host-only"),
            patch.object(quota, "_read_json_file", return_value=blob),
            patch.object(quota.subprocess, "run", side_effect=AssertionError("private route queried host Keychain")),
        ):
            self.assertIsNone(quota.Quota(cfg)._claude_launch_credential_unusable())

    def test_cache_only_file_observation_does_not_poll_keychain(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))
        reader = quota.Quota(cfg)
        reader._cache_only = True
        blob = {"claudeAiOauth": {"accessToken": "synthetic-agent", "expiresAt": (time.time() + 3600) * 1000}}
        with (
            patch.object(quota, "_read_json_file", return_value=blob),
            patch.object(quota.subprocess, "run", side_effect=AssertionError("cache-only Keychain read")),
        ):
            self.assertIsNone(reader._claude_launch_credential_unusable())

    def test_native_host_launch_refuses_found_and_unknown_shadow(self):
        blob = {"claudeAiOauth": {"accessToken": "synthetic-agent", "expiresAt": (time.time() + 3600) * 1000}}
        for code in (0, 36):
            with (
                self.subTest(code=code),
                patch.object(quota, "_read_json_file", return_value=blob),
                patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=code)) as run,
            ):
                with self.assertRaises(NoProgress):
                    agents.host_agent_launch("do not launch", "claude")
            self.assertNotIn("-w", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_binary_inspection_does_not_validate_credentials(self):
        from tauceti_worker import work_units

        with (
            patch.object(quota.subprocess, "run", side_effect=AssertionError("binary inspection read credentials")),
            patch.object(quota, "_read_json_file", side_effect=AssertionError("binary inspection read file")),
        ):
            self.assertEqual(work_units._host_agent_binary("fix", "claude"), "claude")

    def test_normal_quota_read_refuses_shadow_before_usage_request(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))
        blob = {"claudeAiOauth": {"accessToken": "synthetic-agent", "expiresAt": (time.time() + 3600) * 1000}}
        with (
            patch.object(quota, "claude_usage_configuration_problem", return_value=None),
            patch.object(quota, "_read_json_file", return_value=blob),
            patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)),
            patch.object(quota, "_http_get_json", side_effect=AssertionError("usage read before route refusal")),
        ):
            provider = quota.Quota(cfg).claude()
        self.assertFalse(provider.available)
        self.assertIn("shadows", provider.error)

    def test_default_service_is_explicit_and_unhashed(self):
        with patch.object(quota.subprocess, "run", return_value=types.SimpleNamespace(returncode=44)) as run:
            self.assertIsNone(quota.claude_file_keychain_problem(None))
        self.assertEqual(
            run.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials"]
        )

    def test_mac_file_cannot_rely_on_renewal(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))
        blob = {"claudeAiOauth": {"accessToken": "synthetic-agent", "expiresAt": 1, "refreshToken": "not-owned-here"}}
        with patch.object(quota, "_read_json_file", return_value=blob):
            self.assertIn("expired", quota.Quota(cfg)._claude_launch_credential_unusable())

    def test_mac_file_identity_mismatch_is_not_silently_an_operator_assertion(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))

        def read(path):
            name = "usage-account" if Path(path) == Path("/tmp/usage.json") else "agent-account"
            return {"claudeAiOauth": {"accessToken": name, "account": {"uuid": name}}}

        with patch.object(quota, "_read_json_file", side_effect=read):
            self.assertIn("different accounts", quota.claude_usage_configuration_problem(cfg))

    def test_host_environment_token_does_not_use_file_or_keychain(self):
        cfg = types.SimpleNamespace(home=Path("/tmp/home"), quota_cache=Path("/tmp/cache"))
        with (
            patch.dict(os.environ, TAUCETI_CLAUDE_AGENT_CREDENTIAL="environment", CLAUDE_CODE_OAUTH_TOKEN="host-only"),
            patch.object(quota, "_read_json_file", side_effect=AssertionError("unused file read")),
            patch.object(quota.subprocess, "run", side_effect=AssertionError("unused Keychain read")),
        ):
            self.assertIsNone(quota.Quota(cfg)._claude_launch_credential_unusable())


if __name__ == "__main__":
    unittest.main()

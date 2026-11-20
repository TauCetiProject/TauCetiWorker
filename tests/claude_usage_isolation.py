"""Separate usage credentials retain their path, identity and ownership across worker isolation."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, quota
from tauceti_worker.config import Config

USAGE_VAR = "TAUCETI_CLAUDE_USAGE_CREDENTIALS"


def credential(path, token="agent-token", *, expires=3600, refresh=None, account=None, email=None):
    oauth = {"accessToken": token, "expiresAt": (time.time() + expires) * 1000}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if account is not None:
        oauth["accountUuid"] = account
    if email is not None:
        oauth["emailAddress"] = email
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


class Isolation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="usage-isolation-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "operator"
        self.home.mkdir()
        self.agent = self.home / ".claude/.credentials.json"
        self.usage = self.root / "usage/login.json"
        credential(self.agent)
        credential(self.usage, "usage-token", refresh="usage-refresh")
        self.cfg = SimpleNamespace(home=self.home)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), USAGE_VAR: str(self.usage)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_relative_and_tilde_paths_are_pinned_before_home_changes(self):
        usage = self.home / "usage/.credentials.json"
        credential(usage)
        os.environ[USAGE_VAR] = "~/usage"
        self.assertEqual(quota.normalize_claude_usage_credentials(), usage)
        isolated = self.root / "isolated"
        os.environ.update(HOME=str(isolated), TAUCETI_DATA_HOME=str(isolated))
        self.assertEqual(quota.claude_usage_credentials(), usage)
        with patch("os.getcwd", return_value=str(self.root)):
            os.environ[USAGE_VAR] = "usage/login.json"
            self.assertEqual(quota.normalize_claude_usage_credentials(), self.usage)

    def test_already_isolated_old_loop_normalizes_before_early_return(self):
        isolated = self.root / "isolated"
        os.environ.update(HOME=str(isolated), TAUCETI_DATA_HOME=str(isolated))
        os.environ[USAGE_VAR] = "~/usage"
        credential(self.home / "usage/.credentials.json")
        with (
            patch("tauceti_worker.paths.login_home", return_value=self.home),
            patch.object(agents, "_worker_iso_home", return_value=isolated),
            patch.object(agents, "share_build_caches"),
            patch.object(agents, "seed_worker_claude_config"),
        ):
            agents.isolate_home("worker1")
        self.assertEqual(os.environ[USAGE_VAR], str(self.home / "usage/.credentials.json"))

    def test_fresh_isolation_preserves_operator_path_and_source_marker(self):
        credential(self.home / "usage/.credentials.json")
        os.environ[USAGE_VAR] = "~/usage"
        isolated = self.root / "isolated"
        with (
            patch.object(agents, "_worker_iso_home", return_value=isolated),
            patch.object(agents, "share_build_caches"),
        ):
            agents.isolate_home("worker1")
        self.assertEqual(quota.claude_usage_credentials(), self.home / "usage/.credentials.json")
        self.assertEqual(quota.claude_agent_credentials(self.cfg)[-1], self.agent)

    def test_exact_symlink_hardlink_and_source_marker_aliases_are_rejected_before_rotation(self):
        alias = self.root / "alias.json"
        linked = self.root / "linked.json"
        alias.symlink_to(self.agent)
        os.link(self.agent, linked)
        for path in (self.agent, alias, linked):
            with self.subTest(path=path):
                os.environ[USAGE_VAR] = str(path)
                self.assertIn("separate file", quota.claude_usage_configuration_problem(self.cfg))
        isolated = self.root / "isolated/.claude"
        credential(isolated / ".credentials.json")
        (isolated / ".tauceti-creds-source").write_text(str(self.agent.parent))
        os.environ["CLAUDE_CONFIG_DIR"] = str(isolated)
        os.environ[USAGE_VAR] = str(self.agent)
        self.assertIn("separate file", quota.claude_usage_configuration_problem(self.cfg))
        reader = quota.Quota(self.config())
        with (
            patch.object(quota.credential_refresh, "refresh_if_due") as rotate,
            patch.object(reader, "_fetch_usage") as fetch,
        ):
            os.environ["TAUCETI_AUTO_REFRESH"] = "1"
            result = reader.claude(renew=True)
            self.assertFalse(result.available)
            self.assertFalse(reader._refresh_claude_credential(force=True))
        rotate.assert_not_called()
        fetch.assert_not_called()

    def config(self):
        root = self.root / "worker"
        return Config("worker", self.home, root, *(root / n for n in ("state", "co", "store", "sb", "logs", "q")))

    def test_comparable_identity_mismatch_is_refused_without_exposing_values(self):
        credential(self.agent, account="agent-account", email="agent@example.invalid")
        credential(self.usage, "usage-token", account="usage-account", email="usage@example.invalid")
        problem = quota.claude_usage_configuration_problem(self.cfg)
        self.assertEqual(problem, "Claude usage login and agent credential identify different accounts")
        self.assertFalse(quota.claude_usage_identity_verified(self.cfg))
        credential(self.usage, "usage-token", account="agent-account", email="AGENT@example.invalid")
        self.assertIsNone(quota.claude_usage_configuration_problem(self.cfg))
        self.assertTrue(quota.claude_usage_identity_verified(self.cfg))
        credential(self.usage, "usage-token", account="agent-account", email="different@example.invalid")
        self.assertIsNotNone(quota.claude_usage_configuration_problem(self.cfg))
        self.assertFalse(quota.claude_usage_identity_verified(self.cfg))

    def test_opaque_environment_tokens_remain_an_operator_identity_assertion(self):
        credential(self.agent, account="agent-account")
        credential(self.usage, "usage-token", account="another-account")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "opaque-setup-token"
        self.assertIsNone(quota.claude_usage_configuration_problem(self.cfg))
        self.assertFalse(quota.claude_usage_identity_verified(self.cfg))
        os.environ["TAUCETI_CLAUDE_AGENT_CREDENTIAL"] = "file"
        self.assertIsNotNone(quota.claude_usage_configuration_problem(self.cfg))

    def test_unidentified_setup_token_is_not_called_verified(self):
        self.assertIsNone(quota.claude_usage_configuration_problem(self.cfg))
        self.assertFalse(quota.claude_usage_identity_verified(self.cfg))

    def test_route_matrix_keeps_copied_credentials_live_before_launch(self):
        for route in ("environment", "file", "access-only"):
            for expired in (False, True):
                for refresh in (None, "agent-refresh"):
                    with self.subTest(route=route, expired=expired, refresh=bool(refresh)):
                        credential(self.agent, expires=-60 if expired else 3600, refresh=refresh)
                        reader = quota.Quota(self.config())
                        with (
                            patch.dict(os.environ, {"TAUCETI_CLAUDE_AGENT_CREDENTIAL": route}),
                            patch.object(sys, "platform", "linux"),
                        ):
                            problem = reader._claude_launch_credential_unusable()
                        expected = expired and not (route == "environment" and refresh)
                        self.assertEqual(bool(problem), bool(expected))
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "opaque-environment-token"
        self.agent.unlink()
        reader = quota.Quota(self.config())
        with patch.object(sys, "platform", "linux"):
            self.assertIsNone(reader._claude_launch_credential_unusable())
            with patch.dict(os.environ, {"TAUCETI_CLAUDE_AGENT_CREDENTIAL": "file"}):
                self.assertIn("no ", reader._claude_launch_credential_unusable())

    def test_mac_expired_file_is_blocked_without_a_keychain_password_read(self):
        credential(self.agent, expires=-60)
        reader = quota.Quota(self.config())
        with patch.object(sys, "platform", "darwin"), patch.object(quota, "_claude_keychain_creds") as keychain:
            self.assertIn("expired", reader._claude_launch_credential_unusable())
            self.assertIsNone(quota.claude_usage_configuration_problem(self.cfg))
        keychain.assert_not_called()

    def test_usage_path_is_not_forwarded_to_host_agents(self):
        _, env = agents.host_agent_argv("test", "claude")
        self.assertNotIn(USAGE_VAR, env)
        self.assertNotIn("TAUCETI_CLAUDE_USAGE_OPERATOR_HOME", env)
        self.assertEqual(os.environ[USAGE_VAR], str(self.usage))

    def test_inspection_scope_and_work_scope_are_reported_honestly(self):
        reader = quota.Quota(self.config())
        expected = quota.Provider("claude", True, "opus")
        with patch.object(reader, "_claude_read", return_value=expected):
            inspected = reader.claude()
            self.assertIn("inspection", inspected.credential_scope)
            self.assertIn("work starts", quota.quota_line({"claude": inspected}, markup=False))
            with patch.dict(os.environ, {"TAUCETI_CLAUDE_AGENT_CREDENTIAL": "file"}):
                working = reader.claude()
                self.assertEqual(working.credential_scope, "work credential route: file")
        os.environ.pop(USAGE_VAR)
        with patch.object(reader, "_claude_read", return_value=expected):
            self.assertIsNone(reader.claude().credential_scope)

    def test_agent_file_helper_checks_missing_malformed_and_expired_files(self):
        self.agent.unlink()
        self.assertIn("no ", quota.claude_file_credential_problem(self.agent, renewable=False))
        credential(self.agent)
        value = json.loads(self.agent.read_text())
        value["claudeAiOauth"]["expiresAt"] = "invalid"
        self.agent.write_text(json.dumps(value))
        self.assertIn("invalid expiry", quota.claude_file_credential_problem(self.agent, renewable=False))
        credential(self.agent, expires=-60, refresh="refresh")
        self.assertIsNone(quota.claude_file_credential_problem(self.agent, renewable=True))
        self.assertIn("expired", quota.claude_file_credential_problem(self.agent, renewable=False))


if __name__ == "__main__":
    unittest.main()

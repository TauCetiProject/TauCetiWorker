"""Separate usage credentials retain their path, identity and ownership across worker isolation."""

import json
import os
import runpy
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, audit_pool, quota, security_audit, usage, work_units
from tauceti_worker.config import Config, NoProgress

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

    def test_agent_directory_normalizes_for_default_workers_only_with_usage_enabled(self):
        for configured, expected in (("~/agent", self.home / "agent"), ("agent", self.root / "agent")):
            os.environ["CLAUDE_CONFIG_DIR"] = configured
            with patch("os.getcwd", return_value=str(self.root)):
                quota.normalize_claude_usage_credentials()
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], str(expected))
        os.environ.pop(USAGE_VAR)
        os.environ["CLAUDE_CONFIG_DIR"] = "~/unchanged"
        self.assertIsNone(quota.normalize_claude_usage_credentials())
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "~/unchanged")

    def test_identity_is_checked_after_mirroring_an_account_switch(self):
        original = self.root / "agent-source/.credentials.json"
        credential(original, "new-agent-token", account="new-account")
        (self.agent.parent / ".tauceti-creds-source").write_text(str(original.parent))
        credential(self.agent, "old-agent-token", account="old-account")
        credential(self.usage, "usage-token", account="old-account")
        reader = quota.Quota(self.config())
        with patch.object(reader, "_fetch_usage") as fetch:
            result = reader.claude()
        self.assertIn("different accounts", result.error)
        fetch.assert_not_called()
        credential(self.agent, "old-agent-token", account="old-account")
        credential(self.usage, "usage-token", account="new-account")
        with patch.object(reader, "_claude_creds", return_value=(None, False)) as read:
            result = reader.claude()
        read.assert_called_once()  # the stale mirror did not falsely reject the new matching login
        self.assertNotIn("different accounts", result.error)
        self.assertEqual(json.loads(self.agent.read_text())["claudeAiOauth"]["accountUuid"], "new-account")

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

    def test_copied_agent_tokens_are_not_a_separate_usage_login(self):
        for key in ("accessToken", "refreshToken"):
            credential(self.agent, refresh="agent-refresh")
            credential(self.usage, "usage-token", refresh="usage-refresh")
            document = json.loads(self.usage.read_text())
            document["claudeAiOauth"][key] = json.loads(self.agent.read_text())["claudeAiOauth"][key]
            self.usage.write_text(json.dumps(document))
            self.assertIn("not a copy", quota.claude_usage_configuration_problem(self.cfg))
        credential(self.usage, "usage-token", refresh="usage-refresh")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "usage-token"
        self.assertIn("environment token", quota.claude_usage_configuration_problem(self.cfg))
        os.environ["TAUCETI_CLAUDE_AGENT_CREDENTIAL"] = "file"
        self.assertIn("environment token", quota.claude_usage_configuration_problem(self.cfg))

    def test_usage_symlink_is_pinned_to_its_real_filename(self):
        link = self.root / "usage-link.json"
        link.symlink_to(self.usage)
        os.environ[USAGE_VAR] = str(link)
        self.assertEqual(quota.normalize_claude_usage_credentials(), self.usage)

    def test_review_engine_and_probe_environments_do_not_receive_usage_paths(self):
        os.environ["TAUCETI_CLAUDE_USAGE_OPERATOR_HOME"] = str(self.home)
        with patch.object(agents, "run_process", return_value=SimpleNamespace(returncode=0)) as run:
            agents._codex_model_probe(SimpleNamespace(state=self.root / "probe"), "model")
        self.assertNotIn(USAGE_VAR, run.call_args.kwargs["env"])
        with (
            patch.object(agents, "run_process", return_value=SimpleNamespace(returncode=0)) as run,
            patch("tauceti_worker.fleet_capacity.check_launch_admission"),
            patch("tauceti_worker.claude_api.review_environment", return_value=None),
            patch.dict(os.environ, {"TAUCETI_STREAM": "1"}),
        ):
            agents.run_to_logfile(["engine"], self.root / "review.log", "review #1")
        self.assertNotIn(USAGE_VAR, run.call_args.kwargs["env"])
        self.assertNotIn("TAUCETI_CLAUDE_USAGE_OPERATOR_HOME", run.call_args.kwargs["env"])

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

    def test_mac_agent_behavior_stays_with_keychain_until_the_separate_mac_route_change(self):
        credential(self.agent, expires=-60)
        reader = quota.Quota(self.config())
        with patch.object(sys, "platform", "darwin"), patch.object(quota, "_claude_keychain_creds") as keychain:
            self.assertIsNone(reader._claude_launch_credential_unusable())
            self.assertIsNone(quota.claude_usage_configuration_problem(self.cfg))
            with patch.dict(os.environ, {"TAUCETI_CLAUDE_AGENT_CREDENTIAL": "file"}):
                self.assertIn("#682", reader._claude_launch_credential_unusable())
        keychain.assert_not_called()

    def pool_cli(self, inspect):
        cfg = self.config()
        with (
            patch.object(Config, "resolve", return_value=cfg),
            patch.object(audit_pool.Pool, "serve", side_effect=inspect) as serve,
            patch("tauceti_worker.paths.ensure_ssl_cert_file"),
            patch.object(sys, "argv", ["security_audit", "pool", "--state", str(cfg.state / security_audit.CURSOR)]),
            warnings.catch_warnings(),
        ):
            warnings.filterwarnings(
                "ignore", message="'tauceti_worker.security_audit' found in sys.modules", category=RuntimeWarning
            )
            runpy.run_module("tauceti_worker.security_audit", run_name="__main__")
        serve.assert_called_once()

    def test_standalone_pool_checks_the_access_only_route_before_admission(self):
        for platform, exists in (("linux", True), ("linux", False), ("darwin", False)):
            with self.subTest(platform=platform, file_exists=exists):
                os.environ.pop("TAUCETI_CLAUDE_AGENT_CREDENTIAL", None)
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "parent-setup-token"
                credential(self.usage, "usage-token")
                credential(self.agent, expires=-60, refresh="agent-refresh")
                if not exists:
                    self.agent.unlink()

                def inspect(expected="#682" if platform == "darwin" else "expired" if exists else "no "):
                    self.assertEqual(os.environ["TAUCETI_CLAUDE_AGENT_CREDENTIAL"], "access-only")
                    problem = quota.Quota(self.config())._claude_launch_credential_unusable()
                    self.assertIsNotNone(problem)
                    self.assertIn(expected, problem)

                with patch.object(sys, "platform", platform), patch.object(quota, "_claude_keychain_creds") as keychain:
                    self.pool_cli(inspect)
                keychain.assert_not_called()

    def test_standalone_pool_copies_the_live_agent_file_not_the_parent_token(self):
        credential(self.agent, "agent-token", refresh="agent-refresh")
        credential(self.usage, "usage-token", refresh="usage-refresh")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "parent-setup-token"
        before = self.usage.read_bytes()

        def inspect():
            self.assertIsNone(quota.Quota(self.config())._claude_launch_credential_unusable())
            home = self.root / "pool-reviewer"
            env = security_audit.client_env(home, "claude", "subscription")
            blob = json.loads((home / ".claude/.credentials.json").read_text())["claudeAiOauth"]
            self.assertEqual(blob["accessToken"], "agent-token")
            self.assertNotIn("refreshToken", blob)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn(USAGE_VAR, env)
            self.assertEqual(self.usage.read_bytes(), before)

        with patch.object(sys, "platform", "linux"), patch.object(quota, "_claude_keychain_creds") as keychain:
            self.pool_cli(inspect)
        keychain.assert_not_called()

    def test_unsupported_review_file_flag_does_not_charge_the_pr(self):
        values = {"review-err-1": 2}
        w = SimpleNamespace(
            cfg=self.config(),
            gh=SimpleNamespace(repo="org/project"),
            rs=SimpleNamespace(review_rounds=lambda *_: 0),
            counters=SimpleNamespace(incr=lambda key: values.update({key: values.get(key, 0) + 1})),
        )
        candidate = SimpleNamespace(pr=1, head="a" * 40, contest=None)
        with (
            patch(
                "tauceti_worker.review_runtime.resolve", return_value=SimpleNamespace(command=lambda: ["old-engine"])
            ),
            patch.object(work_units, "run_to_logfile", return_value=2),
            patch.object(work_units, "record_review_failure") as record,
        ):
            with self.assertRaisesRegex(NoProgress, "not charged to the PR"):
                work_units.do_review(w, None, candidate, SimpleNamespace(work_model="claude"), False)
        self.assertEqual(values["review-err-1"], 2)
        record.assert_not_called()

    def test_strict_review_file_flag_targets_only_claude_subscriptions(self):
        os.environ["TAUCETI_REVIEW_AUTH"] = "api"
        for models in ("claude", "sonnet", "claude,codex"):
            self.assertEqual(
                agents.claude_review_file_flags(models), ["--auth", "subscription", "--require-claude-config-file"]
            )
        for models in ("codex", "kiro", "codex,kiro"):
            self.assertEqual(agents.claude_review_file_flags(models), [])
        os.environ["TAUCETI_CLAUDE_BILLING"] = "api"
        self.assertEqual(agents.claude_review_file_flags("claude"), [])

    def test_usage_path_is_not_forwarded_to_host_agents(self):
        _, env = agents.host_agent_argv("test", "claude")
        self.assertNotIn(USAGE_VAR, env)
        self.assertNotIn("TAUCETI_CLAUDE_USAGE_OPERATOR_HOME", env)
        self.assertEqual(os.environ[USAGE_VAR], str(self.usage))
        self.assertNotIn(USAGE_VAR, usage.kiro_process_env())

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
        for expiry in (None, True, float("nan"), float("inf")):
            value["claudeAiOauth"]["expiresAt"] = expiry
            self.agent.write_text(json.dumps(value))
            self.assertIn("invalid expiry", quota.claude_file_credential_problem(self.agent, renewable=False))
        value["claudeAiOauth"].pop("expiresAt")
        self.agent.write_text(json.dumps(value))
        self.assertIsNone(quota.claude_file_credential_problem(self.agent, renewable=False))
        credential(self.agent, expires=-60, refresh="refresh")
        self.assertIsNone(quota.claude_file_credential_problem(self.agent, renewable=True))
        self.assertIn("expired", quota.claude_file_credential_problem(self.agent, renewable=False))


if __name__ == "__main__":
    unittest.main()

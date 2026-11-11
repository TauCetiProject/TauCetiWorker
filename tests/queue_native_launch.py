#!/usr/bin/env python3
"""Native launches use explicit private snapshots without credential discovery."""

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from tauceti_queue import __main__ as cli
from tauceti_queue import config
from tauceti_queue.github_app import GitHubApp
from tauceti_queue.native_launch import SCHEMA, NativeLaunch, NativeLaunchError
from tauceti_queue.source_authority import QueueControlPolicy, control_token_fingerprint

CONFIG = b"""schema_version=2
[forges.source]
kind="forgejo"
url="https://source.example"
[repositories]
content={forge="source",name="team/Math"}
roadmap="team/Roadmap"
[ci]
backend="github-actions"
repository="team/Execution"
[review]
rubrics=["proof"]
"""


@unittest.skipUnless(os.name == "posix", "native launch uses checked POSIX descriptors")
class LaunchFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-launch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "execution"
        self.app.mkdir(mode=0o700)
        self.write(self.app / "github-app.json", json.dumps(dict(id=1, installation_id=2, slug="fixture")))
        self.write(
            self.app / "github-private-key.pem",
            rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ),
        )
        forge = Forge("forgejo", "https://source.example")
        self.source = cc.RepositoryIdentity(RepositoryRef(forge, "team/Math"), 41)
        execution = cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Execution"), 77)
        harness = cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Tools"), 78)
        self.policy = cc.ExecutionPolicy(
            self.source,
            execution,
            forge,
            ".github/workflows/pr-build.yml",
            harness,
            "c" * 40,
            "main",
            "main",
            True,
            hashlib.sha256(CONFIG).hexdigest(),
        )
        self.project = self.root / "project.toml"
        self.write(self.project, CONFIG)
        self.runtime = self.root / "queue.toml"
        self.runtime_data = "\n".join(
            f"{key}={json.dumps(value)}"
            for key, value in dict(
                repository="team/Math",
                canonical_branch="main",
                content_clone=str(self.root / "content"),
                database=str(self.root / "queue.sqlite3"),
                credentials_dir=str(self.app),
                log_dir=str(self.root / "logs"),
                listen_host="127.0.0.1",
                listen_port=23456,
                public_url="http://127.0.0.1:23456",
                admission_source="policy",
                admission_rubrics="proof",
                admission_lean_root="Math",
            ).items()
        ).encode()
        self.write(self.runtime, self.runtime_data)
        self.control = self.root / "controls.json"
        controls = QueueControlPolicy(
            self.source, frozenset({control_token_fingerprint(self.source, "operator-control")})
        )
        self.write(self.control, json.dumps(controls.record()))
        for name, value in (
            ("api-token", "controller-source-api"),
            ("git-token", "controller-source-git"),
            ("webhook-secret", "x" * 32),
        ):
            self.write(self.root / name, value)
        self.row = dict(
            schema=SCHEMA,
            queue_configuration=str(self.runtime),
            queue_configuration_sha256=hashlib.sha256(self.runtime_data).hexdigest(),
            project_configuration=str(self.project),
            project_configuration_sha256=hashlib.sha256(CONFIG).hexdigest(),
            execution_policy=self.policy.record(with_config=False),
            workflow_revision="b" * 40,
            publishers=dict(generation="1" * 32, reviews=[8], statuses=[7], recoveries=[7]),
            control_policy_file=str(self.control),
            source_credentials=dict(
                origin=dict(kind="forgejo", url=forge.url),
                api_token_file=str(self.root / "api-token"),
                git_token_file=str(self.root / "git-token"),
            ),
            source_webhook_secret_file=str(self.root / "webhook-secret"),
        )
        self.path = self.root / "launch.json"
        self.save()

    def write(self, path, value):
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        path.chmod(0o600)

    def save(self, row=None):
        self.write(self.path, json.dumps(self.row if row is None else row))

    def test_round_trip_uses_source_bound_credentials_and_repository_scoped_execution(self):
        with (
            patch.object(GitHubApp, "load", side_effect=AssertionError("implicit credentials")),
            patch.dict(
                os.environ,
                GH_TOKEN="wrong-github-token",
                GH_HOST="other.example",
                TAUCETI_QUEUE_REPOSITORY="other/source",
                TAUCETI_QUEUE_PUBLICATION_ENABLED="true",
                TAUCETI_QUEUE_CONFIG="/missing/ambient-credentials",
            ),
        ):
            launch = NativeLaunch.load(self.path)
        self.assertEqual(launch.config.repository, "team/Math")
        self.assertFalse(launch.config.publication_enabled)
        self.assertEqual(launch.bootstrap.execution_policy, self.policy)
        self.assertEqual(launch.bootstrap.source_client._token, "controller-source-api")
        self.assertEqual(launch.bootstrap.source_git_token, "controller-source-git")
        self.assertEqual(launch.bootstrap.execution_tokens.repository_id, 77)
        self.assertEqual(launch.bootstrap.execution_tokens.permissions, {"contents": "write", "actions": "write"})
        self.assertEqual(config.validate(launch.config, check_credentials=False), [])
        for value in ("controller-source-api", "controller-source-git", "operator-control", "x" * 32):
            self.assertNotIn(value, repr(launch))
            self.assertNotIn(value, json.dumps(launch.summary()))

    def test_snapshots_must_match_and_missing_critical_runtime_fields_have_no_defaults(self):
        for name in ("project_configuration_sha256", "queue_configuration_sha256"):
            row = copy.deepcopy(self.row)
            row[name] = "a" * 64
            self.save(row)
            with self.subTest(name=name), self.assertRaises(NativeLaunchError):
                NativeLaunch.load(self.path)
        self.save()
        self.write(self.project, CONFIG.replace(b"team/Math", b"other/Source"))
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(self.path)
        self.write(self.project, CONFIG)
        reduced = b"\n".join(line for line in self.runtime_data.splitlines() if not line.startswith(b"database="))
        self.write(self.runtime, reduced)
        row = dict(self.row, queue_configuration_sha256=hashlib.sha256(reduced).hexdigest())
        self.save(row)
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(self.path)

    def test_origin_mismatch_is_rejected_before_source_credential_files_are_opened(self):
        row = copy.deepcopy(self.row)
        row["source_credentials"]["origin"]["url"] = "https://other.example"
        self.save(row)
        with patch("tauceti_queue.native_launch._token") as read:
            with self.assertRaises(NativeLaunchError):
                NativeLaunch.load(self.path)
            read.assert_not_called()

    def test_symlinks_public_files_and_duplicate_publishers_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(self.path)
        self.path.chmod(0o600)
        link = self.root / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(link)
        row = copy.deepcopy(self.row)
        row["publishers"]["reviews"] = [8, 8]
        self.save(row)
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(self.path)
        self.save()
        (self.root / "api-token").chmod(0o644)
        with self.assertRaises(NativeLaunchError):
            NativeLaunch.load(self.path)

    def test_errors_and_check_config_do_not_expose_private_material(self):
        self.write(self.path, b'{"private-bearer-never-log": invalid json')
        with self.assertRaises(NativeLaunchError) as caught:
            NativeLaunch.load(self.path)
        self.assertNotIn("private-bearer-never-log", str(caught.exception))
        self.save()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["check-config", "--native-launch", str(self.path), "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["native"]["source"], self.source.record())
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["webhook_url"], "http://127.0.0.1:23456/webhook/forgejo")
        self.assertNotIn("review_app_id", report)
        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main(["check-config", "--native-launch", str(self.path), "--print", "webhook_url"]), 0)
        self.assertEqual(printed.getvalue().strip(), report["webhook_url"])
        with redirect_stdout(io.StringIO()) as text:
            self.assertEqual(cli.main(["check-config", "--native-launch", str(self.path)]), 0)
        self.assertNotIn("/webhook/github", text.getvalue())
        self.assertNotIn("controller-source-api", output.getvalue())
        self.assertNotIn(control_token_fingerprint(self.source, "operator-control"), output.getvalue())

    def test_invalid_execution_key_and_missing_project_rubrics_are_rejected_locally(self):
        self.write(
            self.app / "github-private-key.pem", "-----BEGIN PRIVATE KEY-----\ninvalid\n-----END PRIVATE KEY-----"
        )
        with self.assertRaisesRegex(NativeLaunchError, "execution credentials"):
            NativeLaunch.load(self.path)
        project = CONFIG.replace(b'rubrics=["proof"]', b"")
        self.write(self.project, project)
        row = dict(self.row, project_configuration_sha256=hashlib.sha256(project).hexdigest())
        self.save(row)
        with self.assertRaisesRegex(NativeLaunchError, "source/execution policy"):
            NativeLaunch.load(self.path)

    def test_native_selection_is_explicit_and_mutually_exclusive(self):
        for command in ("serve", "check-config"):
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args([command, "--config", "queue.toml", "--native-launch", str(self.path)])
            self.assertEqual(caught.exception.code, 2)

    def test_controller_credentials_cannot_be_enrolled_as_controls(self):
        for token in ("controller-source-api", "controller-source-git"):
            control = QueueControlPolicy(self.source, frozenset({control_token_fingerprint(self.source, token)}))
            self.write(self.control, json.dumps(control.record()))
            with self.assertRaisesRegex(NativeLaunchError, "source credentials"):
                NativeLaunch.load(self.path)

    def test_checked_runtime_bytes_are_not_reopened_and_environment_is_explicit(self):
        self.runtime.unlink()
        loaded = config.load(self.runtime, environ={}, snapshot=self.runtime_data)
        self.assertEqual(loaded.repository, "team/Math")
        self.assertEqual(loaded.path, self.runtime)
        with self.assertRaises(config.ConfigError):
            config.load(snapshot=self.runtime_data)
        with self.assertRaises(config.ConfigError):
            config.load("", snapshot=self.runtime_data)

    def test_serve_passes_the_explicit_bootstrap_and_cleans_up_failed_startup(self):
        with patch.object(cli, "Service") as service_class, patch.object(cli, "_install_signal_handlers"):
            service = service_class.return_value
            service.build_controller.side_effect = RuntimeError("private-bearer-never-log")
            with patch.object(cli, "_log") as log:
                self.assertEqual(cli.main(["serve", "--native-launch", str(self.path)]), 1)
            self.assertEqual(service_class.call_args.kwargs["native"].execution_policy.source, self.source)
            service.shutdown.assert_called_once()
            self.assertNotIn("private-bearer-never-log", str(log.call_args_list))


if __name__ == "__main__":
    unittest.main()

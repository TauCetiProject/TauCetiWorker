#!/usr/bin/env python3
"""Configuration of the queue service (tauceti_queue.config): layers, types, validation, discovery.

Covered: defaults with no file; file values and their recorded sources; the [settings] table;
unknown keys and wrong types as errors; environment overrides winning over the file; boolean and
path coercion; the host name default; validation of the repository, port, paths and credential
files (presence and mode); canonical branch discovery parsing `git ls-remote --symref`; and the
`check-config` rendering.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import config as cfg  # noqa: E402


def write_credentials(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("github-app.json", '{"id": 5607007, "slug": "tauceti-queue"}'),
        ("github-private-key.pem", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"),
        ("github-webhook-secret", "secret\n"),
    ):
        path = directory / name
        path.write_text(text)
        path.chmod(0o600)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.creds = self.home / ".config/tauceti-queue"
        write_credentials(self.creds)

    def write_config(self, text: str, name: str = "config.toml") -> Path:
        path = self.home / name
        path.write_text(text)
        return path


class Layers(Base):
    def test_defaults_without_a_file(self):
        config = cfg.load(environ={})
        self.assertIsNone(config.path)
        self.assertEqual(config.repository, "TauCetiProject/TauCeti")
        self.assertEqual(config.canonical_branch, "")
        self.assertFalse(config.publication_enabled)
        self.assertEqual(config.mode, "shadow")
        self.assertEqual(config.listen_url, "http://127.0.0.1:8780")
        self.assertEqual(config.webhook_url, "https://queue.taucetiproject.org/webhook/github")
        self.assertEqual(
            config.settings.as_dict(),
            {
                "insertion_age_window": 16,
                "repair_concurrency": 1,
                "stall_interval_seconds": 900,
                "approval_poll_interval_seconds": 900,
            },
        )
        self.assertEqual(config.database, self.home / ".local/state/tauceti-queue/queue.sqlite3")
        self.assertEqual(config.credentials_dir, self.creds)
        self.assertTrue(config.host)
        self.assertEqual(config.sources["host"], "hostname")
        self.assertEqual(config.sources["repository"], "default")

    def test_default_file_location_is_read_when_present(self):
        (self.home / ".config/tauceti-queue/config.toml").write_text('listen_port = 9001\nhost = "box"\n')
        config = cfg.load(environ={})
        self.assertEqual(config.listen_port, 9001)
        self.assertEqual(config.host, "box")
        self.assertEqual(config.sources["listen_port"], "file")
        self.assertEqual(config.sources["host"], "file")

    def test_file_values_and_settings_table(self):
        path = self.write_config(
            'repository = "TauCetiProject/TauCetiQueueScratch"\n'
            'canonical_branch = "trunk"\n'
            "publication_enabled = true\n"
            'database = "~/db/queue.sqlite3"\n'
            "[settings]\n"
            "insertion_age_window = 8\n"
            "stall_interval_seconds = 1200\n"
        )
        config = cfg.load(path, environ={})
        self.assertEqual(config.path, path)
        self.assertEqual(config.repository, "TauCetiProject/TauCetiQueueScratch")
        self.assertEqual(config.canonical_branch, "trunk")
        self.assertTrue(config.publication_enabled)
        self.assertEqual(config.mode, "production")
        self.assertEqual(config.database, self.home / "db/queue.sqlite3")
        self.assertEqual(config.settings.insertion_age_window, 8)
        self.assertEqual(config.settings.repair_concurrency, 1)
        self.assertEqual(config.settings.stall_interval_seconds, 1200)
        self.assertEqual(config.sources["insertion_age_window"], "file")
        self.assertEqual(config.sources["repair_concurrency"], "default")

    def test_config_path_from_environment(self):
        path = self.write_config("listen_port = 9100\n", "elsewhere.toml")
        config = cfg.load(environ={"TAUCETI_QUEUE_CONFIG": str(path)})
        self.assertEqual(config.listen_port, 9100)
        with self.assertRaises(cfg.ConfigError):
            cfg.load(environ={"TAUCETI_QUEUE_CONFIG": str(self.home / "missing.toml")})
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.home / "missing.toml", environ={})

    def test_environment_overrides_win(self):
        path = self.write_config(
            "listen_port = 9001\npublication_enabled = false\n[settings]\ninsertion_age_window = 8\n"
        )
        config = cfg.load(
            path,
            environ={
                "TAUCETI_QUEUE_LISTEN_PORT": "9002",
                "TAUCETI_QUEUE_PUBLICATION_ENABLED": "true",
                "TAUCETI_QUEUE_INSERTION_AGE_WINDOW": "4",
                "TAUCETI_QUEUE_LOG_DIR": "~/logs",
                "TAUCETI_QUEUE_HOST": "override",
            },
        )
        self.assertEqual(config.listen_port, 9002)
        self.assertTrue(config.publication_enabled)
        self.assertEqual(config.settings.insertion_age_window, 4)
        self.assertEqual(config.log_dir, self.home / "logs")
        self.assertEqual(config.host, "override")
        self.assertEqual(config.sources["listen_port"], "env:TAUCETI_QUEUE_LISTEN_PORT")
        self.assertEqual(config.sources["insertion_age_window"], "env:TAUCETI_QUEUE_INSERTION_AGE_WINDOW")

    def test_boolean_spellings(self):
        for text, expected in (("1", True), ("yes", True), ("On", True), ("0", False), ("no", False), ("", False)):
            config = cfg.load(environ={"TAUCETI_QUEUE_PUBLICATION_ENABLED": text})
            self.assertEqual(config.publication_enabled, expected, text)
        with self.assertRaises(cfg.ConfigError):
            cfg.load(environ={"TAUCETI_QUEUE_PUBLICATION_ENABLED": "maybe"})

    def test_bad_values_are_errors(self):
        with self.assertRaisesRegex(cfg.ConfigError, "unknown key 'listen_prot'"):
            cfg.load(self.write_config("listen_prot = 1\n"), environ={})
        with self.assertRaisesRegex(cfg.ConfigError, "unknown setting 'cap'"):
            cfg.load(self.write_config("[settings]\ncap = 4\n"), environ={})
        with self.assertRaisesRegex(cfg.ConfigError, "listen_port: expected an integer"):
            cfg.load(self.write_config('listen_port = "eighty"\n'), environ={})
        with self.assertRaisesRegex(cfg.ConfigError, "publication_enabled: expected true or false"):
            cfg.load(self.write_config('publication_enabled = "soon"\n'), environ={})
        with self.assertRaisesRegex(cfg.ConfigError, "expected an integer"):
            cfg.load(environ={"TAUCETI_QUEUE_REPAIR_CONCURRENCY": "two"})
        with self.assertRaisesRegex(cfg.ConfigError, "config.toml"):
            cfg.load(self.write_config("= nonsense\n"), environ={})

    def test_as_dict_is_json_ready(self):
        import json

        config = cfg.load(environ={})
        document = json.loads(json.dumps(config.as_dict()))
        self.assertEqual(document["settings"]["insertion_age_window"], 16)
        self.assertEqual(document["listen_url"], "http://127.0.0.1:8780")
        self.assertEqual(document["sources"]["repository"], "default")
        self.assertIsNone(document["config_file"])


class Validation(Base):
    def test_good_configuration_has_no_problems(self):
        self.assertEqual(cfg.validate(cfg.load(environ={})), [])

    def test_problems(self):
        config = cfg.load(
            environ={
                "TAUCETI_QUEUE_REPOSITORY": "TauCeti",
                "TAUCETI_QUEUE_LISTEN_PORT": "70000",
                "TAUCETI_QUEUE_PUBLIC_URL": "queue.example",
                "TAUCETI_QUEUE_CANONICAL_BRANCH": "bad branch",
                "TAUCETI_QUEUE_STALL_INTERVAL_SECONDS": "0",
                "TAUCETI_QUEUE_RECONCILE_INTERVAL_SECONDS": "0",
                "TAUCETI_QUEUE_DATABASE": "relative/queue.sqlite3",
            }
        )
        problems = "\n".join(cfg.validate(config))
        for expected in (
            "repository must be owner/name",
            "listen_port must be between",
            "public_url must be an http(s) URL",
            "canonical_branch is not a branch name",
            "stall_interval_seconds must be at least 1",
            "reconcile_interval_seconds must be at least 1",
            "database must be an absolute path",
        ):
            self.assertIn(expected, problems)

    def test_credentials_are_checked(self):
        config = cfg.load(environ={"TAUCETI_QUEUE_CREDENTIALS_DIR": str(self.home / "nowhere")})
        self.assertIn("credentials_dir does not exist", "\n".join(cfg.validate(config)))
        (self.creds / "github-webhook-secret").unlink()
        (self.creds / "github-private-key.pem").chmod(0o644)
        (self.creds / "github-app.json").write_text('{"slug": "x"}')
        problems = "\n".join(cfg.validate(cfg.load(environ={})))
        self.assertIn("missing credential file", problems)
        self.assertIn("github-webhook-secret", problems)
        self.assertIn("readable by others", problems)
        self.assertIn("lacks the App id and slug", problems)


class Discovery(Base):
    def test_configured_branch_is_not_rediscovered(self):
        config = cfg.load(environ={"TAUCETI_QUEUE_CANONICAL_BRANCH": "trunk"})
        with mock.patch.object(cfg.subprocess, "run", side_effect=AssertionError("should not run git")):
            self.assertEqual(cfg.discover_canonical_branch(config), "trunk")

    def test_symref_is_parsed(self):
        config = cfg.load(environ={})
        completed = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout="ref: refs/heads/develop\tHEAD\n0123456789abcdef0123456789abcdef01234567\tHEAD\n",
            stderr="",
        )
        with mock.patch.object(cfg.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cfg.discover_canonical_branch(config), "develop")
        self.assertEqual(run.call_args.args[0][:3], ["git", "ls-remote", "--symref"])
        self.assertIn("https://github.com/TauCetiProject/TauCeti.git", run.call_args.args[0])

    def test_failures_are_config_errors(self):
        config = cfg.load(environ={})
        with mock.patch.object(
            cfg.subprocess, "run", side_effect=subprocess.CalledProcessError(128, "git", stderr="denied")
        ):
            with self.assertRaisesRegex(cfg.ConfigError, "cannot discover the canonical branch.*denied"):
                cfg.discover_canonical_branch(config)
        completed = subprocess.CompletedProcess(["git"], 0, stdout="0123\tHEAD\n", stderr="")
        with mock.patch.object(cfg.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(cfg.ConfigError, "no symbolic HEAD"):
                cfg.discover_canonical_branch(config)


class Rendering(Base):
    def test_effective_values_and_problems(self):
        config = cfg.load(self.write_config('host = "box"\n[settings]\nrepair_concurrency = 2\n'), environ={})
        text = cfg.format_effective(config, cfg.validate(config))
        self.assertIn("host", text)
        self.assertIn("box", text)
        self.assertIn("[file]", text)
        self.assertIn("[default]", text)
        self.assertIn("repair_concurrency", text)
        self.assertIn("github-private-key.pem", text)
        self.assertIn("present", text)
        self.assertIn("ok: no problems found", text)
        text = cfg.format_effective(config, ["listen_port must be between 1 and 65535, got 0"])
        self.assertIn("problems:", text)
        self.assertIn("  - listen_port must be between", text)


if __name__ == "__main__":
    unittest.main()

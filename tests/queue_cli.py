#!/usr/bin/env python3
"""The queue service entrypoint (tauceti_queue.__main__) and runtime (tauceti_queue.runtime).

Covered: revision detection from git, from a baked `_revision.py`, and outside any checkout; the
status document envelope and how a controller's status merges into it; the one-screen summary
rendered from the fixture status document; `check-config` output, `--print` and exit codes;
`status` with an injected fetch (summary, JSON, `--wait` retries, `--expect-revision` exit 2);
`settings show` and `settings set` through the admin endpoint with the operator token; and the
real process: `serve` in fallback mode answering /status, /healthz and /settings, refusing
admin writes, verified by `status --expect-revision`, then stopped cleanly by SIGTERM.
"""

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import __main__ as cli  # noqa: E402
from tauceti_queue import config as cfg  # noqa: E402
from tauceti_queue import runtime as rt  # noqa: E402

FIXTURE = json.loads((REPO / "tests/fixtures/queue-status-fixture.json").read_text())
NOW = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        for variable in list(os.environ):
            if variable.startswith(cfg.ENV_PREFIX):
                del os.environ[variable]
        self.creds = self.home / ".config/tauceti-queue"
        write_credentials(self.creds)

    def config_file(self, extra: str = "") -> Path:
        path = self.root / "config.toml"
        path.write_text(
            f'host = "testhost"\ncanonical_branch = "main"\nlisten_port = {free_port()}\n'
            f'database = "{self.root}/queue.sqlite3"\nlog_dir = "{self.root}/logs"\n'
            f'content_clone = "{self.root}/content"\n' + extra
        )
        return path


class RevisionDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "checkout"
        (self.repo / "pkg").mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "pkg/__init__.py").write_text("")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def test_git_revision_and_dirty_flag(self):
        revision = rt.running_revision(self.repo / "pkg")
        self.assertEqual(revision, rt.Revision(self.sha, False, "git"))
        self.assertEqual(revision.short, self.sha[:12])
        (self.repo / "pkg/__init__.py").write_text("changed\n")
        self.assertEqual(rt.running_revision(self.repo / "pkg"), rt.Revision(self.sha, True, "git"))

    def test_baked_revision_wins(self):
        baked = "f" * 40
        (self.repo / "pkg/_revision.py").write_text(f'SHA = "{baked}"\nDIRTY = False\n')
        self.assertEqual(rt.running_revision(self.repo / "pkg"), rt.Revision(baked, False, "baked"))
        (self.repo / "pkg/_revision.py").write_text("SHA = 'not a sha'\n")
        self.assertEqual(rt.running_revision(self.repo / "pkg").source, "git")
        (self.repo / "pkg/_revision.py").write_text("this is not python\n")
        self.assertEqual(rt.running_revision(self.repo / "pkg").source, "git")

    def test_outside_a_checkout(self):
        outside = Path(self.tmp.name) / "plain"
        outside.mkdir()
        self.assertEqual(rt.running_revision(outside), rt.Revision("unknown", False, "unknown"))

    def test_the_package_itself_reports_this_checkout(self):
        revision = rt.running_revision()
        self.assertEqual(revision.sha, git(REPO, "rev-parse", "HEAD"))


class StatusDocument(unittest.TestCase):
    def runtime(self) -> rt.ServiceRuntime:
        return rt.ServiceRuntime(
            revision=rt.Revision("a" * 40, False, "git"),
            started_at="2026-11-04T06:30:00Z",
            host="chungus2",
            pid=4242,
            repository="TauCetiProject/TauCeti",
            canonical_branch="main",
            publication_enabled=False,
        )

    def test_envelope_without_a_controller(self):
        document = rt.status_document(self.runtime(), {"insertion_age_window": 16}, None, "store missing")
        self.assertEqual(document["schema"], "tauceti-queue.status/v1")
        self.assertEqual(document["service"]["revision"], "a" * 40)
        self.assertEqual(document["service"]["started_at"], "2026-11-04T06:30:00Z")
        self.assertEqual(document["service"]["controller"], "unavailable")
        self.assertEqual(document["service"]["reason"], "store missing")
        self.assertEqual(document["service"]["mode"], "shadow")
        self.assertIsNone(document["head"])
        self.assertEqual(document["conditions"], [])
        self.assertEqual(document["feeds"]["observation"], "/repositories/1/active-batches")
        json.dumps(document)

    def test_controller_status_merges_under_the_service(self):
        controller = {
            "head": FIXTURE["head"],
            "settings": {"insertion_age_window": 4},
            "service": {"revision": "stale"},
            "approved_waiting": 9,
        }
        document = rt.status_document(self.runtime(), {"insertion_age_window": 16, "repair_concurrency": 1}, controller)
        self.assertEqual(document["service"]["controller"], "running")
        self.assertEqual(document["service"]["revision"], "a" * 40)
        self.assertEqual(document["settings"], {"insertion_age_window": 4, "repair_concurrency": 1})
        self.assertEqual(document["head"]["id"], 1)
        self.assertEqual(document["approved_waiting"], 9)

    def test_summary_from_the_fixture(self):
        text = rt.summarize(FIXTURE, now=NOW)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 24, text)
        self.assertIn("revision 0123456789ab", lines[0])
        self.assertIn("on chungus2", lines[0])
        self.assertIn("up 1h30m", lines[0])
        self.assertIn("publication_enabled=true", lines[1])
        self.assertIn("! stall_over_two_intervals since 2026-11-04T23:20:00Z", text)
        self.assertIn("HOLD: inference unavailable since 2026-11-04T23:50:00Z (10m): anthropic: 529", text)
        self.assertIn("STALL: phase build silent 2100s (since 2026-11-04T23:25:00Z, interval 900s)", text)
        self.assertIn("head: batch 1 v6  1 members  base 67aa17161ba1  tip e50a09636522  unclaimed  stage ci", text)
        self.assertIn(
            "ci run 41934607574 on nsc-runner-0cui8flmji4i2@nsc-runner-0cui8flmji4i2 pending  started 2026-11-04T23:33:09Z (26m ago)",
            text,
        )
        self.assertIn("(2 earlier phases)", text)
        self.assertIn("pick-runner / pick", text)
        self.assertIn("open batch: 2 size 1197, oldest member 31614s", text)
        self.assertIn("approved waiting: 190", text)
        self.assertIn("settings: insertion_age_window=16  repair_concurrency=1  stall_interval_seconds=900", text)

    def test_age_accepts_epoch_seconds_and_iso_strings(self):
        self.assertEqual(rt._age(1793836200.0, NOW), "10m")
        self.assertEqual(rt._age("2026-11-04T23:50:00Z", NOW), "10m")
        self.assertEqual(rt._age(None, NOW), "?")
        self.assertEqual(rt._age("garbage", NOW), "?")
        self.assertEqual(rt._when(1793836200.0), "2026-11-04T23:50:00Z")

    def test_summary_of_an_idle_service(self):
        document = rt.status_document(self.runtime(), {"insertion_age_window": 16}, {"approved_waiting": 0}, None)
        text = rt.summarize(document, now=NOW)
        self.assertIn("head: none", text)
        self.assertIn("open batch: none", text)
        self.assertIn("approved waiting: 0", text)
        self.assertNotIn("STALL", text)
        self.assertNotIn("HOLD", text)


class CheckConfig(Base):
    def test_good_configuration(self):
        path = self.config_file()
        with captured() as (out, _err):
            code = cli.main(["check-config", "--config", str(path)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("ok: no problems found", out.getvalue())
        self.assertIn("testhost", out.getvalue())
        with captured() as (out, _err):
            self.assertEqual(cli.main(["check-config", "--config", str(path), "--json"]), 0)
        document = json.loads(out.getvalue())
        self.assertEqual(document["host"], "testhost")
        self.assertEqual(document["problems"], [])
        with captured() as (out, _err):
            self.assertEqual(cli.main(["check-config", "--config", str(path), "--print", "database"]), 0)
        self.assertEqual(out.getvalue().strip(), f"{self.root}/queue.sqlite3")
        with captured() as (out, _err):
            self.assertEqual(cli.main(["check-config", "--config", str(path), "--print", "settings"]), 0)
        self.assertEqual(json.loads(out.getvalue())["repair_concurrency"], 1)
        with captured() as (_out, err):
            self.assertEqual(cli.main(["check-config", "--config", str(path), "--print", "nope"]), 1)
        self.assertIn("unknown key", err.getvalue())

    def test_problems_exit_1(self):
        (self.creds / "github-webhook-secret").unlink()
        path = self.config_file()
        with captured() as (out, _err):
            self.assertEqual(cli.main(["check-config", "--config", str(path)]), 1)
        self.assertIn("missing credential file", out.getvalue())
        with captured() as (_out, err):
            self.assertEqual(cli.main(["check-config", "--config", str(self.root / "absent.toml")]), 1)
        self.assertIn("not found", err.getvalue())
        bad = self.root / "bad.toml"
        bad.write_text("listen_prot = 1\n")
        with captured() as (_out, err):
            self.assertEqual(cli.main(["check-config", "--config", str(bad)]), 1)
        self.assertIn("unknown key", err.getvalue())


class StatusCommand(Base):
    def test_summary_and_json(self):
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            return FIXTURE

        args = cli.build_parser().parse_args(["status", "--url", "http://queue.local:1/"])
        with captured() as (out, _err):
            self.assertEqual(cli.cmd_status(args, fetch=fetch), 0)
        self.assertEqual(calls, ["http://queue.local:1/status"])
        self.assertIn("head: batch 1 v6", out.getvalue())
        args = cli.build_parser().parse_args(["status", "--url", "http://queue.local:1", "--json"])
        with captured() as (out, _err):
            self.assertEqual(cli.cmd_status(args, fetch=fetch), 0)
        self.assertEqual(json.loads(out.getvalue())["head"]["id"], 1)

    def test_default_url_comes_from_the_configuration(self):
        path = self.config_file()
        port = cfg.load(path).listen_port
        seen = []
        args = cli.build_parser().parse_args(["status", "--config", str(path)])
        with captured():
            cli.cmd_status(args, fetch=lambda url, **_: seen.append(url) or FIXTURE)
        self.assertEqual(seen, [f"http://127.0.0.1:{port}/status"])

    def test_expected_revision(self):
        parser = cli.build_parser()
        with captured() as (_out, err):
            args = parser.parse_args(["status", "--url", "http://q", "--expect-revision", "0123456789abcdef"])
            self.assertEqual(cli.cmd_status(args, fetch=lambda *_a, **_k: FIXTURE), 0)
        self.assertIn("running revision matches", err.getvalue())
        with captured() as (_out, err):
            args = parser.parse_args(["status", "--url", "http://q", "--expect-revision", "deadbeef00"])
            self.assertEqual(cli.cmd_status(args, fetch=lambda *_a, **_k: FIXTURE), 2)
        self.assertIn("does not match", err.getvalue())
        with captured():
            args = parser.parse_args(["status", "--url", "http://q", "--expect-revision", "012"])
            self.assertEqual(cli.cmd_status(args, fetch=lambda *_a, **_k: FIXTURE), 2, "too short to be a safe prefix")

    def test_wait_retries_until_the_service_answers(self):
        attempts = []

        def fetch(url, **_kwargs):
            attempts.append(url)
            if len(attempts) < 3:
                raise ConnectionRefusedError("not yet")
            return FIXTURE

        with mock.patch.object(rt.time, "sleep"):
            args = cli.build_parser().parse_args(["status", "--url", "http://q", "--wait", "30"])
            with captured():
                self.assertEqual(cli.cmd_status(args, fetch=fetch), 0)
        self.assertEqual(len(attempts), 3)
        args = cli.build_parser().parse_args(["status", "--url", "http://q"])
        with captured() as (_out, err):
            self.assertEqual(cli.cmd_status(args, fetch=mock.Mock(side_effect=ConnectionRefusedError("down"))), 1)
        self.assertIn("no status from http://q/status", err.getvalue())


class SettingsCommand(Base):
    def test_show_and_set(self):
        requests = []

        def fetch(url, method="GET", body=None, token=None, **_kwargs):
            requests.append((url, method, body, token))
            return {"settings": {"insertion_age_window": 8}}

        parser = cli.build_parser()
        with captured() as (out, _err):
            self.assertEqual(
                cli.cmd_settings(parser.parse_args(["settings", "--url", "http://q", "show"]), fetch=fetch), 0
            )
        self.assertEqual(json.loads(out.getvalue())["settings"]["insertion_age_window"], 8)
        with captured():
            args = parser.parse_args(
                ["settings", "--url", "http://q", "set", "insertion_age_window", "8", "--token", "ghp_x"]
            )
            self.assertEqual(cli.cmd_settings(args, fetch=fetch), 0)
        self.assertEqual(requests[-1], ("http://q/admin/settings", "POST", {"insertion_age_window": 8}, "ghp_x"))
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_env"}):
            with captured():
                args = parser.parse_args(["settings", "--url", "http://q", "set", "repair_concurrency", "2"])
                self.assertEqual(cli.cmd_settings(args, fetch=fetch), 0)
        self.assertEqual(requests[-1][3], "ghp_env")

    def test_rejects_bad_values(self):
        parser = cli.build_parser()
        with captured() as (_out, err):
            args = parser.parse_args(
                ["settings", "--url", "http://q", "set", "repair_concurrency", "two", "--token", "t"]
            )
            self.assertEqual(cli.cmd_settings(args, fetch=mock.Mock()), 1)
        self.assertIn("takes an integer", err.getvalue())
        with captured() as (_out, err):
            args = parser.parse_args(
                ["settings", "--url", "http://q", "set", "repair_concurrency", "0", "--token", "t"]
            )
            self.assertEqual(cli.cmd_settings(args, fetch=mock.Mock()), 1)
        self.assertIn("at least 1", err.getvalue())
        with captured(), self.assertRaises(SystemExit):
            parser.parse_args(["settings", "set", "batch_cap", "4"])

    def test_http_errors_are_reported(self):
        parser = cli.build_parser()
        args = parser.parse_args(["settings", "--url", "http://q", "set", "repair_concurrency", "2", "--token", "t"])
        with captured() as (_out, err):
            code = cli.cmd_settings(
                args, fetch=mock.Mock(side_effect=RuntimeError("POST http://q/admin/settings: HTTP 403: not an admin"))
            )
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", err.getvalue())


class ServeProcess(Base):
    """The real entrypoint in a subprocess, in fallback mode (no controller installed)."""

    def test_serve_status_and_graceful_stop(self):
        path = self.config_file()
        config = cfg.load(path)
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        process = subprocess.Popen(
            [sys.executable, "-m", "tauceti_queue", "serve", "--config", str(path)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            expected = git(REPO, "rev-parse", "HEAD")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tauceti_queue",
                    "status",
                    "--config",
                    str(path),
                    "--wait",
                    "20",
                    "--expect-revision",
                    expected,
                ],
                cwd=str(REPO),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("controller unavailable", result.stdout)
            self.assertIn("running revision matches", result.stderr)

            document = cli.fetch_json(config.listen_url + "/status")
            self.assertEqual(document["schema"], "tauceti-queue.status/v1")
            self.assertEqual(document["service"]["revision"], expected)
            self.assertEqual(document["service"]["host"], "testhost")
            self.assertEqual(document["service"]["controller"], "unavailable")
            self.assertEqual(document["service"]["pid"], process.pid)
            self.assertTrue(document["service"]["started_at"].endswith("Z"))
            health = cli.fetch_json(config.listen_url + "/healthz")
            self.assertTrue(health["ok"])
            settings = cli.fetch_json(config.listen_url + "/settings")
            self.assertEqual(settings["settings"]["stall_interval_seconds"], 900)
            with self.assertRaisesRegex(RuntimeError, "HTTP 501"):
                cli.fetch_json(
                    config.listen_url + "/admin/settings", method="POST", body={"repair_concurrency": 2}, token="t"
                )
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                cli.fetch_json(config.listen_url + "/nothing")
            self.assertTrue((self.root / "logs").is_dir())

            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
        except BaseException:
            process.kill()
            process.wait()
            raise
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("stopping (SIGTERM)", stderr)
        self.assertIn("stopped", stderr.splitlines()[-1])
        self.assertIn(f"revision {expected}", stderr)
        time.sleep(0.1)
        with self.assertRaises((ConnectionRefusedError, OSError)):
            cli.fetch_json(config.listen_url + "/status", timeout=2)

    def test_serve_refuses_a_broken_configuration(self):
        (self.creds / "github-private-key.pem").unlink()
        path = self.config_file()
        result = subprocess.run(
            [sys.executable, "-m", "tauceti_queue", "serve", "--config", str(path)],
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing credential file", result.stderr)


if __name__ == "__main__":
    unittest.main()

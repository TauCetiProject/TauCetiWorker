#!/usr/bin/env python3
"""Private operator files never discover GitHub tokens or send a bearer through redirects."""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import __main__ as cli


@unittest.skipUnless(os.name == "posix", "checked private files require POSIX")
class OperatorFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="queue-operator-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "control-token"
        self.path.write_text("dedicated-source-control\n")
        self.path.chmod(0o600)

    def args(self, url="http://127.0.0.1:4567"):
        return cli.build_parser().parse_args(
            [
                "settings",
                *(["--url", url] if url else []),
                "set",
                "publication_enabled",
                "false",
                "--token-file",
                str(self.path),
            ]
        )

    def test_file_is_used_without_env_or_gh_discovery(self):
        fetch = Mock(return_value={"settings": {"publication_enabled": False}})
        with (
            patch.dict(os.environ, GITHUB_TOKEN="wrong-github-token"),
            patch.object(cli.subprocess, "run", side_effect=AssertionError("gh discovery")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_settings(self.args(), fetch=fetch), 0)
        self.assertEqual(fetch.call_args.kwargs["token"], "dedicated-source-control")
        self.assertFalse(fetch.call_args.kwargs["allow_redirects"])
        self.assertEqual(fetch.call_args.args[0], "http://127.0.0.1:4567/admin/settings")

    def test_bad_file_never_falls_back_and_error_contains_no_credential(self):
        for value, mode in (("", 0o600), ("private secret with spaces", 0o600), ("secret", 0o644)):
            self.path.write_text(value)
            self.path.chmod(mode)
            fetch = Mock()
            with (
                patch.dict(os.environ, GITHUB_TOKEN="fallback-must-not-be-used"),
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(cli.cmd_settings(self.args(), fetch=fetch), 1)
            fetch.assert_not_called()
            self.assertNotIn("fallback-must-not-be-used", error.getvalue())
            self.assertNotIn("private secret", error.getvalue())
        self.path.unlink()
        self.path.symlink_to(self.root / "missing")
        with self.assertRaises(RuntimeError):
            cli._operator_token(None, self.path)

    def test_explicit_safe_destination_required_before_default_config_is_read(self):
        for url in (None, "http://remote.example", "https://user:password@queue.example", "https://queue.example/path"):
            fetch = Mock()
            with (
                patch.object(cli.configuration, "load", side_effect=AssertionError("ambient config")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.cmd_settings(self.args(url), fetch=fetch), 1)
            fetch.assert_not_called()

    def test_normalized_origin_is_used_for_empty_query_or_fragment(self):
        for suffix in ("?", "#", "/"):
            fetch = Mock(return_value={})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_settings(self.args("http://127.0.0.1:4567" + suffix), fetch=fetch), 0)
            self.assertEqual(fetch.call_args.args[0], "http://127.0.0.1:4567/admin/settings")
        fetch = Mock(return_value={})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_settings(self.args("https://QUEUE.example:443/"), fetch=fetch), 0)
        self.assertEqual(fetch.call_args.args[0], "https://queue.example/admin/settings")

    def test_file_transport_errors_are_clean_and_legacy_errors_keep_their_behavior(self):
        for error in (OSError("credential-never-echo"), ValueError("credential-never-echo")):
            with patch.object(cli.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "operator request unavailable") as caught:
                    cli.fetch_json(
                        "http://127.0.0.1:1/admin/settings", token="credential-never-echo", allow_redirects=False
                    )
                self.assertNotIn("credential-never-echo", str(caught.exception))
            with patch.object(cli.urllib.request, "urlopen", side_effect=error), self.assertRaises(type(error)):
                cli.fetch_json("http://127.0.0.1:1/admin/settings", token="legacy")

    def test_inherited_proxy_cannot_receive_the_control_token(self):
        received = []

        class Origin(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(("origin", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        class Proxy(Origin):
            def do_POST(self):
                received.append(("proxy", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Origin, Proxy)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            proxy = f"http://127.0.0.1:{servers[1].server_port}"
            with (
                patch.dict(
                    os.environ,
                    HTTP_PROXY=proxy,
                    http_proxy=proxy,
                    HTTPS_PROXY=proxy,
                    https_proxy=proxy,
                    NO_PROXY="",
                    no_proxy="",
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.cmd_settings(self.args(f"http://127.0.0.1:{servers[0].server_port}")), 0)
            self.assertEqual(received, [("origin", "/admin/settings", "Bearer dedicated-source-control")])
        finally:
            for server, thread in zip(servers, threads, strict=True):
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_token_options_are_mutually_exclusive_for_every_mutating_command(self):
        commands = [
            ["settings", "set", "publication_enabled", "false"],
            ["import-bors", "--feed", "fixture.json"],
            ["reconcile-approvals"],
        ]
        for command in commands:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args([*command, "--token", "secret", "--token-file", str(self.path)])
            self.assertEqual(caught.exception.code, 2)

    def test_file_reconcile_uses_dedicated_token(self):
        args = cli.build_parser().parse_args(
            ["reconcile-approvals", "--url", "http://127.0.0.1:4567", "--token-file", str(self.path)]
        )
        fetch = Mock(return_value={"ok": True})
        with (
            patch.object(cli.subprocess, "run", side_effect=AssertionError("gh discovery")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_reconcile_approvals(args, fetch=fetch), 0)
        self.assertEqual(fetch.call_args.kwargs["token"], "dedicated-source-control")

    def test_real_http_redirect_never_forwards_or_replays_control_bearer(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
                self.wfile.write(b"dedicated-source-control")

            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(cli.cmd_settings(self.args(f"http://127.0.0.1:{server.server_port}")), 1)
            self.assertEqual(received, [("/admin/settings", "Bearer dedicated-source-control")])
            self.assertNotIn("dedicated-source-control", error.getvalue())
            self.assertIn("HTTP 302", error.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

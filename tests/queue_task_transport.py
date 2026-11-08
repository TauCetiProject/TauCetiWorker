#!/usr/bin/env python3
"""A native queue peer cannot extend a request deadline by trickling data."""

import datetime
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_source_authority as native_fixtures

from tauceti_queue import repair_task, task_transport
from tauceti_queue.task_identity import TaskBinding


class Deadline(unittest.TestCase):
    def setUp(self):
        self.native = native_fixtures.NativeClaims()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        ledger = self.native.fixture.ledger
        self.binding = TaskBinding.from_policy(ledger.policy, queue_epoch=ledger.epoch)

    def server(self, mode, status=200, payload=b"{}"):
        calls = []
        finished = threading.Event()

        class Peer(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append(self.path)
                try:
                    if mode == "fast":
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if mode == "headers":
                        self.wfile.write(f"HTTP/1.0 {status} OK\r\nX-Slow: ".encode())
                    else:
                        self.send_response(status)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                    # Frequent bytes keep an ordinary socket timeout alive.
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if finished.wait(0.03):
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    finished.set()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(finished.set)
        return f"http://127.0.0.1:{server.server_port}", calls

    def bounded(self, action, *, limit=0.6):
        processes = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        started = time.monotonic()
        with patch.object(task_transport.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex((OSError, repair_task.ServiceError), "total deadline.*uncertain"):
                action(limit)
        self.assertLess(time.monotonic() - started, limit + 1.0)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode, "timed-out request process must be reaped")
        self.assertIsNotNone(processes[0].poll())
        # Secret bytes are passed on stdin, never in the process command line.
        self.assertNotIn("private-native-bearer", repr(processes[0].args))

    def test_slow_success_headers_have_a_total_deadline(self):
        url, calls = self.server("headers")
        self.bounded(
            lambda timeout: repair_task.TaskClient(
                url, "private-native-bearer", "worker", timeout, binding=self.binding
            ).claim()
        )
        self.assertEqual(calls, ["/tasks/claim"])

    def test_slow_success_and_error_bodies_have_a_total_deadline(self):
        for status in (200, 500):
            with self.subTest(status=status):
                url, calls = self.server("body", status)
                self.bounded(
                    lambda timeout, url=url: repair_task.TaskClient(
                        url, "private-native-bearer", "worker", timeout, binding=self.binding
                    ).claim()
                )
                self.assertEqual(calls, ["/tasks/claim"])

    def test_hanging_resolver_is_killed_and_reaped(self):
        with tempfile.TemporaryDirectory(prefix="queue-dns-deadline-") as temporary:
            reached = Path(temporary) / "resolver-entered"
            source = (
                "import pathlib, socket, time\n"
                "def stuck(*args, **kwargs):\n"
                f"    pathlib.Path({str(reached)!r}).touch()\n"
                "    time.sleep(120)\n"
                "socket.getaddrinfo = stuck\n" + task_transport._HELPER_SOURCE
            )
            with patch.object(task_transport, "_HELPER_SOURCE", source):
                self.bounded(
                    lambda timeout: task_transport.post(
                        "http://127.0.0.1:1/tasks/claim",
                        b"{}",
                        {"Authorization": "Bearer private-native-bearer"},
                        timeout=timeout,
                    )
                )
            self.assertTrue(reached.exists(), "fixture must actually enter DNS resolution")

    def test_helper_bounds_its_own_lifetime_without_a_parent_timeout(self):
        url, calls = self.server("headers")
        request = json.dumps({"url": url, "data": "e30=", "headers": {}, "deadline": time.monotonic() + 0.4}).encode()
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", task_transport._HELPER_SOURCE],
            input=request,
            capture_output=True,
            timeout=3,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(calls, ["/"])

    def test_uncertain_native_posts_are_not_automatically_retried(self):
        for mode, status, payload in (
            ("headers", 200, b"{}"),
            ("fast", 500, b"{}"),
            ("fast", 200, b"{}"),
            ("fast", 200, b"[]"),
            ("fast", 201, b"{}"),
            ("fast", 200, b""),
        ):
            with self.subTest(mode=mode, status=status, payload=payload):
                url, calls = self.server(mode, status, payload)
                client = repair_task.TaskClient(url, "token", "worker", 0.5, binding=self.binding)
                with self.assertRaises(repair_task.NativeTaskDeliveryUncertain):
                    repair_task.with_retries(client.claim, attempts=3, delays=(0,), what="claim")
                self.assertEqual(calls, ["/tasks/claim"])

    def test_helper_failures_and_malformed_framing_cannot_alias_a_valid_response(self):
        for source, diagnostic in (
            ("raise SystemExit(2)", "helper failed"),
            ("print('malformed')", "invalid response"),
            ("print('999\\n{}')", "invalid response"),
        ):
            with self.subTest(source=source), patch.object(task_transport, "_HELPER_SOURCE", source):
                with self.assertRaisesRegex(task_transport.TaskTransportError, diagnostic) as raised:
                    task_transport.post("https://queue.example", b"{}", {}, timeout=2)
                self.assertTrue(raised.exception.delivery_uncertain)

    def test_helper_refuses_remote_http_without_sending_a_request(self):
        with self.assertRaisesRegex(task_transport.TaskTransportError, "refused the service URL") as raised:
            task_transport.post("http://outside.example", b"{}", {"Authorization": "private-native-bearer"}, timeout=2)
        self.assertFalse(raised.exception.delivery_uncertain)

    def test_missing_python_interpreter_is_a_typed_transport_failure(self):
        for executable in (None, ""):
            with patch.object(task_transport.sys, "executable", executable):
                with self.assertRaisesRegex(task_transport.TaskTransportError, "Python interpreter"):
                    task_transport.post("https://queue.example", b"{}", {}, timeout=1)

    def test_native_timeout_must_be_positive_finite_and_bounded(self):
        for value in (0, -1, True, None, "1", float("nan"), float("inf"), 3601):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                repair_task.TaskClient("https://queue.example", "token", "worker", value, binding=self.binding)

    def test_tls_trust_and_hostname_checks_survive_process_isolation(self):
        calls = []

        class Peer(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append(self.path)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                pass

        with tempfile.TemporaryDirectory(prefix="queue-task-tls-") as temporary:
            root = Path(temporary)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            key_file = root / "key.pem"
            key_file.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
                )
            )
            key_file.chmod(0o600)
            empty = root / "empty.pem"
            empty.write_text("")
            now = datetime.datetime.now(datetime.UTC)
            for address in ("127.0.0.1", "127.0.0.2"):
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "queue fixture")])
                certificate = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(subject)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(minutes=1))
                    .not_valid_after(now + datetime.timedelta(hours=1))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                    .add_extension(
                        x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False
                    )
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .sign(key, hashes.SHA256())
                )
                cert_file = root / f"{address}.pem"
                cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert_file, key_file)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = repair_task.TaskClient(
                        f"https://127.0.0.1:{server.server_port}",
                        "private-native-bearer",
                        "worker",
                        3,
                        binding=self.binding,
                    )
                    with patch.dict(os.environ, SSL_CERT_FILE=str(empty), SSL_CERT_DIR=str(root / "no-cas")):
                        with self.assertRaises(repair_task.ServiceError):
                            client.claim()
                    self.assertEqual(calls, [])
                    with patch.dict(os.environ, SSL_CERT_FILE=str(cert_file), SSL_CERT_DIR=str(root / "no-cas")):
                        if address == "127.0.0.1":
                            self.assertIsNone(client.claim())
                            self.assertEqual(calls, ["/tasks/claim"])
                            calls.clear()
                        else:
                            with self.assertRaises(repair_task.ServiceError):
                                client.claim()
                            self.assertEqual(calls, [])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)

    def test_http_failure_does_not_disclose_or_resend_credentials(self):
        url, calls = self.server("body")
        # A startup/transport failure reports a fixed diagnostic, never a
        # serialized input request or a child traceback.
        with patch.object(task_transport.subprocess, "Popen", side_effect=OSError("private-native-bearer")) as send:
            with self.assertRaises(task_transport.TaskTransportError) as raised:
                task_transport.post(url, b"{}", {"Authorization": "private-native-bearer"}, timeout=1)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("private-native-bearer", str(raised.exception))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

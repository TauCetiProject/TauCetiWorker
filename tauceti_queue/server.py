"""The queue service's HTTP surface: health, status, the Bors-compatible feeds, the webhook and the
worker and admin endpoints.

`create_server(controller, config)` returns a stdlib `ThreadingHTTPServer` (one thread per request,
daemon threads) that shares the one `Controller` object with the controller loop running in the
same process. Routes:

    GET  /health/                                     200 once the process serves
    GET  /status                                      the controller's status JSON plus the service's
                                                      revision, start time, GitHub client counters
                                                      and webhook counters
    GET  /repositories/<n>/active-batches?base=..     tauceti-bors.observation/v1 (feeds.py)
    GET  /repositories/<n>/candidates?base=..         tauceti-bors.candidates/v1 (feeds.py)
    POST /webhook/github                              HMAC-verified GitHub deliveries (webhook.py)
    POST /tasks/claim                                 worker endpoints: JSON bodies, authenticated by
    POST /tasks/<id>/renew                            a GitHub token in `Authorization: Bearer`,
    POST /tasks/<id>/result                           resolved to a login through `GET /user`
    POST /admin/settings                              same bearer scheme; the controller checks the
    POST /admin/urgent                                caller's repository-admin permission

The controller methods behind them: `status()`, `handle_event`, `claim_task(worker_identity,
github_login)`, `renew_task(task_id, worker_identity, github_login, body)`, `submit_task(task_id,
worker_identity, github_login, body)`, `update_settings(github_login, settings)`, `mark_urgent(repo,
pr, actor)`, and the `feeds.FeedSource` reads. A controller may raise `PermissionError` (403),
`LookupError` (404) or `ValueError` (400); anything else is a 500 with the traceback logged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import pathlib
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import feeds, webhook

log = logging.getLogger("tauceti_queue.server")

MAX_BODY = 25 * 1024 * 1024
REPOSITORY_ROUTE = re.compile(r"^/repositories/(\d+)/(active-batches|candidates)$")
TASK_ROUTE = re.compile(r"^/tasks/([A-Za-z0-9_.:-]+)/(renew|result)$")


@dataclasses.dataclass
class ServerConfig:
    webhook_secret: bytes
    repositories: dict[int, str]
    adapter: object  # github_adapter.GitHubAdapter (or a fake in tests)
    host: str = "127.0.0.1"
    port: int = 8080
    review_app_id: int | None = webhook.REVIEW_APP_ID
    revision: str = ""
    auth_cache_seconds: float = 300.0
    clock: Callable[[], float] = time.time


def running_revision(package_dir: pathlib.Path | None = None) -> str:
    """The git SHA of the running checkout, so a deployment can be verified from `/status`."""
    root = (package_dir or pathlib.Path(__file__).resolve().parent).parent
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class BearerAuth:
    """Resolve `Authorization: Bearer <GitHub token>` to a login, caching by token digest."""

    def __init__(self, adapter, *, ttl: float, clock: Callable[[], float]) -> None:
        self._adapter = adapter
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[float, tuple[str, int]]] = {}
        self._lock = threading.Lock()

    def resolve(self, header: str | None) -> tuple[str, int] | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer ") :].strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(digest)
            if cached and cached[0] > now:
                return cached[1]
        identity = self._adapter.user_for_token(token)
        if identity is None:
            return None
        with self._lock:
            self._cache[digest] = (now + self._ttl, identity)
            if len(self._cache) > 1024:
                for key in [k for k, (expiry, _) in self._cache.items() if expiry <= now]:
                    del self._cache[key]
        return identity


class QueueHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, controller, config: ServerConfig) -> None:
        super().__init__(address, handler)
        self.controller = controller
        self.config = config
        self.started_at = config.clock()
        self.receiver = webhook.WebhookReceiver(
            controller, config.adapter, repositories=config.repositories.values(), review_app_id=config.review_app_id
        )
        self.auth = BearerAuth(config.adapter, ttl=config.auth_cache_seconds, clock=config.clock)
        self.revision = config.revision or running_revision()


class Handler(BaseHTTPRequestHandler):
    server: QueueHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.info("%s " + format, self.address_string(), *args)

    # -- helpers

    def _send(
        self, status: int, body: bytes, content_type: str = "application/json", extra: dict | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload, extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), extra=extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            self._error(413, "body too large")
            return None
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict | None:
        body = self._read_body()
        if body is None:
            return None
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            self._error(400, "body is not JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object")
            return None
        return data

    def _call(self, action: Callable[[], object]) -> None:
        """Run a controller action and map its outcome to a reply."""
        try:
            result = action()
        except PermissionError as error:
            self._error(403, str(error) or "forbidden")
        except LookupError as error:
            self._error(404, str(error) or "not found")
        except ValueError as error:
            self._error(400, str(error) or "bad request")
        except Exception:  # noqa: BLE001 - the service must answer every request
            log.error("request %s %s failed:\n%s", self.command, self.path, traceback.format_exc())
            self._error(500, "internal error")
        else:
            if result is None:
                self._send(204, b"")
            else:
                self._json(200, result)

    def _authenticated(self) -> tuple[str, int] | None:
        identity = self.server.auth.resolve(self.headers.get("Authorization"))
        if identity is None:
            self._error(401, "a GitHub token in `Authorization: Bearer` is required")
        return identity

    # -- routes

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        if path in ("/health", "/health/"):
            self._send(200, b"ok\n", "text/plain")
        elif path == "/status":
            self._call(self._status)
        elif match := REPOSITORY_ROUTE.match(path):
            repo = self.server.config.repositories.get(int(match.group(1)))
            if repo is None:
                self._send(404, b"")
                return
            self._call(lambda: self._feed(match.group(2), repo, query))
        else:
            self._error(404, "no such route")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path
        if path == "/webhook/github":
            self._webhook()
            return
        if path == "/tasks/claim":
            self._task_claim()
            return
        if match := TASK_ROUTE.match(path):
            self._task(match.group(1), match.group(2))
            return
        if path == "/admin/settings":
            self._admin_settings()
            return
        if path == "/admin/urgent":
            self._admin_urgent()
            return
        self._error(404, "no such route")

    def _status(self) -> dict:
        controller = self.server.controller
        status = controller.status() if hasattr(controller, "status") else {}
        status = dict(status or {})
        adapter = self.server.config.adapter
        client = getattr(adapter, "client", None)
        status.update(
            {
                "revision": self.server.revision,
                "started_at": feeds._iso(self.server.started_at),
                "github": client.stats() if client is not None and hasattr(client, "stats") else {},
                "webhooks": {
                    "received": self.server.receiver.received,
                    "duplicates": self.server.receiver.duplicates,
                    "ignored": self.server.receiver.ignored,
                },
            }
        )
        return status

    def _feed(self, which: str, repo: str, query: dict) -> dict:
        source = self.server.controller
        base = (query.get("base") or [None])[0]
        if not base:
            canonical = getattr(source, "canonical_branch", None)
            base = canonical(repo) if callable(canonical) else "main"
        batch_id = None
        raw_batch = (query.get("batch_id") or [""])[0]
        if re.fullmatch(r"[1-9]\d*", raw_batch):
            batch_id = int(raw_batch)
        if which == "active-batches":
            return feeds.observation_feed(source, repo, base, batch_id, now=self.server.config.clock())
        promoted = (query.get("promoted") or [""])[0] == "true"
        return feeds.candidates_feed(source, repo, base, batch_id, promoted=promoted)

    def _webhook(self) -> None:
        body = self._read_body()
        if body is None:
            return
        if not webhook.verify_signature(
            self.server.config.webhook_secret, body, self.headers.get("X-Hub-Signature-256")
        ):
            self._error(401, "bad signature")
            return
        try:
            payload = json.loads(body)
        except ValueError:
            self._error(400, "payload is not JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "payload must be a JSON object")
            return
        delivery = webhook.Delivery(
            event=str(self.headers.get("X-GitHub-Event") or ""),
            delivery_id=str(self.headers.get("X-GitHub-Delivery") or ""),
            payload=payload,
        )
        try:
            disposition = self.server.receiver.handle(delivery)
        except Exception:  # noqa: BLE001 - GitHub retries on 5xx; the failure is logged for the operator
            log.error("webhook %s %s failed:\n%s", delivery.event, delivery.delivery_id, traceback.format_exc())
            self._error(500, "delivery failed")
            return
        self._json(202, {"result": disposition})

    def _worker_identity(self, body: dict) -> str:
        return str(body.get("worker") or self.headers.get("X-TauCeti-Worker") or "")

    def _task_claim(self) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        login = identity[0]
        self._call(lambda: self.server.controller.claim_task(self._worker_identity(body), login))

    def _task(self, task_id: str, action: str) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        login = identity[0]
        controller = self.server.controller
        method = controller.renew_task if action == "renew" else controller.submit_task
        self._call(lambda: method(task_id, self._worker_identity(body), login, body))

    def _admin_settings(self) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        self._call(lambda: self.server.controller.update_settings(identity[0], body))

    def _admin_urgent(self) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        repo = str(body.get("repo") or "")
        pr = body.get("pr")
        if repo not in self.server.config.repositories.values() or not isinstance(pr, int) or pr <= 0:
            self._error(400, "`repo` must be a served repository and `pr` a positive integer")
            return
        self._call(lambda: self.server.controller.mark_urgent(repo, pr, identity[0]))


def create_server(controller, config: ServerConfig) -> QueueHTTPServer:
    """A server ready for `serve_forever()`; `server_address` carries the bound port."""
    return QueueHTTPServer((config.host, config.port), Handler, controller, config)


def serve_in_thread(server: QueueHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="tauceti-queue-http", daemon=True)
    thread.start()
    return thread

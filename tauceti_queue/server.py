"""The queue service's HTTP surface: health, status, the Bors-compatible feeds, the webhook and the
worker and admin endpoints.

`create_server(controller, config)` returns a stdlib `ThreadingHTTPServer` (one thread per request,
daemon threads) that shares the one `Controller` object with the controller loop running in the
same process. Routes:

    GET  /health/                                     200 once the process serves
    GET  /status                                      the controller's status JSON plus the service's
                                                      revision, start time, GitHub client counters,
                                                      webhook counters and the identity cache
    GET  /repositories/<n>/active-batches?base=..     tauceti-bors.observation/v1 (feeds.py)
    GET  /repositories/<n>/candidates?base=..         tauceti-bors.candidates/v1 (feeds.py)
    POST /webhook/github                              HMAC-verified GitHub deliveries (webhook.py)
    POST /tasks/claim                                 worker endpoints: JSON bodies, authenticated by
    POST /tasks/<id>/renew                            a GitHub token in `Authorization: Bearer`,
    POST /tasks/<id>/result                           resolved to a login through `GET /user`
    POST /admin/settings                              same bearer scheme; the controller checks the
    POST /admin/urgent                                caller's repository-admin permission
    POST /admin/revoke-identity                       `{"login": ...}`: forget the cached identity
                                                      and admin answers for a login

`/admin/settings` takes one JSON object `{"<setting>": <value>, ...}` whose keys are any of the
controller's `DEFAULT_SETTINGS`, each value of the type of that setting's initial value.

The controller methods behind them: `status()`, `handle_event`, `claim_task(worker_identity,
github_login)`, `renew_task(task_id, worker_identity, github_login, body)`, `submit_task(task_id,
worker_identity, github_login, body)`, `update_settings(github_login, settings)`, `mark_urgent(repo,
pr, actor)`, and the `feeds.FeedSource` reads. A controller may raise `PermissionError` (403),
`LookupError` (404) or `ValueError` (400); anything else is a 500 with the traceback logged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
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
from .ports import GitHubUnavailable
from .source_authority import (
    NativeQueueAuthority,
    SourceAccessDenied,
    SourceAuthorityUnavailable,
    principal_key,
)
from .stale_cache import MAX_AGE_SECONDS, StaleCache
from .task_identity import TASK_SCHEMA, TaskBinding

log = logging.getLogger("tauceti_queue.server")

MAX_BODY = 25 * 1024 * 1024
SOURCE_BODY_DEADLINE_SECONDS = 10.0
REPOSITORY_ROUTE = re.compile(r"^/repositories/(\d+)/(active-batches|candidates)$")
TASK_ROUTE = re.compile(r"^/tasks/([A-Za-z0-9_.:-]+)/(renew|result|release|checkpoint)$")
# `ControllerError.code` (tauceti_queue/controller.py) to HTTP status.
ERROR_STATUS = {
    "unknown-task": 404,
    "unknown-claim": 404,
    "unknown-repository": 404,
    "unknown-pr": 404,
    "unknown-setting": 400,
    "claim-expired": 409,
    "claim-superseded": 409,
    "version-mismatch": 409,
    "not-authorized": 403,
}


def error_status(code: str) -> int:
    if code in ERROR_STATUS:
        return ERROR_STATUS[code]
    if code.startswith("unknown-"):
        return 404
    return 400


def _jsonable(value):
    """Controller results as JSON: dataclass-like records expose `to_dict`."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclasses.dataclass
class ServerConfig:
    webhook_secret: bytes
    repositories: dict[int, str]
    adapter: object  # github_adapter.GitHubAdapter (or a fake in tests)
    host: str = "127.0.0.1"
    port: int = 8080
    review_app_id: int | None = webhook.REVIEW_APP_ID
    revision: str = ""
    auth_cache_seconds: float = 6 * 3600.0
    clock: Callable[[], float] = time.time
    # Where the bearer identities and admin answers are kept across restarts (`Store`); the
    # controller's store when None and the controller has one.
    store: object | None = None
    identity_max_age_seconds: float = MAX_AGE_SECONDS
    # How often the background thread asks GitHub again about cached answers older than
    # `auth_cache_seconds`; 0 disables it.
    identity_revalidate_seconds: float = 300.0
    native_webhooks: bool = False
    prebound_socket: object | None = dataclasses.field(default=None, repr=False)


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
    """Resolve `Authorization: Bearer <GitHub token>` to a login, caching by token digest.

    The cache keeps expired entries: when `GET /user` cannot answer (a rate-limited 403, 5xx, a
    network error) the identity resolved earlier is served, so a quota outage does not log workers
    out of their claims. With a `store` the digests and logins (never the tokens) are persisted, so
    a restart during such an outage keeps them too. A 401 from GitHub still evicts the entry and
    refuses the token.
    """

    def __init__(
        self,
        adapter,
        *,
        ttl: float,
        clock: Callable[[], float],
        store=None,
        max_age: float = MAX_AGE_SECONDS,
    ) -> None:
        self.cache = StaleCache(
            adapter.user_for_token,
            ttl=ttl,
            clock=clock,
            logger=log,
            what="bearer identity",
            store=store,
            namespace="bearer",
            max_age=max_age,
            decode=lambda value: (str(value[0]), int(value[1])),
        )

    def revoke_login(self, login: str) -> int:
        """Forget every cached token that resolved to `login`; the number forgotten."""
        keys = self.cache.keys_where(lambda identity: identity[0] == login)
        return sum(self.cache.invalidate(key) for key in keys)

    def resolve(self, header: str | None) -> tuple[str, int] | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer ") :].strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            return self.cache.get(digest, token)
        except GitHubUnavailable as error:
            log.warning("bearer identity lookup failed with nothing cached for %s: %s", digest[:12], error)
            return None


HEALTHZ_LOCK_WAIT_SECONDS = 0.5


class NativeBearerAuth:
    """One fresh source observation per request, with no retained bearer state."""

    def __init__(self, authority):
        self.authority = authority

    @staticmethod
    def token(header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer ") :].strip()
        return token if 0 < len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token) else None

    def resolve(self, header):
        token = self.token(header)
        if token is None:
            return None
        return self.authority.authenticate(token)


def _accepts_wait(callable_: Callable[..., object]) -> bool:
    """Whether a status provider takes the `wait` keyword (`Controller.status`, `Service.status_document`)."""
    try:
        return "wait" in inspect.signature(callable_).parameters
    except (TypeError, ValueError):
        return False


class QueueHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address, handler, controller, config: ServerConfig, status: Callable[[], dict] | None = None
    ) -> None:
        self.source_authority = getattr(controller, "source_authority", None)
        candidate_ci = getattr(controller, "candidate_ci", None)
        if candidate_ci is not None:
            if (
                not isinstance(self.source_authority, NativeQueueAuthority)
                or self.source_authority.source != candidate_ci.ledger.policy.source
                or set(config.repositories.values()) != {self.source_authority.source.repository.name}
            ):
                raise ValueError("source-bound HTTP requires matching fresh native authority and repository")
        elif self.source_authority is not None:
            raise ValueError("native HTTP authority requires a source-bound controller")
        bound = config.prebound_socket
        super().__init__(address, handler, bind_and_activate=bound is None)
        if bound is not None:
            self.socket.close()
            self.socket = bound
            self.server_address = bound.getsockname()
            self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]
            self.server_activate()
        self.controller = controller
        self.config = config
        self.status_provider = status
        self.started_at = config.clock()
        self.receiver = (
            None
            if self.source_authority is not None
            else webhook.WebhookReceiver(
                controller,
                config.adapter,
                repositories=config.repositories.values(),
                review_app_id=config.review_app_id,
            )
        )
        self.native_receiver = None
        if self.source_authority is not None and config.native_webhooks:
            from .forgejo_webhook import ForgejoWebhookReceiver

            self.native_receiver = ForgejoWebhookReceiver(
                controller, self.source_authority.source, config.webhook_secret
            )
        store = config.store if config.store is not None else getattr(controller, "store", None)
        self.identity_store = store
        self.auth = (
            NativeBearerAuth(self.source_authority)
            if self.source_authority is not None
            else BearerAuth(
                config.adapter,
                ttl=config.auth_cache_seconds,
                clock=config.clock,
                store=store,
                max_age=config.identity_max_age_seconds,
            )
        )
        self.admins = (
            None
            if self.source_authority is not None
            else StaleCache(
                config.adapter.repository_admin,
                ttl=config.auth_cache_seconds,
                clock=config.clock,
                logger=log,
                what="admin",
                store=store,
                namespace="server-admin",
                max_age=config.identity_max_age_seconds,
                decode=bool,
            )
        )
        self.revision = config.revision or running_revision()
        self.poller = None  # the approval poller, set by `create_server(..., poller=)`
        self._revalidator_stop = threading.Event()

    def identity_caches(self) -> list[StaleCache]:
        """The caches of GitHub's answers about callers: bearer identities, the server's admin
        answers and the controller's (`Controller.admins`) when it keeps one."""
        if self.source_authority is not None:
            return []
        caches = [self.auth.cache, self.admins]
        controller_admins = getattr(self.controller, "admins", None)
        if isinstance(controller_admins, StaleCache):
            caches.append(controller_admins)
        return caches

    def revalidate_identities(self) -> int:
        answered = 0
        for cache in self.identity_caches():
            try:
                answered += cache.revalidate_due()
            except Exception:  # noqa: BLE001 - the background pass must not die with one bad lookup
                log.error("identity revalidation failed:\n%s", traceback.format_exc())
        return answered

    def _revalidate_loop(self) -> None:
        interval = float(self.config.identity_revalidate_seconds)
        while not self._revalidator_stop.wait(interval):
            self.revalidate_identities()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self.source_authority is None and self.config.identity_revalidate_seconds > 0:
            threading.Thread(target=self._revalidate_loop, name="identity-revalidate", daemon=True).start()
        super().serve_forever(poll_interval)

    def shutdown(self) -> None:
        self._revalidator_stop.set()
        super().shutdown()

    def identity_status(self) -> dict:
        """`/status`'s `identities`: how many answers are cached and whether GitHub can be asked."""
        if self.source_authority is not None:
            return {
                "mode": "fresh-source",
                "source": self.source_authority.source.record(),
                "cached": 0,
                "admin_answers_cached": 0,
                "persisted": False,
            }
        auth = self.auth.cache.stats()
        caches = [cache.stats() for cache in self.identity_caches()]
        unavailable = [stats for stats in caches if stats["github_unavailable"]]
        retry_at = max((stats["retry_at"] for stats in unavailable), default=None)
        return {
            "cached": auth["entries"],
            "admin_answers_cached": sum(stats["entries"] for stats in caches[1:]),
            "persisted": self.identity_store is not None,
            "github_lookups_rate_limited": any(stats["rate_limited"] for stats in caches),
            "github_lookups_unavailable": bool(unavailable),
            "github_lookups_retry_at": feeds._iso(retry_at) if retry_at is not None else None,
            "last_lookup_error": next((stats["last_error"] for stats in unavailable), ""),
        }


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
            if self.server.source_authority is not None:
                self.close_connection = True
            self._error(413, "body too large")
            return None
        if self.server.source_authority is None:
            return self.rfile.read(length) if length else b""
        deadline = time.monotonic() + SOURCE_BODY_DEADLINE_SECONDS
        chunks, remaining = [], length
        previous = self.connection.gettimeout()
        try:
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
                if not chunk:
                    self._error(400, "incomplete request body")
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        except TimeoutError:
            self.close_connection = True
            self.connection.settimeout(previous)
            self._error(408, "native request body deadline exceeded")
            return None
        finally:
            self.connection.settimeout(previous)

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

    def _call(self, action: Callable[[], object], *, headers: dict | None = None) -> None:
        """Run a controller action and map its outcome to a reply."""
        try:
            path = urllib.parse.urlsplit(self.path).path
            task_route = path == "/tasks/claim" or TASK_ROUTE.fullmatch(path)
            if self.server.source_authority is not None and self.command == "POST" and not task_route:
                # Authority reads precede this lock. Refuse a request that spent
                # too long waiting; never let cached or old evidence grant work.
                with self.server.controller._lock.priority():
                    access = self.server.source_authority.validate(getattr(self, "_source_access", None))
                    self.server.source_authority.require_admin(access)
                    result = action()
            else:
                result = action()
        except SourceAuthorityUnavailable as error:
            log.warning("native queue request refused: %s", type(error).__name__)
            self._error(503, str(error))
        except SourceAccessDenied as error:
            log.warning("native queue request denied (status %s)", error.status)
            self._error(error.status, str(error))
        except PermissionError as error:
            if self.server.source_authority is not None:
                log.warning("native queue request refused: %s", type(error).__name__)
            self._error(403, str(error) or "forbidden")
        except LookupError as error:
            if isinstance(error, IndexError | KeyError):  # a bug, not a missing resource: keep the traceback
                log.error("request %s %s raised %r:\n%s", self.command, self.path, error, traceback.format_exc())
            self._error(404, str(error) or "not found")
        except ValueError as error:
            self._error(400, str(error) or "bad request")
        except Exception as error:  # noqa: BLE001 - the service must answer every request
            code = getattr(error, "code", None)
            if isinstance(code, str):
                self._json(error_status(code), {"error": code, "detail": str(error)})
                return
            log.error("request %s %s failed:\n%s", self.command, self.path, traceback.format_exc())
            self._error(500, "internal error")
        else:
            if result is None:
                self._send(204, b"")
            else:
                self._json(200, _jsonable(result), extra=headers)

    def _authenticated(self) -> tuple[str, int] | None:
        self._source_access = None
        try:
            identity = self.server.auth.resolve(self.headers.get("Authorization"))
        except SourceAuthorityUnavailable as error:
            log.warning("native source authentication refused: %s", type(error).__name__)
            self._error(503, str(error))
            return None
        except SourceAccessDenied as error:
            log.warning("native source authentication denied (status %s)", error.status)
            self._error(error.status, str(error))
            return None
        if self.server.source_authority is not None and identity is not None:
            self._source_access = identity
            actor = identity.access.actor
            return principal_key(actor), int(actor.principal.native_id)
        if identity is None:
            source = "native source" if self.server.source_authority is not None else "GitHub"
            self._error(401, f"a {source} token in `Authorization: Bearer` is required")
        return identity

    def _authenticated_json(self):
        if self.server.source_authority is not None:
            token = NativeBearerAuth.token(self.headers.get("Authorization"))
            if token is None:
                self.close_connection = True
                return self._authenticated(), None  # Syntactic check only, before any body or source I/O.
            try:
                self.server.source_authority.require_control_credential(token)
            except SourceAccessDenied as error:
                self.close_connection = True
                self._error(error.status, str(error))
                return None, None
            # A slow request body must not keep an earlier native ACL grant.
            body = self._read_json()
            if body is not None and urllib.parse.urlsplit(self.path).path == "/tasks/claim":
                try:
                    binding = TaskBinding.parse(body.get("task_binding"))
                    ledger = self.server.controller.candidate_ci.ledger
                    if body.get("schema") != TASK_SCHEMA or binding != TaskBinding.from_policy(
                        ledger.policy, queue_epoch=ledger.epoch
                    ):
                        raise ValueError("native task policy mismatch")
                except (TypeError, ValueError):
                    self._error(400, "a matching qualified native task contract is required")
                    return None, None
                self._expected_task_binding = binding
            return (self._authenticated(), body) if body is not None else (None, None)
        identity = self._authenticated()
        return (identity, self._read_json()) if identity is not None else (None, None)

    def _source_task_kwargs(self, *, refresh=False):
        if self.server.source_authority is None:
            return {}
        result = {"source_access": self._source_access}
        if refresh:
            authority = self.server.source_authority
            token = self.headers["Authorization"][len("Bearer ") :].strip()
            result["source_refresh"] = lambda: authority.authenticate(token)
        return result

    # -- routes

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        if path in ("/health", "/health/"):
            self._send(200, b"ok\n", "text/plain")
        elif path in ("/status", "/status/"):
            self._call(self._status)
        elif path == "/statistics":
            self._call(self._statistics)
        elif path == "/healthz":
            self._call(self._healthz)
        elif path == "/settings":
            self._call(self._settings)
        elif path == "/api/observation":
            repo = next(iter(self.server.config.repositories.values()), None)
            if repo is None:
                self._send(404, b"")
                return
            self._feed_reply("active-batches", repo, query)
        elif match := REPOSITORY_ROUTE.match(path):
            repo = self.server.config.repositories.get(int(match.group(1)))
            if repo is None:
                self._send(404, b"")
                return
            self._feed_reply(match.group(2), repo, query)
        else:
            self._error(404, "no such route")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path
        if path == "/webhook/forgejo":
            self._native_webhook()
            return
        if self.server.source_authority is not None:
            refused = {
                "/webhook/github": (404, "GitHub webhooks are not a native source authority"),
                "/admin/import-bors": (400, "unqualified Bors imports are unavailable for a native source"),
                "/admin/reconcile-approvals": (503, "qualified native approval reconciliation is not available"),
                "/admin/revoke-identity": (400, "native caller authority is read fresh; revoke access at the source"),
            }.get(path)
            if refused is not None:
                self.close_connection = True
                self._error(*refused)
                return
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
        if path == "/admin/primary":
            self._admin_primary()
            return
        if path == "/admin/retry-ci":
            self._admin_retry_ci()
            return
        if path == "/admin/rebuild-head":
            self._admin_rebuild_head()
            return
        if path == "/admin/import-bors":
            self._admin_import_bors()
            return
        if path == "/admin/reconcile-approvals":
            self._admin_reconcile_approvals()
            return
        if path == "/admin/revoke-identity":
            self._admin_revoke_identity()
            return
        if path == "/tasks/inference":
            self._task_inference()
            return
        self._error(404, "no such route")

    def _counters(self) -> dict:
        adapter = self.server.config.adapter
        client = getattr(adapter, "client", None)
        receiver = self.server.native_receiver or self.server.receiver
        return {
            "github": client.stats() if client is not None and hasattr(client, "stats") else {},
            "identities": self.server.identity_status(),
            "webhooks": {
                "received": receiver.received if receiver else 0,
                "duplicates": receiver.duplicates if receiver else 0,
                "ignored": receiver.ignored if receiver else 0,
            },
        }

    def _status(self, *, wait: float | None = None) -> dict:
        """The status document: the entrypoint's provider (runtime envelope around the controller's
        status) when given, else the controller's status with the service facts added. `wait` bounds
        the wait for the controller lock (`Controller.status`) where the provider or controller takes it."""
        provider = self.server.status_provider
        if provider is not None:
            status = dict(provider(wait=wait) if wait is not None and _accepts_wait(provider) else provider())
        else:
            controller = self.server.controller
            status_of = getattr(controller, "status", None)
            if status_of is None:
                status = {}
            elif wait is not None and _accepts_wait(status_of):
                status = dict(status_of(wait=wait) or {})
            else:
                status = dict(status_of() or {})
            status.update({"revision": self.server.revision, "started_at": feeds._iso(self.server.started_at)})
        status.update(self._counters())
        return status

    def _healthz(self) -> dict:
        # The liveness probe must answer within a second even while a tick holds the controller lock.
        status = self._status(wait=HEALTHZ_LOCK_WAIT_SECONDS)
        return {
            "ok": True,
            "schema": status.get("schema"),
            "service": status.get("service"),
            "revision": status.get("revision", (status.get("service") or {}).get("revision")),
        }

    def _settings(self) -> dict:
        controller = self.server.controller
        status = (controller.status() if hasattr(controller, "status") else None) or {}
        return {"settings": dict(status.get("settings") or {}), "source": "controller"}

    def _admin(self, login: str) -> bool:
        """The admin predicate the security audit uses: repository-admin permission for the caller.

        The answer is cached per login (and persisted with the bearer identities) with the bearer
        cache's lifetime and served stale while GitHub cannot answer; a login never checked before
        is refused (503) during such an outage. Sends the error response itself when refusing.
        """
        if self.server.source_authority is not None:
            try:
                access = self.server.source_authority.validate(getattr(self, "_source_access", None))
                self.server.source_authority.require_admin(access)
            except SourceAuthorityUnavailable as error:
                log.warning("native source admin authority unavailable")
                self._error(503, str(error))
                return False
            except SourceAccessDenied as error:
                log.warning("native source admin authority denied")
                self._error(403, str(error))
                return False
            return True
        repo = next(iter(self.server.config.repositories.values()), None)
        if not repo:
            self._error(403, f"{login} is not a repository admin")
            return False
        try:
            admin = bool(self.server.admins.get(login, login, repo))
        except GitHubUnavailable:
            self._error(503, f"GitHub could not confirm whether {login} is a repository admin")
            return False
        if not admin:
            self._error(403, f"{login} is not a repository admin")
        return admin

    def _statistics(self) -> dict:
        from .statistics import snapshot

        controller = self.server.controller
        if self.server.source_authority is not None or not hasattr(controller, "store"):
            raise ValueError("controller ledger statistics are unavailable")
        repository = getattr(controller, "repository", None)
        if repository not in self.server.config.repositories.values():
            raise ValueError("repository statistics are not public")
        return snapshot(controller.store.path, repository, controller.canonical_branch, now=self.server.config.clock())

    def _feed_reply(self, which: str, repo: str, query: dict) -> None:
        if self.server.source_authority is not None:
            self._error(503, "qualified native source feeds are not available")
            return
        source = feeds.feed_source(self.server.controller)
        # Only the native controller guarantees retained, frozen admissions.
        # The legacy JSON schema and imported FeedSource behavior stay intact.
        headers = {}
        if which == "active-batches" and isinstance(source, feeds.ControllerFeedSource):
            headers["TauCeti-Admission"] = "frozen-accepted-head/v1"
        self._call(lambda: self._feed(which, repo, query, source=source), headers=headers)

    def _feed(self, which: str, repo: str, query: dict, *, source=None) -> dict:
        if source is None:
            source = feeds.feed_source(self.server.controller)
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
        if self.server.source_authority is not None:
            self._error(404, "GitHub webhooks are not a native source authority")
            return
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
        except Exception:  # noqa: BLE001 - failed deliveries need explicit redelivery or reconciliation
            log.error("webhook %s %s failed:\n%s", delivery.event, delivery.delivery_id, traceback.format_exc())
            self._error(500, "delivery failed")
            return
        self._json(202, {"result": disposition})

    def _native_webhook(self):
        receiver = self.server.native_receiver
        if receiver is None:
            self._error(404, "native source notifications are unavailable")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "invalid native notification length")
            self.close_connection = True
            return
        if not 0 <= length <= 1024 * 1024:
            self._error(413, "native notification exceeds one MiB")
            self.close_connection = True
            return
        body = self._read_body()
        if body is None:
            return
        try:
            result = receiver.handle(
                body,
                event=self.headers.get("X-Forgejo-Event", ""),
                delivery=self.headers.get("X-Forgejo-Delivery", ""),
                signature=self.headers.get("X-Forgejo-Signature", ""),
            )
        except PermissionError as error:
            self._error(401, str(error))
        except ValueError as error:
            self._error(400, str(error))
        except Exception:
            log.exception("native source invalidation failed")
            self._error(503, "source invalidation was not recorded; redeliver")
        else:
            self._json(202, {"result": result})

    def _worker_identity(self, body: dict) -> str:
        return str(body.get("worker") or self.headers.get("X-TauCeti-Worker") or "")

    def _task_claim(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        login = identity[0]
        kwargs = self._source_task_kwargs()
        if self.server.source_authority is not None:
            kwargs["expected_binding"] = self._expected_task_binding
        elif "task_schemas" in body:
            schemas = body["task_schemas"]
            if (
                not isinstance(schemas, list)
                or len(schemas) > 8
                or any(not isinstance(schema, str) for schema in schemas)
            ):
                self._error(400, "task_schemas must be a bounded list of schema names")
                return
            from .mixed_tasks import SCHEMA

            if SCHEMA in schemas:
                kwargs["mixed_capable"] = True
        self._call(lambda: self.server.controller.claim_task(self._worker_identity(body), login, **kwargs))

    def _task(self, task_id: str, action: str) -> None:
        """`renew`, `result` and `release` carry the claim token the claim handed out; the
        controller's task API (`renew_task(task_id, claim_token)`, `submit_task(task_id, claim_token,
        tip_sha, worker_notes)`, `release_task(task_id, claim_token, reason, error)`) checks it."""
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        token = str(body.get("claim_token") or "")
        if not token:
            self._error(400, "`claim_token` is required")
            return
        tid = int(task_id) if task_id.isdigit() else task_id
        controller = self.server.controller
        if action == "renew":
            self._call(
                lambda: _renewal(
                    tid, controller.renew_task(tid, token, **self._source_task_kwargs(refresh=True)), controller
                )
            )
        elif action == "release":
            self._call(
                lambda: controller.release_task(
                    tid,
                    token,
                    str(body.get("reason") or "released"),
                    str(body.get("error") or ""),
                    **self._source_task_kwargs(),
                )
            )
        else:
            tip = str(body.get("tip_sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", tip):
                self._error(400, "`tip_sha` must be a full commit SHA")
                return
            self._call(
                lambda: controller.submit_task(
                    tid,
                    token,
                    tip,
                    str(body.get("worker_notes") or ""),
                    **({"checkpoint": True} if action == "checkpoint" else {}),
                    **self._source_task_kwargs(refresh=True),
                )
            )

    def _admin_rebuild_head(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if not self._admin(identity[0]):
            return
        repo = str(body.get("repo") or next(iter(self.server.config.repositories.values()), ""))
        if self.server.source_authority is not None and repo != self.server.source_authority.source.repository.name:
            self._error(400, "`repo` must be the configured native source")
            return
        self._call(
            lambda: self.server.controller.rebuild_head(repo, identity[0], **self._source_task_kwargs(refresh=True))
        )

    def _admin_primary(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None or not self._admin(identity[0]):
            return
        if (
            set(body) != {"primary"}
            or not isinstance(body["primary"], str)
            or body["primary"] not in {"github", "forgejo"}
        ):
            self._error(400, "body must contain primary: github or forgejo")
            return
        self._call(lambda: self.server.controller.change_primary(body["primary"], identity[0]))

    def _admin_retry_ci(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if not self._admin(identity[0]):
            return
        repo = str(body.get("repo") or next(iter(self.server.config.repositories.values()), ""))
        if self.server.source_authority is not None and repo != self.server.source_authority.source.repository.name:
            self._error(400, "`repo` must be the configured native source")
            return
        request_id = body.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
                self._error(400, "`request_id` must be the exact 32-character request nonce")
                return
            self._call(
                lambda: self.server.controller.retry_ci(
                    repo, identity[0], request_id=request_id, **self._source_task_kwargs(refresh=True)
                )
            )
        else:
            self._call(
                lambda: self.server.controller.retry_ci(repo, identity[0], **self._source_task_kwargs(refresh=True))
            )

    def _task_inference(self) -> None:
        """A worker reports whether an inference call succeeded (`report_inference`), for the hold."""
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        ok = body.get("ok")
        if not isinstance(ok, bool):
            self._error(400, "`ok` must be a boolean")
            return
        self._call(
            lambda: self.server.controller.report_inference(
                self._worker_identity(body), ok, str(body.get("error") or "")
            )
        )

    def _admin_settings(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if not self._admin(identity[0]):
            return
        self._call(lambda: self._apply_settings(identity[0], body))

    def _apply_settings(self, login: str, body: dict) -> dict:
        """`POST /admin/settings` with a JSON object `{"<key>": <value>, ...}`: every key of the
        controller's settings (`DEFAULT_SETTINGS`) may be set, each value of the type of its initial
        value. Unknown keys are refused before any value is applied."""
        controller = self.server.controller
        if hasattr(controller, "set_setting"):
            if hasattr(controller, "effective_settings"):
                known = controller.effective_settings()
                unknown = sorted(key for key in body if key not in known)
                if unknown:
                    raise ValueError(f"unknown setting {', '.join(unknown)}; known: {', '.join(sorted(known))}")
            for key, value in body.items():
                controller.set_setting(key, value, login)
            status = controller.status() if hasattr(controller, "status") else {}
            return {"settings": dict((status or {}).get("settings") or {}), "actor": login}
        return controller.update_settings(login, body)

    def _admin_urgent(self) -> None:
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        repo = str(body.get("repo") or next(iter(self.server.config.repositories.values()), ""))
        pr = body.get("pr")
        if repo not in self.server.config.repositories.values() or not isinstance(pr, int) or pr <= 0:
            self._error(400, "`repo` must be a served repository and `pr` a positive integer")
            return
        if not self._admin(identity[0]):
            return
        self._call(lambda: self.server.controller.mark_urgent(repo, pr, identity[0]))

    def _admin_import_bors(self) -> None:
        """Bors's state at the cutover: `{"observation": feed, "candidates": journal | [journals], "dry_run", "release_head"}`."""
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if self.server.source_authority is not None:
            self._error(400, "unqualified Bors imports are unavailable for a native source")
            return
        observation = body.get("observation")
        if not isinstance(observation, dict):
            self._error(400, "`observation` must be the observation feed document")
            return
        candidates = body.get("candidates")
        if candidates is not None and not isinstance(candidates, dict | list):
            self._error(400, "`candidates` must be a candidate journal document or a list of them")
            return
        if not self._admin(identity[0]):
            return
        self._call(
            lambda: self.server.controller.import_bors_state(
                observation,
                candidates,
                dry_run=bool(body.get("dry_run")),
                release_head=bool(body.get("release_head")),
            )
        )

    def _admin_reconcile_approvals(self) -> None:
        """One approval reconciliation pass now (`tauceti-queue reconcile-approvals`)."""
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if not self._admin(identity[0]):
            return
        if self.server.source_authority is not None:
            self._error(503, "qualified native approval reconciliation is not available")
            return
        poller = self.server.poller
        if poller is None:
            self._error(503, "the approval poller is not running in this service")
            return
        request = getattr(poller, "request_discovery", None)
        if callable(request):
            request()
            self._json(200, {"requested": True, **poller.state.summary()})
            return
        self._call(lambda: poller.run_once(reason=f"operator {identity[0]}"))

    def _admin_revoke_identity(self) -> None:
        """Forget what the service cached about a login (its bearer tokens and admin answers), so
        the next request with any of its tokens asks GitHub again and an outage cannot serve them."""
        identity, body = self._authenticated_json()
        if identity is None or body is None:
            return
        if self.server.source_authority is not None:
            self._error(400, "native caller authority is read fresh; revoke access at the source")
            return
        login = body.get("login")
        if not isinstance(login, str) or not login:
            self._error(400, "`login` must be a GitHub login")
            return
        if not self._admin(identity[0]):
            return

        def revoke() -> dict:
            tokens = self.server.auth.revoke_login(login)
            admin = [cache.invalidate(login) for cache in self.server.identity_caches()[1:]]
            log.warning("%s revoked the cached identity of %s (%d tokens)", identity[0], login, tokens)
            return {"login": login, "tokens_forgotten": tokens, "admin_answers_forgotten": sum(admin)}

        self._call(revoke)


def _renewal(task_id, result, controller=None) -> dict:  # noqa: E302
    result = dict(result) if isinstance(result, dict) else {"task_id": task_id, "expires_at": result}
    policy = getattr(controller, "task_check_policy", None)
    if callable(policy):
        result.update(policy(task_id))
    return result


def server_config(config, *, adapter=None, app=None, controller=None) -> ServerConfig:
    """A `ServerConfig` from the service's `tauceti_queue.config.Config` (the entrypoint's form).

    The webhook secret comes from the loaded App when given, else from `credentials_dir`; the
    adapter is the one passed, else the controller's `github`; repository id 1 is the configured
    repository, as it is for Bors.
    """
    if isinstance(config, ServerConfig):
        return config
    adapter = adapter or getattr(controller, "github", None)
    if adapter is None:
        raise ValueError("create_server needs the GitHub adapter (`adapter=`) or a controller with `.github`")
    if app is not None:
        secret = app.webhook_secret
    else:
        secret = (pathlib.Path(config.credentials_dir).expanduser() / "github-webhook-secret").read_bytes().strip()
    return ServerConfig(
        webhook_secret=secret,
        repositories={1: config.repository},
        adapter=adapter,
        host=config.listen_host,
        port=config.listen_port,
        review_app_id=int(getattr(config, "review_app_id", webhook.REVIEW_APP_ID) or webhook.REVIEW_APP_ID),
    )


def create_server(
    controller, config, *, status: Callable[[], dict] | None = None, adapter=None, app=None, poller=None
) -> QueueHTTPServer:
    """A server ready for `serve_forever()`; `server_address` carries the bound port.

    `config` is a `ServerConfig` or the service `Config`; `status` is the entrypoint's status
    document provider (`Service.status_document`), used for `/status` and `/healthz` when given;
    `poller` is the `approval_poll.ApprovalPoller` behind `/admin/reconcile-approvals`.
    """
    resolved = server_config(config, adapter=adapter, app=app, controller=controller)
    server = QueueHTTPServer((resolved.host, resolved.port), Handler, controller, resolved, status)
    server.poller = poller
    return server


def serve_in_thread(server: QueueHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="tauceti-queue-http", daemon=True)
    thread.start()
    return thread

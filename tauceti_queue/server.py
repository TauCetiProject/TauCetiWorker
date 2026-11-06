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
from .stale_cache import MAX_AGE_SECONDS, StaleCache

log = logging.getLogger("tauceti_queue.server")

MAX_BODY = 25 * 1024 * 1024
REPOSITORY_ROUTE = re.compile(r"^/repositories/(\d+)/(active-batches|candidates)$")
TASK_ROUTE = re.compile(r"^/tasks/([A-Za-z0-9_.:-]+)/(renew|result|release)$")
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
        super().__init__(address, handler)
        self.controller = controller
        self.config = config
        self.status_provider = status
        self.started_at = config.clock()
        self.receiver = webhook.WebhookReceiver(
            controller, config.adapter, repositories=config.repositories.values(), review_app_id=config.review_app_id
        )
        store = config.store if config.store is not None else getattr(controller, "store", None)
        self.identity_store = store
        self.auth = BearerAuth(
            config.adapter,
            ttl=config.auth_cache_seconds,
            clock=config.clock,
            store=store,
            max_age=config.identity_max_age_seconds,
        )
        self.admins = StaleCache(
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
        self.revision = config.revision or running_revision()
        self.poller = None  # the approval poller, set by `create_server(..., poller=)`
        self._revalidator_stop = threading.Event()

    def identity_caches(self) -> list[StaleCache]:
        """The caches of GitHub's answers about callers: bearer identities, the server's admin
        answers and the controller's (`Controller.admins`) when it keeps one."""
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
        if self.config.identity_revalidate_seconds > 0:
            threading.Thread(target=self._revalidate_loop, name="identity-revalidate", daemon=True).start()
        super().serve_forever(poll_interval)

    def shutdown(self) -> None:
        self._revalidator_stop.set()
        super().shutdown()

    def identity_status(self) -> dict:
        """`/status`'s `identities`: how many answers are cached and whether GitHub can be asked."""
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

    def _call(self, action: Callable[[], object], *, headers: dict | None = None) -> None:
        """Run a controller action and map its outcome to a reply."""
        try:
            result = action()
        except PermissionError as error:
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
        elif path in ("/status", "/status/"):
            self._call(self._status)
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
        return {
            "github": client.stats() if client is not None and hasattr(client, "stats") else {},
            "identities": self.server.identity_status(),
            "webhooks": {
                "received": self.server.receiver.received,
                "duplicates": self.server.receiver.duplicates,
                "ignored": self.server.receiver.ignored,
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

    def _feed_reply(self, which: str, repo: str, query: dict) -> None:
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
        """`renew`, `result` and `release` carry the claim token the claim handed out; the
        controller's task API (`renew_task(task_id, claim_token)`, `submit_task(task_id, claim_token,
        tip_sha, worker_notes)`, `release_task(task_id, claim_token, reason, error)`) checks it."""
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        token = str(body.get("claim_token") or "")
        if not token:
            self._error(400, "`claim_token` is required")
            return
        tid = int(task_id) if task_id.isdigit() else task_id
        controller = self.server.controller
        if action == "renew":
            self._call(lambda: _renewal(tid, controller.renew_task(tid, token)))
        elif action == "release":
            self._call(
                lambda: controller.release_task(
                    tid, token, str(body.get("reason") or "released"), str(body.get("error") or "")
                )
            )
        else:
            tip = str(body.get("tip_sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", tip):
                self._error(400, "`tip_sha` must be a full commit SHA")
                return
            self._call(lambda: controller.submit_task(tid, token, tip, str(body.get("worker_notes") or "")))

    def _admin_rebuild_head(self) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        if not self._admin(identity[0]):
            return
        repo = str(body.get("repo") or next(iter(self.server.config.repositories.values()), ""))
        self._call(lambda: self.server.controller.rebuild_head(repo, identity[0]))

    def _admin_retry_ci(self) -> None:
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        if not self._admin(identity[0]):
            return
        repo = str(body.get("repo") or next(iter(self.server.config.repositories.values()), ""))
        self._call(lambda: self.server.controller.retry_ci(repo, identity[0]))

    def _task_inference(self) -> None:
        """A worker reports whether an inference call succeeded (`report_inference`), for the hold."""
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
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
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
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
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
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
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
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
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
            return
        if not self._admin(identity[0]):
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
        identity = self._authenticated()
        if identity is None:
            return
        body = self._read_json()
        if body is None:
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


def _renewal(task_id, result) -> dict:  # noqa: E302
    if isinstance(result, dict):
        return result
    return {"task_id": task_id, "expires_at": result}


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

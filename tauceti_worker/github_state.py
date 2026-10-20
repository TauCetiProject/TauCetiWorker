"""Client for the shared state service. Age is telemetry, never a reason to bypass it.

The direct facade owns fallback: an unavailable service result cannot become an empty history.
No installation credentials leave the server. Conditional reads always contact it, including force.
"""

from __future__ import annotations

import contextvars
import hashlib
import http.client
import json
import math
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import github_transport as transport
from .paths import ensure_ssl_cert_file


class StateQueryError(ValueError):
    pass


DEPENDENCY = "github-state"
# Reasons a caller can act on. Authentication/configuration needs an operator; the rest recover.
ACTIONABLE = frozenset({"client_auth_configuration"})
_last_failure: contextvars.ContextVar[dict | None] = contextvars.ContextVar("github_state_failure", default=None)


class _Failure(OSError):
    def __init__(self, reason: str, detail: str = "", *, status: int | None = None, retry_after: float | None = None):
        super().__init__(reason)
        self.reason, self.detail, self.status, self.retry_after = reason, detail, status, retry_after


def last_failure() -> dict | None:
    """The structured cause of this context's most recent unavailable read, or None after success."""
    return _last_failure.get()


_SECRET_FIELD = re.compile(
    r"(?i)\b(authorization|proxy-authorization|bearer|basic|token|access_token|password|secret|api[_-]?key|cookie)\b"
    r"[\"']?\s*[:=]?\s*(?:(?:bearer|basic|token)\s+)?[\"']?[^\s\"',;}]+"
)
# Opaque credential-shaped runs (GitHub tokens, JWTs, base64 blobs); diagnostics never need them.
_OPAQUE = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")


def _sanitize(text, *secrets) -> str:
    """Bounded printable diagnostics with credential fields and credential-shaped values removed."""
    text = str(text or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _SECRET_FIELD.sub(lambda m: m.group(1) + " [redacted]", text)
    text = _OPAQUE.sub("[redacted]", text)
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", text)).strip()[:200]


MAX_RETRY_AFTER = 300.0


def _retry_after(value) -> float | None:
    """A server's Retry-After seconds, finite and capped so a bad header cannot park recovery."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER) if math.isfinite(seconds) else None


def _classify(error, token) -> _Failure:
    """Map one attempt's exception to a bounded, credential-free reason."""
    if isinstance(error, _Failure):
        return error
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read(512).decode("utf-8", "replace")
            message = json.loads(body).get("error", body) if body.lstrip().startswith("{") else body
        except Exception:
            message = ""
        finally:
            error.close()
        retry_after = _retry_after((error.headers or {}).get("Retry-After"))
        return _Failure(
            f"http_{error.code}", _sanitize(message or error.reason, token), status=error.code, retry_after=retry_after
        )
    if isinstance(error, urllib.error.URLError):
        inner = error.reason
        if isinstance(inner, (TimeoutError, socket.timeout)) or "timed out" in str(inner):
            return _Failure("timeout", _sanitize(inner, token))
        return _Failure("connection", _sanitize(inner, token))
    if isinstance(error, (TimeoutError, socket.timeout)):
        return _Failure("timeout", _sanitize(error, token))
    if isinstance(error, (ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return _Failure("connection", _sanitize(type(error).__name__, token))
    if isinstance(error, http.client.HTTPException):
        return _Failure("invalid_response", _sanitize(type(error).__name__, token))
    if isinstance(error, OSError):
        return _Failure("connection", _sanitize(error, token))
    return _Failure("invalid_response", _sanitize(type(error).__name__, token))


def _unavailable(repo, operation, reason, detail="", *, status=None, retry_at=None, circuit="closed", cached=None):
    record = {
        "dependency": DEPENDENCY,
        "repository": repo,
        "operation": operation,
        "reason": reason,
        "detail": detail,
        "status": status,
        "retry_at": retry_at,
        "circuit": circuit,
        "actionable": reason in ACTIONABLE,
    }
    if cached:
        # Provenance of the last complete response; it supports planning, never current-state claims.
        payload = cached["payload"]
        record["cached"] = {
            "revision": payload.get("revision"),
            "observed_at": payload.get("observed_at"),
            "verified_at": payload.get("verified_at"),
            "stored_at": cached.get("stored_at"),
        }
    _last_failure.set(record)
    transport.event(
        {
            "kind": "gateway_fallback",
            "operation": operation,
            "reason": "circuit_open" if circuit == "open" else reason,
            **({"cause": reason} if circuit == "open" else {}),
            **({"status": status} if status else {}),
            **({"detail": detail} if detail else {}),
        }
    )
    return False, None


def _backoff(failures: int) -> float:
    """Bounded, jittered open interval so machines sharing one service do not probe in lockstep."""
    return min(300.0, 30.0 * (1 << min(max(failures - 3, 0), 4))) * random.uniform(0.8, 1.2)


def _url() -> str | None:
    value = os.environ.get("TAUCETI_GITHUB_STATE_URL", "").rstrip("/")
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise StateQueryError("GitHub state service requires HTTPS (HTTP is allowed for localhost)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise StateQueryError("GitHub state URL must not contain credentials, query or fragment")
    return value


def _wire(data, operation):
    if operation in ("issue_comments", "review_comments", "reactions") and isinstance(data, list):
        for row in data:
            for field in ("id", "in_reply_to_id"):
                if row.get(field) is not None:
                    row[field] = int(row[field])
    return data


def _cached(path: Path, binding: dict):
    """The last completely assembled response for exactly this query and client identity."""
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(cached, dict)
        or cached.get("query") != binding
        or not isinstance(cached.get("payload"), dict)
        or cached["payload"].get("status") != "ok"
        or "revision" not in cached["payload"]
        or "cursor" in cached["payload"]
    ):
        return None  # Unbound, partial or legacy entries never answer a conditional read.
    return cached


PROBE_LEASE = 15.0


def _circuit_update(circuit: Path, change, *args) -> None:
    """Apply one serialized circuit transition; local storage failure never blocks a read."""
    try:
        with transport.locked(circuit.with_suffix(".lock"), timeout=1):
            try:
                state = json.loads(circuit.read_text())
            except (OSError, ValueError):
                state = {}
            new = change(state, *args)
            if new is not state:
                transport.atomic(circuit, new)
    except OSError:
        pass


def _renew(state, probe):
    return {**state, "probe_until": time.time() + PROBE_LEASE} if state.get("probe") == probe else state


def _recovered(state, started, probe, key):
    if state.get("failed_at", 0) > started and (probe is None or state.get("probe") != probe):
        return state  # A request that began before a newer recorded failure is not evidence of recovery.
    now = time.time()
    cooling = {k: v for k, v in state.get("coverage_until", {}).items() if k != key and v > now}
    return {
        "failures": 0,
        "until": 0,
        **({"coverage_until": cooling, "coverage_reason": state.get("coverage_reason", "")} if cooling else {}),
    }


def _cooldown(state, operation, key, until, reason):
    if operation != "survey":
        return state
    now = time.time()
    active = {k: v for k, v in state.get("coverage_until", {}).items() if v > now}
    return {**state, "coverage_until": {**active, key: until}, "coverage_reason": reason}


def read(repo: str, operation: str, *, _allow_observed=False, _with_metadata=False, _optional=False, **args):
    """Return (available, data). Failed transport/config/coverage falls back; bad queries raise.

    An unavailable result records its structured cause for `last_failure()`.
    """
    if _allow_observed and operation != "workflow_jobs":
        raise StateQueryError("observed coverage is available only for workflow jobs")
    began = time.monotonic()
    _last_failure.set(None)
    if transport.direct_reads():
        return False, None
    url = _url()
    if not url:
        return False, None
    ensure_ssl_cert_file()
    try:
        token = Path(os.environ["TAUCETI_GITHUB_STATE_TOKEN_FILE"]).read_text().strip()
        if not token:
            raise ValueError("empty client token")
    except (KeyError, OSError, ValueError) as error:
        detail = "TAUCETI_GITHUB_STATE_TOKEN_FILE is not set" if isinstance(error, KeyError) else type(error).__name__
        return _unavailable(repo, operation, "client_auth_configuration", detail)
    identity = hashlib.sha256((url + "\0" + token).encode()).hexdigest()
    base = transport.root() / "gateway" / identity
    binding = {"identity": identity, "repository": repo, "operation": operation, "args": args}
    if _allow_observed:
        binding["allow_observed"] = True
    key = hashlib.sha256(
        json.dumps([repo, operation, args, bool(_allow_observed)], sort_keys=True).encode()
    ).hexdigest()
    cached_path = base / "responses" / (key + ".json")
    cached = _cached(cached_path, binding)
    probe = None
    started = time.time()
    # An unsupported repository/auth scope must not poison healthy TauCeti reads.
    circuit = base / "circuits" / (hashlib.sha256(repo.encode()).hexdigest() + ".json")
    try:
        with transport.locked(circuit.with_suffix(".lock"), timeout=1):
            try:
                state = json.loads(circuit.read_text())
            except (OSError, ValueError):
                state = {}
            now = time.time()
            if state.get("until", 0) > now or state.get("probe_until", 0) > now:
                # Keep the cause that opened the circuit; "circuit open" alone is not actionable.
                return _unavailable(
                    repo,
                    operation,
                    state.get("reason", "transport"),
                    state.get("detail", ""),
                    status=state.get("status"),
                    retry_at=max(state.get("until", 0), state.get("probe_until", 0)),
                    circuit="open",
                    cached=cached,
                )
            if operation == "survey" and state.get("coverage_until", {}).get(key, 0) > now:
                # The global inventory is backfilling; peers share one cooldown instead of each asking.
                return _unavailable(
                    repo,
                    operation,
                    "coverage_unavailable",
                    state.get("coverage_reason", ""),
                    retry_at=state["coverage_until"][key],
                    cached=cached,
                )
            if state.get("failures", 0) >= 3:
                # Only one process probes recovery; peers wait for that probe's lease, which the
                # prober renews page by page so a long healthy read never admits a second probe.
                probe = uuid.uuid4().hex
                transport.atomic(circuit, {**state, "probe": probe, "probe_until": now + PROBE_LEASE})
    except OSError:
        state = {}
    deadline = time.monotonic() + 5
    last = _Failure("transport")
    for attempt in range(2):
        rows, cursor, cursors, revision = [], None, set(), None
        unchanged = False
        try:
            while True:
                params = {"repository": repo, "operation": operation, "args": json.dumps(args, separators=(",", ":"))}
                if _allow_observed:
                    params["allow_observed"] = "true"
                if cursor:
                    params["cursor"] = cursor
                headers = {
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
                }
                if cached and not cursor:
                    headers["If-None-Match"] = cached.get("etag", "")
                request = urllib.request.Request(url + "/v1/query?" + urllib.parse.urlencode(params), headers=headers)
                try:
                    with urllib.request.urlopen(
                        request, timeout=max(0.1, min(2.4, deadline - time.monotonic()))
                    ) as response:
                        try:
                            payload = json.load(response)
                        except ValueError:
                            raise _Failure("invalid_response", "malformed JSON") from None
                        etag = response.headers.get("ETag")
                except urllib.error.HTTPError as error:
                    if error.code == 304 and cached and not cursor:
                        payload = cached["payload"]
                        etag = cached.get("etag")
                        unchanged = True
                    elif error.code == 400:
                        if _optional:
                            return _unavailable(
                                repo, operation, "incompatible_protocol", "unsupported service version", status=400
                            )
                        raise StateQueryError(
                            "state service rejected the query; check the client/service protocol versions"
                        ) from None
                    elif error.code in (401, 403):
                        raise _Failure(
                            "client_auth_configuration", _classify(error, token).detail, status=error.code
                        ) from None
                    else:
                        raise _classify(error, token) from None
                if not isinstance(payload, dict):
                    raise _Failure("invalid_response", "response is not an object")
                if (
                    payload.get("version") != 1
                    or payload.get("repository") != repo
                    or payload.get("operation") != operation
                ):
                    raise _Failure("incompatible_protocol", "service protocol mismatch")
                accepted_observation = (
                    _allow_observed
                    and payload.get("coverage") == "webhook_observed"
                    and payload.get("complete") is False
                )
                if payload.get("status") != "ok" or not (payload.get("complete") is True or accepted_observation):
                    # Unknown coverage is never an empty inventory, and a partial page is never cached.
                    detail = _sanitize(payload.get("reason", ""), token)
                    if detail == "repository_unavailable":
                        # The service does not serve this repository to this client: a scope/config fix.
                        return _unavailable(repo, operation, "client_auth_configuration", detail, cached=cached)
                    retry_at = time.time() + 30 * random.uniform(0.8, 1.2)
                    _circuit_update(circuit, _cooldown, operation, key, retry_at, detail)
                    return _unavailable(
                        repo, operation, "coverage_unavailable", detail, retry_at=retry_at, cached=cached
                    )
                if revision is not None and revision != payload["revision"]:
                    raise _Failure("mixed_revision", "service snapshot changed during pagination")
                revision = payload["revision"]
                data = payload["data"]
                if isinstance(data, list):
                    rows.extend(data)
                elif cursor:
                    raise _Failure("invalid_response", "invalid paginated scalar")
                next_cursor = payload.get("cursor")
                if not next_cursor:
                    result = rows if isinstance(data, list) else data
                    complete = {**payload, "data": result}
                    complete.pop("cursor", None)
                    try:
                        if unchanged:
                            os.utime(cached_path, None)
                        else:
                            transport.atomic(
                                cached_path,
                                {"etag": etag, "payload": complete, "query": binding, "stored_at": time.time()},
                            )
                    except OSError:
                        pass  # A healthy mirror remains usable when the local cache is unavailable.
                    _circuit_update(circuit, _recovered, started, probe, key)
                    transport.event(
                        {
                            "kind": "gateway_read",
                            "operation": operation,
                            "attempt": attempt,
                            "revision": revision,
                            "coverage": payload.get("coverage", "complete"),
                            "elapsed_ms": round((time.monotonic() - began) * 1000),
                            "observed_at": payload.get("observed_at"),
                            "verified_at": payload.get("verified_at"),
                        }
                    )
                    _last_failure.set(None)
                    wire = _wire(result, operation)
                    return True, {**complete, "data": wire} if _with_metadata else wire
                if next_cursor in cursors or time.monotonic() >= deadline:
                    raise _Failure("pagination", "repeated cursor or stalled pagination")
                if len(cursors) >= 10000:
                    raise _Failure("pagination", "page bound exceeded")
                # A responsive server with a large, stable snapshot is operable.
                # Bound each request and stalled interval rather than treating
                # a healthy multi-page response as an outage after five seconds.
                deadline = time.monotonic() + 5
                if probe:
                    _circuit_update(circuit, _renew, probe)
                cursors.add(next_cursor)
                cursor = next_cursor
        except StateQueryError:
            raise
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            http.client.HTTPException,
            urllib.error.URLError,
        ) as error:
            last = _classify(error, token)
            if time.monotonic() >= deadline or last.reason == "client_auth_configuration":
                break
    retry_at = time.time() + max(last.retry_after or 0, 5 * random.uniform(0.8, 1.2))

    def failed(state):
        nonlocal retry_at
        failures = state.get("failures", 0) + 1
        now = time.time()
        until = now + max(_backoff(failures), last.retry_after or 0) if failures >= 3 else 0
        retry_at = until or retry_at
        # A failure from a request that is not the active probe leaves that probe's lease intact.
        lease = {k: state[k] for k in ("probe", "probe_until") if k in state and state.get("probe") != probe}
        return {
            **lease,
            "failures": failures,
            "until": until,
            "reason": last.reason,
            "detail": last.detail,
            "status": last.status,
            "since": state.get("since") or now,
            "failed_at": now,
        }

    _circuit_update(circuit, failed)
    return _unavailable(repo, operation, last.reason, last.detail, status=last.status, retry_at=retry_at, cached=cached)


def read_workflow_jobs(repo: str, run_id: int | str, *, allow_observed=False):
    """Return (available, result-with-coverage); observed jobs never imply a complete list."""
    return read(repo, "workflow_jobs", id=str(run_id), _allow_observed=allow_observed, _with_metadata=True)


class SurveyRows(list):
    """Compact rows with small inventory-wide counts for automatic authoring limits."""

    def __init__(self, rows, author_counts):
        super().__init__(rows)
        self.author_counts = author_counts


def read_survey(repo, roadmap="any", skip=(), *, numbers=(), stages=None):
    from .survey_facts import CONTRACT

    available, result = read(
        repo,
        "survey",
        roadmap=roadmap,
        skip=",".join(skip),
        numbers=",".join(map(str, numbers)),
        stages=",".join(stages if stages is not None else ("review", "fix", "rebase")),
        _optional=True,
        _with_metadata=True,
    )
    if not available:
        return False, None
    try:
        if result.get("contract") != CONTRACT:
            raise ValueError("incompatible survey contract")
        rows = result["data"]
        if not isinstance(rows, list):
            raise ValueError("invalid survey")
        for row in rows:
            if row["contract"] != CONTRACT or not isinstance(row["pr"], dict) or not isinstance(row["review"], dict):
                raise ValueError("incompatible survey facts")
            facts = row["review"]
            if (
                facts.get("meta") is not None
                and not isinstance(facts["meta"], dict)
                or not isinstance(facts.get("markers"), list)
                or not isinstance(facts.get("rebase_heads"), list)
            ):
                raise ValueError("invalid survey facts")
            if (
                type(row["pr"]["number"]) is not int
                or type(facts["issue_complete"]) is not bool
                or type(facts["review_complete"]) is not bool
            ):
                raise ValueError("invalid survey coverage")
            if facts.get("contest"):
                facts["contest"]["id"] = int(facts["contest"]["id"])
            pr = row["pr"]
            for field in ("author", "headRepository", "headRepositoryOwner"):
                if pr.get(field) is not None and not isinstance(pr[field], dict):
                    raise ValueError("invalid survey PR")
            for field in ("labels", "statusCheckRollup"):
                if not isinstance(pr.get(field, []), list) or any(
                    not isinstance(value, dict) for value in pr.get(field, [])
                ):
                    raise ValueError("invalid survey PR collection")
            if not isinstance(pr.get("target_focuses", []), list) or any(
                not isinstance(value, str) for value in pr.get("target_focuses", [])
            ):
                raise ValueError("invalid survey targets")
            for marker in facts["markers"]:
                if (
                    not isinstance(marker, dict)
                    or type(marker.get("expires_at")) is not int
                    or not isinstance(marker.get("providers"), list)
                    or any(not isinstance(provider, str) for provider in marker["providers"])
                ):
                    raise ValueError("invalid survey claim")
        counts = result.get("author_counts")
        if counts is not None and (
            not isinstance(counts, dict) or any(type(count) is not int or count < 0 for count in counts.values())
        ):
            raise ValueError("invalid survey author counts")
        return True, SurveyRows(rows, counts)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return _unavailable(repo, "survey", "incompatible_protocol", _sanitize(error))

"""Client for the shared state service. Age is telemetry, never a reason to bypass it.

The direct facade owns fallback: an unavailable service result cannot become an empty history.
No installation credentials leave the server. Conditional reads always contact it, including force.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import github_transport as transport
from .paths import ensure_ssl_cert_file


class StateQueryError(ValueError):
    pass


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


def read(repo: str, operation: str, *, _allow_observed=False, _with_metadata=False, _optional=False, **args):
    """Return (available, data). Failed transport/config/coverage falls back; bad queries raise."""
    if _allow_observed and operation != "workflow_jobs":
        raise StateQueryError("observed coverage is available only for workflow jobs")
    began = time.monotonic()
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
    except (KeyError, OSError, ValueError):
        transport.event({"kind": "gateway_fallback", "operation": operation, "reason": "client_auth_configuration"})
        return False, None
    identity = hashlib.sha256((url + "\0" + token).encode()).hexdigest()
    base = transport.root() / "gateway" / identity
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
                transport.event({"kind": "gateway_fallback", "operation": operation, "reason": "circuit_open"})
                return False, None
            if state.get("failures", 0) >= 3:
                # Only one process probes recovery; peers use direct access during that probe.
                transport.atomic(circuit, {**state, "probe_until": now + 6})
    except OSError:
        state = {}
    key = hashlib.sha256(
        json.dumps([repo, operation, args, bool(_allow_observed)], sort_keys=True).encode()
    ).hexdigest()
    cached_path = base / "responses" / (key + ".json")
    try:
        cached = json.loads(cached_path.read_text())
    except (OSError, ValueError):
        cached = None
    deadline = time.monotonic() + 5
    last = "transport"
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
                        payload = json.load(response)
                        etag = response.headers.get("ETag")
                except urllib.error.HTTPError as error:
                    if error.code == 304 and cached:
                        payload = cached["payload"]
                        etag = cached.get("etag")
                        unchanged = True
                    elif error.code == 400:
                        if _optional:
                            transport.event(
                                {
                                    "kind": "gateway_fallback",
                                    "operation": operation,
                                    "reason": "unsupported_service_version",
                                }
                            )
                            return False, None
                        raise StateQueryError(
                            "state service rejected the query; check the client/service protocol versions"
                        ) from None
                    elif error.code in (401, 403):
                        last = "client_auth_configuration"
                        raise OSError(last) from None
                    else:
                        raise
                if not isinstance(payload, dict):
                    raise OSError("invalid service response")
                if (
                    payload.get("version") != 1
                    or payload.get("repository") != repo
                    or payload.get("operation") != operation
                ):
                    raise OSError("service protocol mismatch")
                accepted_observation = (
                    _allow_observed
                    and payload.get("coverage") == "webhook_observed"
                    and payload.get("complete") is False
                )
                if payload.get("status") != "ok" or not (payload.get("complete") is True or accepted_observation):
                    transport.event(
                        {
                            "kind": "gateway_fallback",
                            "operation": operation,
                            "reason": payload.get("reason", "coverage_unavailable"),
                        }
                    )
                    return False, None
                if revision is not None and revision != payload["revision"]:
                    raise OSError("mixed service snapshot revisions")
                revision = payload["revision"]
                data = payload["data"]
                if isinstance(data, list):
                    rows.extend(data)
                elif cursor:
                    raise OSError("invalid paginated scalar")
                next_cursor = payload.get("cursor")
                if not next_cursor:
                    result = rows if isinstance(data, list) else data
                    complete = {**payload, "data": result}
                    complete.pop("cursor", None)
                    try:
                        if unchanged:
                            os.utime(cached_path, None)
                        else:
                            transport.atomic(cached_path, {"etag": etag, "payload": complete})
                        transport.atomic(circuit, {"failures": 0, "until": 0})
                    except OSError:
                        pass  # A healthy mirror remains usable when the local cache is unavailable.
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
                    wire = _wire(result, operation)
                    return True, {**complete, "data": wire} if _with_metadata else wire
                if next_cursor in cursors or time.monotonic() >= deadline:
                    raise OSError("service pagination failed")
                if len(cursors) >= 10000:
                    raise OSError("service pagination exceeded its page bound")
                # A responsive server with a large, stable snapshot is operable.
                # Bound each request and stalled interval rather than treating
                # a healthy multi-page response as an outage after five seconds.
                deadline = time.monotonic() + 5
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
        ):
            if time.monotonic() >= deadline or last == "client_auth_configuration":
                break
    try:
        with transport.locked(circuit.with_suffix(".lock"), timeout=1):
            try:
                state = json.loads(circuit.read_text())
            except (OSError, ValueError):
                state = {}
            failures = state.get("failures", 0) + 1
            transport.atomic(circuit, {"failures": failures, "until": time.time() + 30 if failures >= 3 else 0})
    except OSError:
        pass
    transport.event({"kind": "gateway_fallback", "operation": operation, "reason": last})
    return False, None


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
    except (ValueError, KeyError, TypeError, AttributeError):
        transport.event({"kind": "gateway_fallback", "operation": "survey", "reason": "survey_contract_mismatch"})
        return False, None

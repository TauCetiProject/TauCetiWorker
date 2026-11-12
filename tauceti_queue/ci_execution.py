"""Repository-ID-scoped, single-send GitHub execution for frozen source requests."""

from __future__ import annotations

import base64
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable, GitHubEvidence, NoRedirect
from tauceti_tools.forge import GITHUB

from . import github_app
from .github_adapter import UrllibTransport
from .ports import CandidateRun, DispatchNotAccepted
from .workflow_token import WorkflowTokenSource


@dataclass(frozen=True)
class CandidateObservation:
    run: CandidateRun
    ownership: dict | None
    result: dict | None
    result_absent: bool = False


@dataclass(frozen=True)
class PreparedDispatch:
    """Process-local preflight result; credentials are never persisted or logged."""

    request_digest: str
    repository_id: int
    token: str = field(repr=False)


class ExecutionEvidence(GitHubEvidence):
    def repository(self):
        req = urllib.request.Request(
            GITHUB.api_url + "/repos/" + self.policy.execution.repository.name,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "Cache-Control": "no-cache",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            value = json.loads(self._read(req, 1024 * 1024))
        except (ValueError, UnicodeError):
            raise EvidenceUnavailable("execution repository metadata is malformed") from None
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or value["id"] != self.policy.execution.native_id
            or value.get("full_name") != self.policy.execution.repository.name
        ):
            raise ValueError("execution repository identity changed")
        return value


class GitHubSourceExecution:
    def __init__(self, tokens, *, transport=None, reader_factory=None, clock=time.time, max_pages=10):
        if (
            not isinstance(tokens, (github_app.InstallationTokenSource, WorkflowTokenSource))
            or tokens.repository_id is None
            or tokens._api != github_app.API
            # For job tokens this is the explicit trusted bootstrap's permission
            # contract, not an assertion inferred from repository metadata.
            or tokens.permissions != {"contents": "write", "actions": "write"}
        ):
            raise ValueError("source execution requires a GitHub repository-ID-scoped token source")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("invalid execution discovery page bound")
        self.tokens, self.clock, self.max_pages = tokens, clock, max_pages
        self.transport = transport or UrllibTransport()
        opener = (
            None
            if reader_factory is not None
            else urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=github_app.ssl_context()),
                NoRedirect(),
            )
        )
        self.reader_factory = reader_factory or (
            lambda policy, token: ExecutionEvidence(policy, token=token, token_origin=GITHUB, opener=opener)
        )

    def _reader(self, frozen, *, token=None):
        if frozen.policy.execution.native_id != self.tokens.repository_id:
            raise ValueError("execution credential belongs to another repository ID")
        frozen.request.check_policy(frozen.policy)
        return self.reader_factory(frozen.policy, self.tokens.token() if token is None else token)

    def caller(self, enrolled_policy):
        """Read one immutable caller/configuration snapshot from protected main."""
        if (
            enrolled_policy.execution.native_id != self.tokens.repository_id
            or enrolled_policy.execution_branch != "main"
        ):
            raise ValueError("trusted caller differs from the enrolled execution repository/main")
        reader = self.reader_factory(enrolled_policy, self.tokens.token())
        metadata = reader.repository()
        if (metadata.get("id"), metadata.get("full_name"), metadata.get("default_branch")) != (
            enrolled_policy.execution.native_id,
            enrolled_policy.execution.repository.name,
            "main",
        ):
            raise ValueError("trusted caller repository identity or default branch changed")
        branch = reader.get("/branches/main")
        revision = branch.get("commit", {}).get("sha")
        if (
            branch.get("protected") is not True
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or revision == "0" * 40
        ):
            raise ValueError("trusted caller requires an immutable commit on protected main")

        def content(path):
            value = reader.get("/contents/" + path + "?ref=" + revision)
            if value.get("type") != "file" or value.get("path") != path or value.get("encoding") != "base64":
                raise ValueError("trusted caller configuration is not a regular encoded file")
            size = value.get("size")
            if type(size) is not int or not 0 <= size <= 2 * 1024 * 1024:
                raise ValueError("trusted caller file exceeds the configuration bound")
            raw = base64.b64decode(value["content"].replace("\n", "").replace("\r", ""), validate=True)
            if len(raw) != size:
                raise ValueError("trusted caller file is truncated")
            return raw

        configuration = content(".tauceti/ci-source.toml")
        policy = cc.ExecutionPolicy.parse(cc.read_json(content(".tauceti/ci-source.json")), configuration=configuration)
        from .ci_requests import RequestLedger

        RequestLedger.check_configuration(policy, configuration)
        if RequestLedger.binding(policy) != RequestLedger.binding(enrolled_policy) or policy.execution_branch != "main":
            raise ValueError("protected caller changes enrolled repository or credential roles")
        content(policy.workflow)  # The configured caller must exist at this exact revision.
        return revision, policy, configuration

    def _post(self, frozen, path, payload=None, *, token=None):
        # No generic retrying API client: a lost POST response is not permission
        # to send the same dispatch again. The token is resource-scoped when minted,
        # (or by GitHub's native job-token contract), so a name reused between
        # observation and POST cannot select another repo.
        return self.transport(
            "POST",
            GITHUB.api_url + "/repos/" + frozen.policy.execution.repository.name + path,
            {
                "Authorization": "Bearer " + (self.tokens.token() if token is None else token),
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None if payload is None else cc.canonical(payload),
        )

    def preflight(self, frozen):
        try:
            if isinstance(self.tokens, WorkflowTokenSource) and (
                self.tokens.job.repository != frozen.policy.execution
                or self.tokens.job.revision != frozen.request.workflow_revision
                or self.tokens.job.branch != frozen.policy.execution_branch
            ):
                raise ValueError("workflow controller launch differs from the approved frozen caller")
            token = self.tokens.token()
            reader = self._reader(frozen, token=token)
            repository = reader.repository()
            if repository.get("default_branch") != frozen.policy.execution_branch:
                raise ValueError("execution branch is not the GitHub default branch")
            branch = urllib.parse.quote(frozen.policy.execution_branch, safe="")
            current = reader.get("/branches/" + branch)
            if (
                not isinstance(current, dict)
                or current.get("commit", {}).get("sha") != frozen.request.workflow_revision
            ):
                raise DispatchNotAccepted(
                    "trusted CI caller revision changed before dispatch; prepare a fresh request", retryable=False
                )
        except DispatchNotAccepted:
            raise
        except Exception as error:
            # This block precedes the only dispatch POST, so these are proven
            # non-sends even when credential refresh or a read was unavailable.
            raise DispatchNotAccepted(
                "source CI dispatch preflight failed",
                retryable=getattr(error, "retryable", not isinstance(error, ValueError)),
            ) from None
        return PreparedDispatch(frozen.request.digest, frozen.policy.execution.native_id, token)

    def dispatch(self, frozen, *, prepared=None):
        prepared = self.preflight(frozen) if prepared is None else prepared
        if (
            not isinstance(prepared, PreparedDispatch)
            or prepared.request_digest != frozen.request.digest
            or prepared.repository_id != frozen.policy.execution.native_id
            or prepared.repository_id != self.tokens.repository_id
        ):
            raise DispatchNotAccepted("source CI dispatch preparation differs from request", retryable=False)
        try:
            status, headers, _body = self._post(frozen, "/dispatches", frozen.request.dispatch(), token=prepared.token)
        except (urllib.error.URLError, ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
                raise DispatchNotAccepted("source CI connection failed before HTTP dispatch", retryable=True) from None
            raise
        if status == 204:
            return frozen.request.request_id
        if status in {400, 401, 403, 404, 410, 422, 429}:
            if status == 401:
                self.tokens.invalidate()
            limited = status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0")
            reset = headers.get("x-ratelimit-reset") if limited else None
            try:
                retry_at = float(reset) if status in {403, 429} and reset is not None else None
                if retry_at is not None and (not math.isfinite(retry_at) or retry_at < 0):
                    retry_at = None
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if status in {403, 429} and headers.get("retry-after") is not None:
                try:
                    delay = float(headers["retry-after"])
                    if math.isfinite(delay) and delay >= 0:
                        limited = True
                        retry_at = self.clock() + delay
                except (TypeError, ValueError, OverflowError):
                    pass
            raise DispatchNotAccepted(
                f"source CI dispatch refused with HTTP {status}",
                retry_at=retry_at,
                retryable=(status == 401 and not isinstance(self.tokens, WorkflowTokenSource)) or limited,
            )
        raise OSError("source CI dispatch outcome is unconfirmed")

    @staticmethod
    def _run(raw):
        def timestamp(key):
            try:
                return github_app.parse_github_time(raw[key]) if raw.get(key) else None
            except (ValueError, TypeError):
                return None

        status = raw.get("status")
        if status in {"waiting", "requested", "pending"}:
            status = "queued"
        if status not in {"queued", "in_progress", "completed"}:
            raise EvidenceUnavailable("execution run status is unavailable")
        return CandidateRun(
            run_id=str(raw["id"]),
            attempt=raw["run_attempt"],
            status=status,
            conclusion=raw.get("conclusion"),
            created_at=timestamp("created_at"),
            started_at=timestamp("run_started_at"),
            completed_at=timestamp("updated_at") if status == "completed" else None,
            runner_name=None,
            runner_labels=(),
            host=None,
            display_title=str(raw.get("display_title") or ""),
            html_url=f"https://github.com/{raw['repository']['full_name']}/actions/runs/{raw['id']}",
            updated_at=timestamp("updated_at"),
        )

    def _metadata(self, frozen, run_id):
        if not isinstance(run_id, str) or not run_id.isascii() or not run_id.isdecimal() or run_id.startswith("0"):
            raise ValueError("invalid execution run ID")
        reader = self._reader(frozen)
        reader.repository()
        raw = reader.get("/actions/runs/" + run_id)
        attempt = raw.get("run_attempt") if isinstance(raw, dict) else None
        cc.check_run_metadata(
            frozen.request, frozen.policy, raw, run_id=int(run_id), attempt=attempt, require_completed=False
        )
        return reader, self._run(raw)

    def status(self, frozen, run_id):
        """Fresh run identity/status only; old artifact expiry is irrelevant."""
        return self._metadata(frozen, run_id)[1]

    def observe(self, frozen, run_id, *, owned=False):
        reader, run = self._metadata(frozen, run_id)
        ownership = result = None
        if not owned:
            try:
                ownership = reader.receipt(frozen.request, run_id=int(run_id), observed_attempt=run.attempt)
            except (EvidenceUnavailable, ValueError):
                pass
        if run.status == "completed":
            try:
                result = reader.result(frozen.request, run_id=int(run_id), attempt=run.attempt)
                ownership = ownership or result
            except (EvidenceUnavailable, ValueError):
                pass
        if not owned and ownership is None:
            raise EvidenceUnavailable("execution run has no verified request ownership yet")
        absent = False
        if (
            owned
            and run.status == "completed"
            and result is None
            and run.conclusion in {"cancelled", "timed_out", "startup_failure", "failure"}
        ):
            absent = self._result_absent(reader, frozen, run)
        return CandidateObservation(run, ownership, result, absent)

    def _result_absent(self, reader, frozen, run):
        """A failed artifact download is not evidence of an absent verdict.

        Deleted/purged artifacts can establish current absence, not that a
        result was never published. This permits only a bounded retry, no pass.
        """
        expected = cc.artifact_name(frozen.request, int(run.run_id), run.attempt)
        seen, total = set(), None
        try:
            for page in range(1, self.max_pages + 1):
                value = reader.get(f"/actions/runs/{run.run_id}/artifacts?per_page=100&page={page}")
                if (
                    not isinstance(value, dict)
                    or type(value.get("total_count")) is not int
                    or value["total_count"] < 0
                    or not isinstance(value.get("artifacts"), list)
                ):
                    return False
                total = value["total_count"] if total is None else total
                rows = value["artifacts"]
                if total != value["total_count"] or len(rows) > 100:
                    return False
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not int
                        or row["id"] < 1
                        or row["id"] in seen
                        or not isinstance(row.get("name"), str)
                    ):
                        return False
                    if row["name"] == expected:
                        return False  # Present but unreadable, expired or invalid is not absent.
                    seen.add(row["id"])
                if len(seen) == total:
                    current = reader.get("/actions/runs/" + run.run_id)
                    cc.check_run_metadata(
                        frozen.request,
                        frozen.policy,
                        current,
                        run_id=int(run.run_id),
                        attempt=run.attempt,
                        require_completed=True,
                    )
                    return current.get("conclusion") == run.conclusion
                if not rows or len(seen) > total:
                    return False
        except (EvidenceUnavailable, ValueError, KeyError, TypeError):
            return False
        return False

    def _list_runs(self, reader, frozen, *, since):
        """Split closed time windows before GitHub's filtered 1,000-run cap.

        All scans remain bounded. Exceeding the request/record/time precision
        bounds yields unavailable; it never proves a dispatch did not happen.
        """
        filename = urllib.parse.quote(frozen.policy.workflow.rsplit("/", 1)[-1], safe="")
        lower = int(max(0, since - 300))
        upper = int(max(since, self.clock() - 1))
        windows = [(lower, upper)]
        runs = {}
        reads = 0
        while windows:
            lower, upper = windows.pop()
            start = datetime.fromtimestamp(lower, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = datetime.fromtimestamp(upper, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            window = urllib.parse.quote(start + ".." + end, safe="")
            prefix = f"/actions/workflows/{filename}/runs?event=repository_dispatch&created={window}&per_page=100"
            collected, total = [], None
            for page in range(1, self.max_pages + 1):
                reads += 1
                if reads > 64:
                    raise EvidenceUnavailable("execution discovery exceeded its request bound")
                value = reader.get(prefix + f"&page={page}")
                if (
                    not isinstance(value, dict)
                    or type(value.get("total_count")) is not int
                    or not isinstance(value.get("workflow_runs"), list)
                ):
                    raise EvidenceUnavailable("execution run listing is incomplete")
                total = value["total_count"] if total is None else total
                if total < 0 or total != value["total_count"] or len(value["workflow_runs"]) > 100:
                    raise EvidenceUnavailable("execution run listing changed")
                if page == 1 and (total >= 1000 or total > self.max_pages * 100):
                    if upper - lower <= 1:
                        raise EvidenceUnavailable("execution discovery exceeds its time precision bound")
                    midpoint = (lower + upper) // 2
                    # Overlap at the boundary to avoid assuming subsecond API
                    # filter precision. Native run IDs deduplicate the windows.
                    windows.extend(((midpoint, upper), (lower, midpoint)))
                    break
                collected.extend(value["workflow_runs"])
                if len(collected) == total:
                    break
                if not value["workflow_runs"] or len(collected) > total:
                    raise EvidenceUnavailable("execution run listing is truncated")
            else:
                raise EvidenceUnavailable("execution discovery exceeded its page bound")
            if len(collected) != total:
                continue  # This oversized interval was replaced by its children.
            if any(
                not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1 for row in collected
            ) or len({row["id"] for row in collected}) != len(collected):
                raise EvidenceUnavailable("execution run listing has invalid identities")
            for row in collected:
                runs[row["id"]] = row
            if len(runs) > 10_000:
                raise EvidenceUnavailable("execution discovery exceeded its record bound")
        return list(runs.values())

    def discover(self, frozen, *, since, known_run_ids=()):
        reader = self._reader(frozen)
        reader.repository()
        runs = self._list_runs(reader, frozen, since=since)
        observations, unavailable = [], False
        for row in runs:
            if row.get("display_title") != "source request=" + frozen.request.request_id:
                continue  # Discovery hint only; observe verifies the artifact.
            try:
                run_id = str(row["id"])
                observations.append(
                    CandidateObservation(self.status(frozen, run_id), None, None)
                    if run_id in known_run_ids
                    else self.observe(frozen, run_id)
                )
            except ValueError:
                continue  # A matching title with another claim is not our run.
            except EvidenceUnavailable:
                # A completed unowned lookalike is not a live run. This grants
                # no ownership, verdict or cancellation authority for it.
                try:
                    unavailable |= self.status(frozen, str(row["id"])).status != "completed"
                except ValueError:
                    continue
                except EvidenceUnavailable:
                    unavailable = True
        return observations, unavailable

    def cancel(self, frozen, run_id, *, proof):
        # The coordinator calls this only for a ledger-owned run. Recheck the
        # native repository/caller/harness before the idempotent cancellation.
        if (
            not isinstance(proof, dict)
            or proof.get("kind") not in {"request", "result"}
            or proof.get("request_digest") != frozen.request.digest
            or str(proof.get("run_id")) != run_id
        ):
            raise ValueError("cancellation requires the stored request ownership proof")
        if self.status(frozen, run_id).status == "completed":
            return "already-completed"
        status, _headers, _body = self._post(frozen, "/actions/runs/" + run_id + "/cancel")
        if status == 202:
            return "cancelled"
        if status == 409:
            return "already-completed"
        raise OSError("owned execution cancellation is unconfirmed")

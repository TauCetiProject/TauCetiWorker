"""Native GitHub job credentials for a trusted, pinned workflow bootstrap.

The bootstrap must pass the literal github.token context, never an event input,
repository secret, PAT, or inherited gh credential. API observations corroborate
that launch; they cannot prove a token came from a job or how it was minted.
GitHub's job-token contract supplies the immutable repository resource scope.
An installation currently exposing one repository does not prove mint-time scope;
the scope observation must never be used to adopt an arbitrary supplied secret.
No production entry point selects this provider automatically.
"""

from __future__ import annotations

import math
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import NoRedirect
from tauceti_tools.forge import GITHUB

from .github_app import API, CredentialsError, ssl_context

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_LIFETIME_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class TrustedWorkflowJob:
    """Approved launch identity, assembled by trusted workflow code.

    The revision, workflow and job name must come from approved configuration;
    copying attacker-controlled event fields or environment values is not approval.
    The live run ID and attempt are checked against GitHub before credential use.
    """

    repository: cc.RepositoryIdentity
    workflow: str
    revision: str
    branch: str
    job_name: str
    run_id: int
    attempt: int
    ref: str

    def __post_init__(self):
        if not isinstance(self.repository, cc.RepositoryIdentity) or self.repository.repository.forge != GITHUB:
            raise CredentialsError("workflow job needs a native GitHub repository identity")
        if (
            not isinstance(self.workflow, str)
            or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", self.workflow) is None
        ):
            raise CredentialsError("workflow job path is invalid")
        if (
            not isinstance(self.revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.revision) is None
            or not int(self.revision, 16)
        ):
            raise CredentialsError("workflow job revision is invalid")
        if not isinstance(self.job_name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.job_name) is None:
            raise CredentialsError("workflow job name must be an explicit unique nonmatrix name")
        if not isinstance(self.branch, str) or re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", self.branch) is None:
            raise CredentialsError("workflow job branch is invalid")
        # The trusted bootstrap supplies the literal github.ref context. The
        # REST head_branch field alone cannot distinguish a same-named tag.
        if self.ref != "refs/heads/" + self.branch:
            raise CredentialsError("workflow job must run on its approved branch ref")
        if any(type(value) is not int or not 0 < value < 2**63 for value in (self.run_id, self.attempt)):
            raise CredentialsError("workflow job run identity is invalid")


class WorkflowTokenSource:
    """One native job token, with a checked launch and instance lifetime cap.

    Construct only in a trusted workflow with explicit contents:write and
    actions:write job permissions. Permissions below describe that bootstrap
    requirement; they are not inferred from repository metadata. There is no
    environment fallback, token refresh, serialization, or automatic enrollment.
    Invalidating this fixed credential permanently disables this instance.
    """

    _api = API

    def __init__(self, job: TrustedWorkflowJob, token: str, *, transport=None, clock=time.monotonic):
        if not isinstance(job, TrustedWorkflowJob):
            raise CredentialsError("workflow token requires an approved workflow job")
        if not isinstance(token, str) or not token or not all(33 <= ord(char) <= 126 for char in token):
            raise CredentialsError("workflow job token is invalid")
        self._job = job
        self._value = token
        self._clock = clock
        self._started = self._now()
        self._verified = False
        self._invalid = False
        self._lock = threading.Lock()
        if transport is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ssl_context()),
                NoRedirect(),
            )

            def transport(method, url, headers, body):
                request = urllib.request.Request(url, data=body, method=method, headers=headers)
                try:
                    response = opener.open(request, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.code, dict(response.headers), response.read(MAX_RESPONSE_BYTES + 1)

        self._transport = transport

    @property
    def job(self):
        return self._job

    @property
    def repository_id(self):
        return self.job.repository.native_id

    @property
    def permissions(self):
        return {"contents": "write", "actions": "write"}

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise CredentialsError("workflow token clock is invalid")
        return value

    def invalidate(self):
        with self._lock:
            self._invalid = True
            self._value = None

    def token(self):
        with self._lock:
            now = self._now()
            if self._invalid or now < self._started or now - self._started >= MAX_LIFETIME_SECONDS:
                self._invalid = True
                self._value = None
                raise CredentialsError("workflow job credential is no longer available")
            if not self._verified:
                try:
                    self._verify()
                except CredentialsError as error:
                    if not error.retryable:
                        self._invalid = True
                        self._value = None
                    raise
                self._verified = True
            finished = self._now()
            if finished < now or finished - self._started >= MAX_LIFETIME_SECONDS:
                self._invalid = True
                self._value = None
                raise CredentialsError("workflow job credential expired during observation")
            return self._value

    def _get(self, path):
        try:
            status, headers, body = self._transport(
                "GET",
                API + path,
                {
                    "Authorization": "Bearer " + self._value,
                    "Accept": "application/vnd.github+json",
                    "Cache-Control": "no-cache",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                None,
            )
        except Exception:
            raise CredentialsError("workflow credential observation is unavailable", retryable=True) from None
        headers = {key.lower(): value for key, value in headers.items()}
        if status != 200:
            if status == 401:
                self._invalid = True
                self._value = None
            limited = status == 429 or (
                status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
            )
            raise CredentialsError(
                f"workflow credential observation refused with HTTP {status}",
                retryable=limited or 500 <= status <= 599,
            )
        if headers.get("link"):
            raise CredentialsError("workflow credential observation is paginated")
        try:
            value = cc.read_json(body, max_bytes=MAX_RESPONSE_BYTES)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise CredentialsError("workflow credential observation is malformed")
        return value

    def _repository_matches(self, value):
        return (
            isinstance(value, dict)
            and type(value.get("id")) is int
            and value["id"] == self.repository_id
            and value.get("full_name") == self.job.repository.repository.name
        )

    def _verify(self):
        scope = self._get("/installation/repositories?per_page=2")
        repositories = scope.get("repositories")
        if (
            type(scope.get("total_count")) is not int
            or scope["total_count"] != 1
            or not isinstance(repositories, list)
            or len(repositories) != 1
            or not self._repository_matches(repositories[0])
        ):
            raise CredentialsError("workflow credential has unexpected repository resources")
        prefix = "/repos/" + self.job.repository.repository.name
        run = self._get(f"{prefix}/actions/runs/{self.job.run_id}/attempts/{self.job.attempt}")
        if (
            type(run.get("id")) is not int
            or run["id"] != self.job.run_id
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != self.job.attempt
            or not self._repository_matches(run.get("repository"))
            or not self._repository_matches(run.get("head_repository"))
            or run.get("head_sha") != self.job.revision
            or run.get("head_branch") != self.job.branch
            or run.get("path")
            not in {
                self.job.workflow,
                self.job.workflow + "@" + self.job.branch,
                self.job.workflow + "@refs/heads/" + self.job.branch,
            }
            or run.get("status") != "in_progress"
            or run.get("event") not in {"push", "workflow_dispatch"}
        ):
            raise CredentialsError("workflow credential launch differs from the approved live workflow")
        jobs = self._get(f"{prefix}/actions/runs/{self.job.run_id}/attempts/{self.job.attempt}/jobs?per_page=100")
        rows = jobs.get("jobs")
        if (
            not isinstance(rows, list)
            or type(jobs.get("total_count")) is not int
            or jobs["total_count"] != len(rows)
            or len(rows) > 100
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise CredentialsError("workflow job observation is incomplete")
        matching = [row for row in rows if row.get("name") == self.job.job_name]
        if len(matching) != 1 or matching[0].get("status") != "in_progress":
            raise CredentialsError("approved workflow job is not uniquely running")

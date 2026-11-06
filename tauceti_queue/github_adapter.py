"""The queue's GitHub adapter: one rate-limit-aware client and the `GitHubPort` implementation.

Every GitHub call the service makes goes through `GitHubClient`, which authenticates as the App's
installation (`github_app.InstallationTokenSource`), records the primary rate limit from each
response, retries secondary-limit and transient failures with bounded waits, and exposes those
counters through `stats()` for the status endpoint.

`GitHubAdapter` implements the controller's `GitHubPort` (`tauceti_queue/ports.py`, the authoritative
protocol and record shapes; timestamps are unix seconds, run ids strings):

    fetch_pr(repo, number) -> PullRequest                 (raises PullRequestNotFound)
    fetch_refs(repo, refspecs)
    dispatch_candidate(repo, *, head_sha, base_sha, base_ref, batch_id, members, repair_outputs_revs) -> dispatch id
    find_candidate_run(repo, *, batch_id, head_sha, base_sha) -> CandidateRun | None
    commit_status(repo, sha, context) -> Status | None
    fast_forward(repo, branch, expected_parent, tip) -> FastForwardResult
    update_ref(repo, ref, sha)
    post_comment(repo, number, body, marker) -> comment id
    set_labels(repo, number, add, remove)
    ensure_label(repo, name, color, description)
    close_pr(repo, number, title)
    repository_admin(login) -> bool
    rate_limit() -> (remaining, reset_at)
    eligibility_checks(repo, head) -> [check run records]
    pr_merge_base(repo, number, head_sha) -> sha | None
    list_open_pull_requests(repo, *, review_app_id) -> [OpenPullRequest]   (GraphQL, 100 PRs per call)

The conventions that keep the queue a drop-in replacement for Bors are encoded here: the dispatch
is `repository_dispatch` of `pr-build.yml` with event type `tauceti-bors-staging` and the same
client payload, the run is found by the `run-name` the workflow derives from that payload, the
verdict is the `build` commit status whose target URL names the run, a completed run's diagnostics
are read from its job log with Bors's patterns (`diagnostics.py`), promotion is a plain
fast-forward update of the base branch through the Git Data API, and a landed member is closed with
the `[Merged by Bors] - ` title, as Bors's `close_merged_pr` does.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

from . import chain, declaration_names, diagnostics, github_app, ports
from .ports import CandidatePhase, CandidateRun, FastForwardResult, Status

API = github_app.API
API_VERSION = "2022-11-28"
USER_AGENT = "tauceti-queue (https://github.com/TauCetiProject/TauCetiWorker)"
STAGING_EVENT = "tauceti-bors-staging"
STAGING_WORKFLOW = "pr-build.yml"
BUILD_JOB = "sandboxed-build"
SHA = re.compile(r"[0-9a-f]{40}")
RUN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")
COMMENT_MARKER = "<!-- tauceti-queue {marker} -->"
# Bors treats collaborators with push access as reviewers (`auto_reviewer_required_perm: :push`).
REVIEWER_PERMISSIONS = frozenset({"admin", "maintain", "write"})
MAX_LOG_BYTES = 64 * 1024 * 1024

Transport = github_app.Transport
Phase = CandidatePhase


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PullRequestNotFound(GitHubError, LookupError):
    """`fetch_pr` for a number GitHub does not know."""


class RequestDeferred(GitHubError):
    """A resource opportunity, with an absolute time the owner can persist."""

    def __init__(self, message: str, status: int = 0, body: bytes = b"", *, retry_at: float, resource: str) -> None:
        super().__init__(message, status, body)
        self.retry_at = retry_at
        self.resource = resource


class RateLimited(RequestDeferred):
    """GitHub itself has blocked the resource until retry_at."""


class AmbiguousCandidateRun(GitHubError):
    """More than one `repository_dispatch` run carries the candidate's identity."""


# ---------------------------------------------------------------------------------------------
# Records


@dataclasses.dataclass(frozen=True)
class PullRequest(ports.PullRequest):
    """The port's record plus what command handling and scope approvals need."""

    base_sha: str = ""
    author_id: int = 0
    head_repo: str = ""
    merged: bool = False
    updated_at: str = ""
    comments_count: int = 0

    @classmethod
    def from_json(cls, data: dict) -> PullRequest:
        head = data.get("head") or {}
        base = data.get("base") or {}
        user = data.get("user") or {}
        return cls(
            number=int(data["number"]),
            head_sha=str(head.get("sha") or ""),
            base_ref=str(base.get("ref") or ""),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            author_login=str(user.get("login") or ""),
            state=str(data.get("state") or ""),
            draft=bool(data.get("draft")),
            labels=tuple(str(label.get("name")) for label in data.get("labels") or [] if isinstance(label, dict)),
            base_sha=str(base.get("sha") or ""),
            author_id=int(user.get("id") or 0),
            head_repo=str(((head.get("repo") or {}).get("full_name")) or ""),
            merged=bool(data.get("merged")),
            updated_at=str(data.get("updated_at") or ""),
            comments_count=int(data.get("comments") or 0),
        )


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body) if self.body else None


def _timestamp(text) -> float | None:
    """Unix seconds for a GitHub ISO timestamp; None for a missing one."""
    if not text or not isinstance(text, str):
        return None
    try:
        return github_app.parse_github_time(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Transport and client


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses instead of following them: a job log redirect points at a signed URL
    that must be fetched without the Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """The real transport: stdlib urllib with the system-aware TLS context, no redirects."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=github_app.ssl_context()), _NoRedirect()
        )
        self._timeout = timeout

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return (
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read()
                    if response.headers.get_content_type() == "application/json"
                    else response.read(MAX_LOG_BYTES),
                )
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


class GitHubClient:
    """One authenticated client with retry on secondary limits and a visible rate-limit counter."""

    MAX_ATTEMPTS = 4
    MAX_WAIT_SECONDS = 120.0

    def __init__(
        self,
        tokens: github_app.InstallationTokenSource | None,
        transport: Transport,
        *,
        api: str = API,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tokens = tokens
        self._transport = transport
        self._api = api.rstrip("/")
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._priority = threading.Condition(self._lock)
        self._essential: dict[str, int] = {}
        self._resource_limits: dict[str, dict] = {}
        self._background_at: dict[str, float] = {}
        self._local = threading.local()
        self._blocked_until: dict[str, float] = {}
        self.requests = 0
        self.retries = 0
        self.limit_waits = 0
        self.failures = 0
        self.remaining: int | None = None
        self.limit: int | None = None
        self.reset_at: float | None = None
        self.last_error: str | None = None

    # -- bookkeeping

    def restore_deadlines(self, deadlines: dict[str, float]) -> None:
        """Retain observed resource waits across restart, including for newly dirty PRs."""
        with self._lock:
            for resource, deadline in deadlines.items():
                if not isinstance(deadline, int | float) or deadline <= self._clock():
                    continue
                if resource.endswith(":background"):
                    target, key = self._background_at, resource.removesuffix(":background")
                else:
                    target, key = self._blocked_until, resource
                target[key] = max(target.get(key, 0), deadline)

    def stats(self) -> dict:
        with self._lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "limit_waits": self.limit_waits,
                "failures": self.failures,
                "rate_limit_remaining": self.remaining,
                "rate_limit": self.limit,
                "rate_limit_reset_at": self.reset_at,
                "last_error": self.last_error,
                "resources": {key: dict(value) for key, value in self._resource_limits.items()},
                "blocked_until": dict(self._blocked_until),
            }

    def rate_limit(self) -> tuple[int | None, float | None]:
        with self._lock:
            return self.remaining, self.reset_at

    def _record(self, headers: dict[str, str], resource="core", *, cost=None) -> None:
        with self._lock:
            record = self._resource_limits.setdefault(resource, {})
            previous_remaining, previous_reset = record.get("remaining"), record.get("reset_at")
            if isinstance(cost, int) and cost > 0:
                record["cost"] = cost
            try:
                if "x-ratelimit-remaining" in headers:
                    record["remaining"] = int(headers["x-ratelimit-remaining"])
                if "x-ratelimit-limit" in headers:
                    record["limit"] = int(headers["x-ratelimit-limit"])
                if "x-ratelimit-reset" in headers:
                    record["reset_at"] = float(headers["x-ratelimit-reset"])
            except ValueError:
                pass
            if resource == "core":
                self.remaining = record.get("remaining")
                self.limit = record.get("limit")
                self.reset_at = record.get("reset_at")
            remaining, reset = record.get("remaining"), record.get("reset_at")
            now = self._clock()
            observed = "x-ratelimit-remaining" in headers or cost is not None
            if observed and remaining is not None and reset is not None and reset > now:
                if remaining <= 0:
                    self._blocked_until[resource] = reset + 1
                else:
                    # Background reads share the real remaining window; essential requests
                    # consume their share immediately and their responses adjust this spacing.
                    next_at = now + record.get("cost", 1) * (reset - now) / remaining
                    if previous_reset == reset and previous_remaining is not None and remaining <= previous_remaining:
                        next_at = max(next_at, self._background_at.get(resource, 0))
                    self._background_at[resource] = min(reset + 1, next_at)

    # -- requests

    def _send(self, method, url, headers, body, *, resource, deferred):
        # Essential queue/CI requests never wait for background intake. Before each intake
        # request, let already-active essential work finish on that bucket. There is no fixed
        # quota reserve; unrelated buckets and essential requests remain free to run.
        with self._priority:
            while deferred and self._essential.get(resource, 0):
                self._priority.wait()
            if not deferred:
                self._essential[resource] = self._essential.get(resource, 0) + 1
        try:
            return self._transport(method, url, headers, body)
        finally:
            if not deferred:
                with self._priority:
                    self._essential[resource] -= 1
                    self._priority.notify_all()

    @contextmanager
    def deferred_requests(self, *, urgent=False):
        """Background intake persists waits instead of sleeping inside a source read.

        This is thread-local: essential queue and command calls retain their existing retry
        behavior. A known exhausted bucket refuses new reads until its original deadline;
        trying another PR cannot move that deadline further away.
        """
        previous = getattr(self._local, "deferred", False)
        prior_background = getattr(self._local, "background", False)
        self._local.deferred = True
        self._local.background = not urgent
        try:
            yield
        finally:
            self._local.deferred = previous
            self._local.background = prior_background

    def url(self, path: str, params: dict | None = None) -> str:
        url = path if path.startswith("http") else f"{self._api}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + query
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: object = None,
        token: str | None = None,
        accept: str = "application/vnd.github+json",
        ok: Iterable[int] = (200, 201, 202, 204),
        tolerate: Iterable[int] = (),
        record_limit: bool = True,
    ) -> Response:
        """One API call. Statuses in `ok` return; `tolerate` returns too (callers inspect); others raise.

        `record_limit=False` keeps a GraphQL response's headers (its own, separate quota) out of the
        REST counter `rate_limit()` reports.
        """
        url = self.url(path, params)
        resource = "graphql" if path.strip("/") == "graphql" else "core"
        deferred = getattr(self._local, "deferred", False)
        background = getattr(self._local, "background", False)
        if token is None:
            with self._lock:
                blocked_resource, deadline = max(
                    ((key, self._blocked_until.get(key, 0)) for key in (resource, "secondary")),
                    key=lambda pair: pair[1],
                )
            if deadline > self._clock():
                if deferred or deadline - self._clock() > self.MAX_WAIT_SECONDS:
                    raise RateLimited("GitHub resource wait", retry_at=deadline, resource=blocked_resource)
                self._sleep(deadline - self._clock())
            if background:
                with self._lock:
                    next_at = self._background_at.get(resource, 0)
                if next_at > self._clock():
                    raise RequestDeferred(
                        "background read paced by remaining GitHub capacity",
                        retry_at=next_at,
                        resource=resource + ":background",
                    )
        body = None if json_body is None else json.dumps(json_body).encode()
        accepted = set(ok) | set(tolerate)
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
            if body is not None:
                headers["Content-Type"] = "application/json"
            used_installation = False
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            elif self._tokens is not None:
                headers["Authorization"] = f"Bearer {self._tokens.token()}"
                used_installation = True
            with self._lock:
                self.requests += 1
            status, response_headers, data = self._send(
                method, url, headers, body, resource=resource, deferred=background
            )
            response_headers = {k.lower(): v for k, v in response_headers.items()}
            if token is None:
                # GET /user uses an operator/worker bearer, and GET /app uses an App JWT.
                # Neither is the installation bucket used for queue/intake work.
                self._record(
                    response_headers,
                    response_headers.get("x-ratelimit-resource") or (resource if not record_limit else "core"),
                )
            if status in accepted:
                return Response(status, response_headers, data)
            if status == 401 and used_installation and attempt == 1:
                self._tokens.invalidate()
                self._retry()
                continue
            wait = self._limit_wait(status, response_headers, data)
            if wait is not None:
                blocked_resource = resource if response_headers.get("x-ratelimit-remaining") == "0" else "secondary"
                deadline = self._clock() + wait
                if token is None:
                    with self._lock:
                        self._blocked_until[blocked_resource] = deadline
                if deferred or wait > self.MAX_WAIT_SECONDS or attempt >= self.MAX_ATTEMPTS:
                    self._fail(f"rate limited for {wait:.0f}s on {method} {path}")
                    raise RateLimited(
                        f"rate limited for {wait:.0f}s", status, data, retry_at=deadline, resource=blocked_resource
                    )
                with self._lock:
                    self.limit_waits += 1
                self._retry()
                self._sleep(wait)
                continue
            if status >= 500 and not deferred and attempt < self.MAX_ATTEMPTS:
                self._retry()
                self._sleep(min(2.0**attempt, 30.0))
                continue
            self._fail(f"HTTP {status} on {method} {path}")
            raise GitHubError(f"HTTP {status} on {method} {path}", status, data)

    def _retry(self) -> None:
        with self._lock:
            self.retries += 1

    def _fail(self, message: str) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = message

    def _limit_wait(self, status: int, headers: dict[str, str], body: bytes = b"") -> float | None:
        """Seconds to wait when the response is a primary or secondary rate limit, else None."""
        if status not in (403, 429):
            return None
        waits = []
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                waits.append(max(1.0, float(retry_after)))
            except ValueError:
                try:
                    waits.append(max(1.0, parsedate_to_datetime(retry_after).timestamp() - self._clock()))
                except (TypeError, ValueError, OverflowError):
                    pass
        if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset"):
            try:
                waits.append(max(1.0, float(headers["x-ratelimit-reset"]) - self._clock() + 1.0))
            except ValueError:
                pass
        if waits:
            return max(waits)
        if status == 429:
            return 60.0
        if status == 403:
            message = body.decode("utf-8", errors="replace").lower()
            if "secondary rate limit" in message or "abuse detection" in message:
                return 60.0
        return None

    def get_json(self, path: str, *, params: dict | None = None, token: str | None = None, tolerate=()):
        response = self.request("GET", path, params=params, token=token, tolerate=tolerate)
        return response.status, response.json()

    def paginate(
        self,
        path: str,
        *,
        params: dict | None = None,
        key: str | None = None,
        per_page: int = 100,
        progress: dict | None = None,
    ) -> list:
        """Every page; an intake-owned progress object retains read pages across a resource wait."""
        progress = {} if progress is None else progress
        items = progress.setdefault("items", [])
        page = progress.setdefault("page", 1)
        if progress.get("done"):
            return items
        while True:
            query = dict(params or {})
            query.update(per_page=per_page, page=page)
            status, data = self.get_json(path, params=query)
            batch = data.get(key) if key else data
            if not isinstance(batch, list):
                raise GitHubError(f"unexpected page shape from {path}", status)
            items.extend(batch)
            if len(batch) < per_page:
                progress["done"] = True
                return items
            page += 1
            progress["page"] = page

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """One GraphQL call (`POST /graphql`); returns `data`, raising `GitHubError` on `errors`."""
        response = self.request(
            "POST", "graphql", json_body={"query": query, "variables": variables or {}}, record_limit=False
        )
        document = response.json()
        if not isinstance(document, dict):
            raise GitHubError("unexpected GraphQL response shape", response.status)
        errors = document.get("errors")
        if errors:
            if any(isinstance(error, dict) and error.get("type") == "RATE_LIMITED" for error in errors):
                # GitHub reports GraphQL exhaustion with HTTP 200. Its headers still describe
                # the GraphQL bucket, independently of the REST counter.
                reset = response.headers.get("x-ratelimit-reset")
                deadline = float(reset) + 1 if reset else self._clock() + 60
                with self._lock:
                    self._blocked_until["graphql"] = deadline
                raise RateLimited(
                    "GraphQL rate limited", response.status, response.body, retry_at=deadline, resource="graphql"
                )
            raise GitHubError(f"GraphQL: {errors[0].get('message') if isinstance(errors[0], dict) else errors[0]}")
        data = document.get("data")
        if not isinstance(data, dict):
            raise GitHubError("GraphQL response without data", response.status)
        rate = data.get("rateLimit") or {}
        if isinstance(rate.get("cost"), int):
            headers = dict(response.headers)
            if isinstance(rate.get("remaining"), int):
                headers["x-ratelimit-remaining"] = str(rate["remaining"])
            reset = _timestamp(rate.get("resetAt"))
            if reset is not None:
                headers["x-ratelimit-reset"] = str(reset)
            self._record(headers, "graphql", cost=rate["cost"])
        return data

    def fetch_bare(self, url: str) -> tuple[int, bytes]:
        """A GET without any credential, for signed URLs GitHub redirects to."""
        status, _, body = self._transport("GET", url, {"User-Agent": USER_AGENT}, None)
        return status, body


# ---------------------------------------------------------------------------------------------
# Adapter


def dispatch_title(base_ref: str, batch_id, head_sha: str, base_sha: str) -> str:
    """The `run-name` pr-build.yml derives from a staging dispatch; the key for finding the run."""
    return f"bors branch={base_ref} batch={batch_id} head={head_sha} base={base_sha}"


def comment_marker(marker: str) -> str:
    return COMMENT_MARKER.format(marker=marker)


def run_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = RUN_URL.search(url)
    return match.group(1) if match else None


def dispatch_members(members: Sequence) -> list[dict]:
    """Bors's `members` payload, `[{pr, head_sha}]`; a bare PR number carries no head."""
    out = []
    for member in members:
        if isinstance(member, Mapping):
            entry = {"pr": int(member["pr"])}
            if member.get("head_sha"):
                entry["head_sha"] = str(member["head_sha"])
            out.append(entry)
        else:
            out.append({"pr": int(member)})
    return out


def _batch_id(value):
    return int(value) if str(value).isdigit() else value


class GitHubAdapter:
    """`GitHubPort` over `GitHubClient`.

    Built either from a `github_app.GitHubApp` (`GitHubAdapter(app, repository, clone_dir=...)`, the
    service entrypoint's form) or from a ready client (`GitHubAdapter(client, repository, tokens=...)`,
    the tests' form). `repository` is the default for the repo-less `repository_admin(login)`.
    """

    def __init__(
        self,
        source,
        repository: str | None = None,
        *,
        repo: str | None = None,
        clone_dir: str | os.PathLike | None = None,
        tokens: github_app.InstallationTokenSource | None = None,
        git: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        if isinstance(source, GitHubClient):
            self.client = source
            self._tokens = tokens
        else:
            self.client = GitHubClient(source.tokens, source.transport, api=source.api)
            self._tokens = source.tokens
        self.repo = repository or repo or ""
        if not self.repo:
            raise ValueError("GitHubAdapter needs the repository name")
        self.clone_dir = os.fspath(os.path.expanduser(clone_dir)) if clone_dir is not None else None
        self._git = git
        # Resident API pagination, not an external work queue or an Actions checkpoint. The
        # database owns PR work; these read buffers prevent exhausting quota from restarting
        # a long listing at page one. A process restart uses normal source reconciliation.
        self._admission_reads: dict = {}
        self._admission_listing: dict = {}
        self._admission_comment_pages: dict = {}

    # -- pull requests

    def fetch_pr(self, repo: str, number: int) -> PullRequest:
        status, data = self.client.get_json(f"repos/{repo}/pulls/{number}", tolerate=(404,))
        if status == 404 or not isinstance(data, dict):
            raise PullRequestNotFound(f"{repo}#{number} not found", status)
        return PullRequest.from_json(data)

    def fetch_pr_or_none(self, repo: str, number: int) -> PullRequest | None:
        try:
            return self.fetch_pr(repo, number)
        except PullRequestNotFound:
            return None

    def pr_merge_base(self, repo: str, number: int, head_sha: str) -> str | None:
        """The merge base of the PR head and its base branch, as Bors records for scope approvals."""
        pr = self.fetch_pr_or_none(repo, number)
        if pr is None or pr.head_sha != head_sha or not SHA.fullmatch(pr.base_sha):
            return None
        status, data = self.client.get_json(f"repos/{repo}/compare/{pr.base_sha}...{head_sha}", params={"per_page": 1})
        base = ((data or {}).get("merge_base_commit") or {}).get("sha")
        return base if isinstance(base, str) and SHA.fullmatch(base) else None

    def close_pr(self, repo: str, number: int, title: str) -> None:
        """`PATCH pulls/<n> {state: closed, title}`: Bors's `close_merged_pr`, bookkeeping fields only."""
        self.client.request("PATCH", f"repos/{repo}/pulls/{number}", json_body={"state": "closed", "title": title})

    def labels(self, repo: str, number: int, *, progress=None) -> list[str]:
        return [
            str(label.get("name"))
            for label in self.client.paginate(f"repos/{repo}/issues/{number}/labels", progress=progress)
            if isinstance(label, dict)
        ]

    # -- git objects

    def fetch_refs(self, repo: str, refspecs: Sequence[str]) -> None:
        """Fetch refspecs into the local clone with the installation token in the remote URL.

        The URL in argv is the plain repository URL; the token travels as an `http.extraheader`
        Authorization header passed through the `GIT_CONFIG_*` environment, so it reaches neither
        the process list, nor `.git/config`, nor any log line.
        """
        if self.clone_dir is None:
            raise GitHubError("fetch_refs needs a clone directory")
        if self._tokens is None:
            raise GitHubError("fetch_refs needs the installation token source")
        self._git_with_token(repo, ["fetch", "--no-tags", "--quiet", f"https://github.com/{repo}.git", *refspecs])

    def _git_with_token(self, repo: str, args: Sequence[str]) -> None:
        """Run git in the clone with the installation token as an `http.extraheader` in the environment."""
        token = self._tokens.token()
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SSL_CERT_FILE", "LANG")}
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}",
            GIT_TERMINAL_PROMPT="0",
        )
        command = ["git", "-C", self.clone_dir, *args]
        result = self._git(command, env=env, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            # The header never appears in git's own output; the stderr is safe to surface.
            raise GitHubError(f"git {args[0]} failed for {repo}: {result.stderr.strip()}")

    def fast_forward(self, repo: str, branch: str, expected_parent: str, tip: str) -> FastForwardResult:
        """Advance `branch` from `expected_parent` to `tip` without a merge commit.

        The Git Data API offers no compare-and-swap; `force: false` refuses anything but a
        fast-forward, and the ref is read before and after so a parent that moved is reported as
        `actual_parent` instead of being overwritten. A tip that is a descendant of a moved parent
        is still refused by the pre-check, which is what the controller's rebase-then-promote cycle
        expects.
        """
        current = self.ref_sha(repo, f"heads/{branch}")
        if current != expected_parent:
            return FastForwardResult(False, current, "parent changed before the push")
        response = self.client.request(
            "PATCH", f"repos/{repo}/git/refs/heads/{branch}", json_body={"sha": tip, "force": False}, tolerate=(422,)
        )
        if response.status == 200:
            return FastForwardResult(True, expected_parent)
        current = self.ref_sha(repo, f"heads/{branch}")
        detail = ""
        try:
            detail = str((response.json() or {}).get("message") or "")
        except ValueError:
            pass
        return FastForwardResult(False, current, detail or "update is not a fast forward")

    def update_ref(self, repo: str, ref: str, sha: str) -> None:
        """Create or force-update `refs/<ref>` (retained candidate refs, never the base branch).

        Retained commits (squash chains, replacement tips) are built in the service's clone and
        exist nowhere else, so with a clone the ref is pushed from it (`git push +<sha>:refs/<ref>`),
        which uploads the objects as well; the Git Data API, used without a clone, can only point a
        ref at an object GitHub already has.
        """
        ref = ref.removeprefix("refs/")
        if self.clone_dir is not None and self._tokens is not None:
            self._git_with_token(
                repo, ["push", "--quiet", "--no-verify", f"https://github.com/{repo}.git", f"+{sha}:refs/{ref}"]
            )
            return
        response = self.client.request(
            "PATCH", f"repos/{repo}/git/refs/{ref}", json_body={"sha": sha, "force": True}, tolerate=(404, 422)
        )
        if response.status in (404, 422):
            self.client.request("POST", f"repos/{repo}/git/refs", json_body={"ref": f"refs/{ref}", "sha": sha})

    def update_refs(self, repo: str, updates: Sequence[tuple[str, str]]) -> None:
        """Create or force-update several refs in one `git push` with many refspecs (one connection,
        one object upload) when there is a clone; without one, each ref goes through the API."""
        if self.clone_dir is not None and self._tokens is not None and updates:
            refspecs = [f"+{sha}:refs/{ref.removeprefix('refs/')}" for ref, sha in updates]
            self._git_with_token(repo, ["push", "--quiet", "--no-verify", f"https://github.com/{repo}.git", *refspecs])
            return
        for ref, sha in updates:
            self.update_ref(repo, ref, sha)

    def ref_sha(self, repo: str, ref: str) -> str | None:
        ref = ref.removeprefix("refs/")
        status, data = self.client.get_json(f"repos/{repo}/git/ref/{ref}", tolerate=(404,))
        if status == 404 or not isinstance(data, dict):
            return None
        return str((data.get("object") or {}).get("sha") or "") or None

    # -- CI dispatch and observation

    def dispatch_candidate(
        self,
        repo: str,
        *,
        head_sha: str,
        base_sha: str,
        base_ref: str,
        batch_id,
        members: Sequence,
        repair_outputs_revs: Sequence[str] = (),
    ) -> str:
        """`repository_dispatch` of pr-build.yml as Bors sends it; returns the run title.

        The payload adds `repair_outputs_revs`, the revisions whose published build outputs CI may
        restore when none are published for `head_sha` (empty when the caller names none).
        """
        payload = {
            "event_type": STAGING_EVENT,
            "client_payload": {
                "head_sha": head_sha,
                "base_sha": base_sha,
                "batch_id": _batch_id(batch_id),
                "members": dispatch_members(members),
                "base_ref": base_ref,
                "repair_outputs_revs": list(repair_outputs_revs),
            },
        }
        self.client.request("POST", f"repos/{repo}/dispatches", json_body=payload, ok=(204,))
        return dispatch_title(base_ref, _batch_id(batch_id), head_sha, base_sha)

    def find_candidate_run(
        self,
        repo: str,
        *,
        batch_id,
        head_sha: str,
        base_sha: str,
        base_ref: str = "main",
        since: float | None = None,
        with_jobs: bool = True,
        with_diagnostics: bool = True,
    ) -> CandidateRun | None:
        """The pr-build.yml run dispatched for this candidate, by its derived `run-name`.

        One list call bounded to a creation window (a minute of skew either side of `since`, or the
        last day), one jobs call for the phases, and for a completed unsuccessful run the build
        job's log for the diagnostics. Two runs with the same identity make the verdict ambiguous
        and are reported as such rather than guessed between.
        """
        expected = dispatch_title(base_ref, _batch_id(batch_id), head_sha, base_sha)
        now = time.time()
        start = (since if since is not None else now - 86400) - 60
        window = f"{_iso(start)}..{_iso(now + 60)}"
        runs = self.client.paginate(
            f"repos/{repo}/actions/workflows/{STAGING_WORKFLOW}/runs",
            params={"event": "repository_dispatch", "created": window},
            key="workflow_runs",
        )
        matches = [r for r in runs if isinstance(r, dict) and r.get("display_title") == expected]
        if not matches:
            return None
        if len(matches) > 1 and since is not None:
            # An infrastructure re-dispatch of the same head, base and batch creates a second run with
            # the same title; the one created at or after the dispatch being observed answers for it,
            # the newest first. Runs whose creation time is unreadable stay ambiguous.
            dated = [(_timestamp(r.get("created_at")), r) for r in matches]
            recent = [(t, r) for t, r in dated if t is not None and t >= since - 60]
            if recent:
                recent.sort(key=lambda item: (item[0], int(item[1].get("id") or 0)))
                matches = [recent[-1][1]]
        if len(matches) > 1:
            raise AmbiguousCandidateRun(f"{len(matches)} runs titled {expected!r}")
        return self.candidate_run(repo, matches[0], with_jobs=with_jobs, with_diagnostics=with_diagnostics)

    def read_run(self, repo: str, run_id: str, *, with_jobs: bool = False) -> CandidateRun | None:
        """`GET repos/{repo}/actions/runs/{run_id}`: the run as GitHub reports it now, `None` on 404."""
        status, data = self.client.get_json(f"repos/{repo}/actions/runs/{run_id}", tolerate=(404,))
        if status == 404 or not isinstance(data, dict) or not data.get("id"):
            return None
        return self.candidate_run(repo, data, with_jobs=with_jobs, with_diagnostics=with_jobs)

    def cancel_run(self, repo: str, run_id: str) -> str:
        """`POST repos/{repo}/actions/runs/{run_id}/cancel`: `cancelled` when GitHub accepted the request,
        `already-completed` when the run had finished (409). Other failures (403, 404, 5xx) raise."""
        response = self.client.request("POST", f"repos/{repo}/actions/runs/{run_id}/cancel", ok=(202,), tolerate=(409,))
        return "already-completed" if response.status == 409 else "cancelled"

    def candidate_run(
        self, repo: str, run: dict, *, with_jobs: bool = True, with_diagnostics: bool = True
    ) -> CandidateRun:
        run_id = str(run["id"])
        attempt = int(run.get("run_attempt") or 1)
        jobs: list[dict] = []
        if with_jobs:
            jobs = self.client.paginate(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs")
        phases: list[CandidatePhase] = []
        runner_name = None
        runner_labels: tuple[str, ...] = ()
        for job in jobs:
            name = str(job.get("name") or "")
            if job.get("runner_name") and (runner_name is None or name == BUILD_JOB):
                runner_name = str(job["runner_name"])
                runner_labels = tuple(str(label) for label in job.get("labels") or [])
            phases.append(
                CandidatePhase(
                    name,
                    _timestamp(job.get("started_at")),
                    _timestamp(job.get("completed_at")),
                    _phase_status(job),
                    str(job.get("conclusion") or ""),
                )
            )
            for step in job.get("steps") or []:
                phases.append(
                    CandidatePhase(
                        f"{name}: {step.get('name')}",
                        _timestamp(step.get("started_at")),
                        _timestamp(step.get("completed_at")),
                        _phase_status(step),
                        str(step.get("conclusion") or ""),
                    )
                )
        completed = run.get("status") == "completed"
        findings: list[dict] = []
        if completed and with_diagnostics and run.get("conclusion") != "success":
            findings = self.run_diagnostics(repo, run, jobs)
        return CandidateRun(
            run_id=run_id,
            attempt=attempt,
            status=str(run.get("status") or ""),
            conclusion=run.get("conclusion"),
            created_at=_timestamp(run.get("created_at")),
            started_at=_timestamp(run.get("run_started_at")),
            completed_at=_timestamp(run.get("updated_at")) if completed else None,
            runner_name=runner_name,
            runner_labels=runner_labels,
            host=runner_name,
            phases=tuple(phases),
            diagnostics=tuple(findings),
            display_title=str(run.get("display_title") or ""),
            html_url=str(run.get("html_url") or ""),
            updated_at=_timestamp(run.get("updated_at")),
        )

    def run_diagnostics(self, repo: str, run: dict, jobs: Sequence[dict]) -> list[dict]:
        """Source findings from the failed build job's log plus infrastructure findings of the run."""
        findings: list[dict] = []
        for job in jobs:
            if job.get("conclusion") == "failure" and str(job.get("name") or "") == BUILD_JOB and job.get("id"):
                log = self.job_log(repo, int(job["id"]))
                if log is not None:
                    findings.extend(diagnostics.parse_log(log.splitlines()))
                else:
                    findings.append(
                        {
                            "kind": "infrastructure",
                            "module": "",
                            "message": f"log of job {job['id']} unavailable",
                            "reason": "log_unavailable",
                        }
                    )
        findings.extend(diagnostics.infrastructure_findings(run, jobs))
        return findings

    def job_log(self, repo: str, job_id: int) -> str | None:
        """A job's plain-text log: the API answers with a redirect to a signed URL fetched bare."""
        response = self.client.request(
            "GET", f"repos/{repo}/actions/jobs/{job_id}/logs", ok=(200,), tolerate=(301, 302, 307, 404, 410)
        )
        if response.status == 200:
            return response.body[:MAX_LOG_BYTES].decode("utf-8", errors="replace")
        location = response.headers.get("location")
        if response.status in (404, 410) or not location:
            return None
        status, body = self.client.fetch_bare(location)
        if status != 200:
            return None
        return body[:MAX_LOG_BYTES].decode("utf-8", errors="replace")

    def workflow_run(self, repo: str, run_id, *, with_jobs: bool = True) -> CandidateRun | None:
        status, data = self.client.get_json(f"repos/{repo}/actions/runs/{run_id}", tolerate=(404,))
        if status == 404 or not isinstance(data, dict):
            return None
        return self.candidate_run(repo, data, with_jobs=with_jobs)

    def commit_status(self, repo: str, sha: str, context: str) -> Status | None:
        """The latest commit status for `context` on `sha`, with the run its target URL names."""
        status, data = self.client.get_json(f"repos/{repo}/commits/{sha}/status", tolerate=(404,))
        if status == 404 or not isinstance(data, dict):
            return None
        for entry in data.get("statuses") or []:
            if isinstance(entry, dict) and entry.get("context") == context:
                return Status(
                    state=str(entry.get("state") or ""),
                    description=str(entry.get("description") or ""),
                    target_url=str(entry.get("target_url") or ""),
                    updated_at=_timestamp(entry.get("updated_at")),
                    run_id=run_id_from_url(entry.get("target_url")),
                )
        return None

    def declaration_names(self, repo: str, head_sha: str) -> dict:
        """The exact accepted head's latest successful build, its trusted workflow and one job.

        The status URL must name this repository, and only the recorded run attempt's
        `sandboxed-build` log supplies evidence. No search over unrelated runs or candidate logs.
        Transient API failures raise for the controller's bounded background retry; absent
        evidence is explicitly unknown and never delays admission or dispatch.
        """
        _, statuses = self.client.get_json(f"repos/{repo}/commits/{head_sha}/statuses", params={"per_page": 100})
        build = (
            next((s for s in statuses if isinstance(s, dict) and s.get("context") == "build"), None)
            if isinstance(statuses, list)
            else None
        )
        if not build or build.get("state") != "success":
            return declaration_names.unknown("successful_build_unavailable")
        url = str(build.get("target_url") or "")
        match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/([1-9][0-9]*)", url)
        if match is None:
            return declaration_names.unknown("untrusted_build_url")
        run_id = match.group(1)
        code, run = self.client.get_json(f"repos/{repo}/actions/runs/{run_id}", tolerate=(404,))
        if code == 404 or not isinstance(run, dict) or run.get("path") != ".github/workflows/pr-build.yml":
            return declaration_names.unknown("untrusted_build_workflow")
        attempt = run.get("run_attempt")
        if type(attempt) is not int or attempt < 1:
            return declaration_names.unknown("invalid_run_attempt")
        _, result = self.client.get_json(
            f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", params={"per_page": 100}
        )
        jobs = result.get("jobs") if isinstance(result, dict) else None
        matching = (
            [j for j in jobs if isinstance(j, dict) and j.get("name") == BUILD_JOB] if isinstance(jobs, list) else []
        )
        if len(matching) != 1 or (result.get("total_count") or len(jobs or [])) > 100:
            return declaration_names.unknown("build_job_ambiguous")
        job = matching[0]
        if type(job.get("id")) is not int or job.get("status") != "completed" or job.get("conclusion") != "success":
            return declaration_names.unknown("successful_build_job_unavailable")
        log = self.job_log(repo, job["id"])
        if log is None:
            return declaration_names.unknown("log_unavailable")
        if len(log.encode("utf-8")) >= MAX_LOG_BYTES:
            return declaration_names.unknown("log_limit")
        record = declaration_names.parse_log(log, head_sha)
        return {**record, "run_id": run_id, "run_attempt": attempt, "job_id": job["id"]}

    def eligibility_checks(self, repo: str, head: str) -> list[dict]:
        """Every `merge eligibility` check run on `head`, any App (`filter=all`), as Bors lists them."""
        return self.client.paginate(
            f"repos/{repo}/commits/{head}/check-runs",
            params={"check_name": "merge eligibility", "filter": "all"},
            key="check_runs",
        )

    def _diff_version(self, base: str, head: str) -> str:
        if (
            not self.clone_dir
            or not chain.object_exists(self.clone_dir, base)
            or not chain.object_exists(self.clone_dir, head)
        ):
            return "unknown:" + base
        bases = sorted(chain.git(self.clone_dir, "merge-base", "--all", base, head).split())
        if not bases:
            raise GitHubError("PR and target have no merge base")
        # Advancing main normally leaves the reviewed diff unchanged. Multiple merge bases are
        # ambiguous: retain the target identity and let the compare API resolve a fresh decision.
        return bases[0] if len(bases) == 1 else ",".join([*bases, base])

    def admission_snapshot(self, repo: str, number: int, *, generation=0) -> ports.AdmissionSnapshot:
        """Read complete current inputs and the exact reviewed diff; never run a PR's code."""
        progress = self._admission_reads.get((repo, number))
        if progress is None or progress["generation"] != generation:
            pull = self.fetch_pr(repo, number)
            progress = {"generation": generation, "pull": pull}
            self._admission_reads[(repo, number)] = progress
        else:
            pull = progress["pull"]
        if pull.state != "open" or pull.draft:
            self._admission_reads.pop((repo, number), None)
            return ports.AdmissionSnapshot(pull, "", (), (), {})
        if not self.clone_dir or not SHA.fullmatch(pull.head_sha) or not SHA.fullmatch(pull.base_sha):
            raise GitHubError("admission needs a clone and complete head/target identities")
        missing = [sha for sha in (pull.base_sha, pull.head_sha) if not chain.object_exists(self.clone_dir, sha)]
        if missing:
            self.fetch_refs(repo, missing)
        # The pending head stays reachable across local GC until it is replaced or closed.
        chain.git(self.clone_dir, "update-ref", f"{chain.REF_NAMESPACE}/admission/{number}", pull.head_sha)
        if "merge_base" not in progress:
            _, compare = self.client.get_json(
                f"repos/{repo}/compare/{pull.base_sha}...{pull.head_sha}", params={"per_page": 1}
            )
            progress["merge_base"] = ((compare or {}).get("merge_base_commit") or {}).get("sha")
        merge_base = progress["merge_base"]
        if not isinstance(merge_base, str) or not SHA.fullmatch(merge_base):
            raise GitHubError("compare did not return a complete merge base")
        if not chain.object_exists(self.clone_dir, merge_base):
            self.fetch_refs(repo, [merge_base])
        paths = tuple(
            p
            for p in chain.git(
                self.clone_dir,
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=none",
                merge_base,
                pull.head_sha,
                "--",
            ).split("\0")
            if p
        )
        comments = tuple(
            self.client.paginate(f"repos/{repo}/issues/{number}/comments", progress=progress.setdefault("comments", {}))
        )
        fresh, statuses = self._admission_fence(repo, number, pull, labels_progress=progress.setdefault("labels", {}))
        if (
            fresh.head_sha,
            fresh.base_sha,
            fresh.base_ref,
            fresh.state,
            fresh.draft,
            tuple(sorted(fresh.labels)),
            fresh.body,
            _timestamp(fresh.updated_at),
            fresh.comments_count,
        ) != (
            pull.head_sha,
            pull.base_sha,
            pull.base_ref,
            pull.state,
            pull.draft,
            tuple(sorted(pull.labels)),
            pull.body,
            _timestamp(pull.updated_at),
            pull.comments_count,
        ):
            self._admission_reads.pop((repo, number), None)
            raise GitHubError("PR inputs changed while admission was reading source evidence")
        self._admission_reads.pop((repo, number), None)
        return ports.AdmissionSnapshot(
            fresh, merge_base, paths, comments, statuses, self._diff_version(pull.base_sha, pull.head_sha)
        )

    def _admission_fence(self, repo, number, pull, *, labels_progress=None):
        """Current head/target and CI together, after paced or paginated source reads.

        Statuses can change without moving PR updatedAt. Read the commit by SHA, avoiding
        both resumed newest-first status pages and PR commit-connection caps.
        """
        owner, name = repo.split("/", 1)
        # Finish a prepared decision before spending the next background discovery slot.
        # This still obeys the GraphQL bucket and secondary-limit deadlines.
        with self.client.deferred_requests(urgent=True):
            data = self.client.graphql(
                ADMISSION_FENCE_QUERY, {"owner": owner, "name": name, "number": number, "head": pull.head_sha}
            )
        repository = data.get("repository") or {}
        node, commit = repository.get("pullRequest"), repository.get("object")
        if not isinstance(node, dict) or not isinstance(commit, dict) or commit.get("oid") != pull.head_sha:
            raise GitHubError("incomplete admission head/CI fence")
        labels = node.get("labels") or {}
        if "pageInfo" not in labels:
            raise GitHubError("incomplete admission label fence")
        names = (
            self.labels(repo, number, progress=labels_progress)
            if labels["pageInfo"].get("hasNextPage")
            else [s["name"] for s in labels.get("nodes", ())]
        )
        fresh = dataclasses.replace(
            pull,
            head_sha=node.get("headRefOid", ""),
            base_sha=node.get("baseRefOid", ""),
            base_ref=node.get("baseRefName", ""),
            state=str(node.get("state") or "").lower(),
            draft=bool(node.get("isDraft")),
            body=node.get("body") or "",
            title=node.get("title") or pull.title,
            updated_at=node.get("updatedAt") or "",
            comments_count=(node.get("comments") or {}).get("totalCount", 0),
            labels=tuple(names),
        )
        statuses = {
            s["context"]: str(s.get("state") or "").lower()
            for s in (commit.get("status") or {}).get("contexts", ())
            if s.get("context") in ("build", "scope", "bump-guard")
        }
        return fresh, statuses

    def _admission_pulls(self, repo, on_page):
        owner, name = repo.split("/", 1)
        progress = self._admission_listing.setdefault(
            repo, {"listed": [], "cursor": None, "connection": None, "index": 0, "batch": []}
        )
        while True:
            cursor = progress["cursor"]
            if progress["connection"] is None:
                data = self.client.graphql(ADMISSION_PULLS_QUERY, {"owner": owner, "name": name, "after": cursor})
                progress["connection"] = (data.get("repository") or {}).get("pullRequests")
            connection = progress["connection"]
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise GitHubError("incomplete PR discovery")
            batch = progress["batch"]
            for node in connection["nodes"][progress["index"] :]:
                if not isinstance(node, dict) or not isinstance(node.get("number"), int):
                    raise GitHubError("incomplete PR discovery node")
                try:
                    observed = self._admission_node(repo, node, labels_progress=progress.setdefault("labels", {}))
                except RequestDeferred:
                    raise
                except Exception as error:
                    # A known PR with incomplete evidence stays dirty. It is not evidence that
                    # later PRs disappeared, and must not restart repository discovery at page one.
                    self._admission_comment_pages.pop((repo, node["number"]), None)
                    observed = ports.OpenPullRequest(
                        node["number"],
                        node.get("headRefOid", ""),
                        node.get("baseRefName", ""),
                        bool(node.get("isDraft")),
                        admission={"incomplete": str(error)},
                    )
                batch.append(observed)
                progress.pop("labels", None)
                progress["index"] += 1
            if on_page is not None:
                on_page(batch)
            progress["listed"].extend(batch)
            page = connection.get("pageInfo")
            if not isinstance(page, dict) or (page.get("hasNextPage") and not page.get("endCursor")):
                raise GitHubError("incomplete PR pagination")
            if not page.get("hasNextPage"):
                self._admission_listing.pop(repo, None)
                return progress["listed"]
            if page["endCursor"] == cursor:
                raise GitHubError("PR pagination did not advance")
            progress.update(cursor=page["endCursor"], connection=None, index=0, batch=[])

    def _admission_node(self, repo, node, *, labels_progress=None):
        number, head, base = node["number"], node.get("headRefOid", ""), node.get("baseRefOid", "")
        # Fetch a new target once, so a normal canonical advance doesn't invalidate every
        # unchanged review. PR heads are fetched only when evaluating new/dirty work.
        if self.clone_dir and SHA.fullmatch(base) and not chain.object_exists(self.clone_dir, base):
            self.fetch_refs(repo, [base])
        comments = self._admission_comments(repo, number, node.get("comments"))
        label_connection = node.get("labels") or {}
        page = label_connection.get("pageInfo")
        if not isinstance(page, dict):
            raise GitHubError("incomplete label connection")
        labels = (
            self.labels(repo, number, progress=labels_progress)
            if page.get("hasNextPage")
            else [s["name"] for s in label_connection.get("nodes", ())]
        )
        commits = (node.get("commits") or {}).get("nodes") or []
        commit = (commits[-1].get("commit") or {}) if commits else {}
        if commit.get("oid") != head:
            raise GitHubError("discovery commit does not match the PR head")
        statuses = {
            s["context"]: str(s.get("state") or "").lower()
            for s in (commit.get("status") or {}).get("contexts", ())
            if s.get("context") in ("build", "scope", "bump-guard")
        }
        source = {
            "head": head,
            "base_ref": node.get("baseRefName", ""),
            "state": "open",
            "draft": bool(node.get("isDraft")),
            "body": node.get("body", ""),
            "labels": labels,
            "comments": comments,
            "statuses": statuses,
            "diff_version": self._diff_version(base, head),
        }
        return ports.OpenPullRequest(number, head, source["base_ref"], source["draft"], admission=source)

    def _admission_comments(self, repo, number, connection):
        owner, name = repo.split("/", 1)
        progress = self._admission_comment_pages.setdefault((repo, number), {"comments": [], "cursor": None})
        comments, cursor = progress["comments"], progress["cursor"]
        if cursor:
            data = self.client.graphql(
                ADMISSION_COMMENTS_QUERY, {"owner": owner, "name": name, "number": number, "after": cursor}
            )
            connection = ((data.get("repository") or {}).get("pullRequest") or {}).get("comments")
        while True:
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise GitHubError("incomplete review comments")
            for comment in connection["nodes"]:
                if not isinstance(comment, dict) or not isinstance(comment.get("databaseId"), int):
                    raise GitHubError("incomplete review comment")
                author = comment.get("author") or {}
                login = author.get("login", "")
                # GraphQL Bot.login omits REST's suffix. Preserve the same actor identity
                # for trusted recovery holds and stable source versions across both APIs.
                if author.get("__typename") == "Bot" and login and not login.endswith("[bot]"):
                    login += "[bot]"
                comments.append(
                    {
                        "id": comment["databaseId"],
                        "body": comment.get("body", ""),
                        "created_at": comment.get("createdAt", ""),
                        "updated_at": comment.get("updatedAt", ""),
                        "html_url": comment.get("url", ""),
                        "author": login,
                    }
                )
            page = connection.get("pageInfo")
            if not isinstance(page, dict) or (page.get("hasNextPage") and not page.get("endCursor")):
                raise GitHubError("incomplete review comment pagination")
            if not page.get("hasNextPage"):
                self._admission_comment_pages.pop((repo, number), None)
                return comments
            if page["endCursor"] == cursor:
                raise GitHubError("review comment pagination did not advance")
            cursor = page["endCursor"]
            progress["cursor"] = cursor
            data = self.client.graphql(
                ADMISSION_COMMENTS_QUERY, {"owner": owner, "name": name, "number": number, "after": cursor}
            )
            connection = ((data.get("repository") or {}).get("pullRequest") or {}).get("comments")

    def list_open_pull_requests(
        self, repo: str, *, review_app_id: int, admission=False, on_page=None
    ) -> list[ports.OpenPullRequest]:
        """Every open PR with the review App's `merge eligibility` check runs on its head.

        One GraphQL page per 100 PRs: the PR's head, draft flag and base, and from the head commit
        the review App's check suite (`checkSuites(filterBy: {appId})`) with its `merge eligibility`
        runs, mapped to the REST record shape `eligibility.latest` reads. Twelve calls for 1,200
        open PRs, on the GraphQL quota rather than the REST one. The suite's `app` is null when read
        with an installation token (observed on production, 2026-11-04), so the review App id the
        filter selected by is what the mapped records carry.
        """
        if admission:
            try:
                return self._admission_pulls(repo, on_page)
            except RequestDeferred:
                raise
            except Exception:
                # Invalid/partial GraphQL data is never promoted into a completed source view.
                self._admission_listing.pop(repo, None)
                self._admission_comment_pages = {k: v for k, v in self._admission_comment_pages.items() if k[0] != repo}
                raise
        owner, name = repo.split("/", 1)
        listed: list[ports.OpenPullRequest] = []
        cursor = None
        while True:
            data = self.client.graphql(
                OPEN_PULLS_QUERY,
                {"owner": owner, "name": name, "after": cursor, "appId": int(review_app_id)},
            )
            connection = ((data.get("repository") or {}).get("pullRequests")) or {}
            for node in connection.get("nodes") or []:
                if not isinstance(node, dict) or not isinstance(node.get("number"), int):
                    continue
                head = str(node.get("headRefOid") or "")
                checks = []
                for commit_node in ((node.get("commits") or {}).get("nodes")) or []:
                    commit = (commit_node or {}).get("commit") or {}
                    oid = str(commit.get("oid") or head)
                    for suite in ((commit.get("checkSuites") or {}).get("nodes")) or []:
                        # The suites were selected by `filterBy: {appId}`, so they are the review App's;
                        # through an installation token GraphQL returns `app: null` on another App's
                        # suite, and the REST record's `app.id` must still name it for `eligibility.trusted`.
                        app_id = ((suite or {}).get("app") or {}).get("databaseId")
                        if not isinstance(app_id, int):
                            app_id = int(review_app_id)
                        for run in (((suite or {}).get("checkRuns") or {}).get("nodes")) or []:
                            if isinstance(run, dict) and run.get("name") == "merge eligibility":
                                checks.append(_rest_check_run(run, oid, app_id))
                listed.append(
                    ports.OpenPullRequest(
                        number=node["number"],
                        head_sha=head,
                        base_ref=str(node.get("baseRefName") or ""),
                        draft=bool(node.get("isDraft")),
                        eligibility_checks=tuple(checks),
                    )
                )
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage") or not page.get("endCursor"):
                return listed
            cursor = page["endCursor"]

    def post_commit_status(
        self, repo: str, sha: str, *, context: str, state: str, description: str, target_url: str | None = None
    ):
        body = {"context": context, "state": state, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        self.client.request("POST", f"repos/{repo}/statuses/{sha}", json_body=body, ok=(201,))

    # -- comments and labels

    def post_comment(self, repo: str, number: int, body: str, marker: str) -> str:
        """Post `body` once per `marker`: a comment already carrying the marker is edited instead."""
        hidden = comment_marker(marker)
        existing = self.find_comment(repo, number, hidden)
        full = f"{body.rstrip()}\n\n{hidden}\n"
        if existing is not None:
            if existing.get("body") == full:
                return str(existing["id"])
            response = self.client.request(
                "PATCH", f"repos/{repo}/issues/comments/{existing['id']}", json_body={"body": full}
            )
            return str(response.json()["id"])
        response = self.client.request(
            "POST", f"repos/{repo}/issues/{number}/comments", json_body={"body": full}, ok=(201,)
        )
        return str(response.json()["id"])

    def find_comment(self, repo: str, number: int, needle: str) -> dict | None:
        for comment in self.client.paginate(f"repos/{repo}/issues/{number}/comments"):
            if isinstance(comment, dict) and needle in str(comment.get("body") or ""):
                return comment
        return None

    def set_labels(self, repo: str, number: int, add: Sequence[str], remove: Sequence[str]) -> None:
        if add:
            self.client.request("POST", f"repos/{repo}/issues/{number}/labels", json_body={"labels": list(add)})
        for label in remove:
            encoded = urllib.parse.quote(label, safe="")
            self.client.request("DELETE", f"repos/{repo}/issues/{number}/labels/{encoded}", tolerate=(404,))

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        """`POST labels`; GitHub's 422 `already_exists` leaves the existing label untouched."""
        self.client.request(
            "POST",
            f"repos/{repo}/labels",
            json_body={"name": name, "color": color, "description": description},
            ok=(201,),
            tolerate=(422,),
        )

    # -- people and limits

    def collaborator_permission(self, repo: str, login: str) -> str | None:
        """GitHub's effective permission for `login` (`admin`, `maintain`, `write`, `triage`, `read`), or None.

        Raises `ports.GitHubUnavailable` when GitHub could not answer (rate limit, 5xx, network error).
        """
        encoded = urllib.parse.quote(login, safe="")
        try:
            status, data = self.client.get_json(f"repos/{repo}/collaborators/{encoded}/permission", tolerate=(404,))
        except (GitHubError, OSError) as error:
            raise ports.GitHubUnavailable(f"permission lookup for {login}: {error}") from error
        if status == 404 or not isinstance(data, dict):
            return None
        role = data.get("role_name") or data.get("permission")
        return str(role) if role else None

    def repository_admin(self, login: str, repo: str | None = None) -> bool:
        return self.collaborator_permission(repo or self.repo, login) == "admin"

    def reviewer(self, login: str, repo: str | None = None) -> bool:
        """Bors's reviewer check: a collaborator with push access."""
        return self.collaborator_permission(repo or self.repo, login) in REVIEWER_PERMISSIONS

    def user_for_token(self, token: str) -> tuple[str, int] | None:
        """The login and id a bearer token authenticates as, or None when GitHub rejects it (401).

        Raises `ports.GitHubUnavailable` when GitHub could not answer (a rate-limited 403, 5xx, network
        error), so `server.BearerAuth` can keep serving the identity it already resolved.
        """
        try:
            status, data = self.client.get_json("user", token=token, tolerate=(401,))
        except (GitHubError, OSError) as error:
            raise ports.GitHubUnavailable(f"GET /user: {error}") from error
        if status != 200 or not isinstance(data, dict) or not data.get("login"):
            return None
        return str(data["login"]), int(data.get("id") or 0)

    def rate_limit(self) -> tuple[int, float]:
        remaining, reset_at = self.client.rate_limit()
        if remaining is None:
            _, data = self.client.get_json("rate_limit")
            core = ((data or {}).get("resources") or {}).get("core") or {}
            remaining, reset_at = core.get("remaining"), core.get("reset")
        return int(remaining or 0), float(reset_at or 0.0)

    def app(self) -> dict:
        """`GET /app` as the App itself; the read-only proof that the JWT path works."""
        if self._tokens is None:
            raise GitHubError("no App credentials configured")
        _, data = self.client.get_json("app", token=self._tokens.jwt())
        return data

    def installation_repositories(self) -> list[str]:
        data = self.client.paginate("installation/repositories", key="repositories")
        return [str(r.get("full_name")) for r in data if isinstance(r, dict)]


OPEN_PULLS_QUERY = """
query($owner: String!, $name: String!, $after: String, $appId: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number isDraft baseRefName headRefOid
        commits(last: 1) { nodes { commit { oid
          checkSuites(first: 1, filterBy: {appId: $appId}) { nodes { app { databaseId }
            checkRuns(last: 5, filterBy: {checkName: "merge eligibility"}) {
              nodes { databaseId name status conclusion externalId completedAt permalink } } } } } } }
      }
    }
  }
}
"""

ADMISSION_FENCE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $head: GitObjectID!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefOid baseRefOid baseRefName state isDraft body title updatedAt
      comments { totalCount }
      labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
    }
    object(oid: $head) { ... on Commit { oid status { contexts { context state } } } }
  }
}
"""

ADMISSION_COMMENT_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes { databaseId body createdAt updatedAt url author { login __typename } }
"""
ADMISSION_PULLS_QUERY = (
    """
query($owner: String!, $name: String!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number isDraft baseRefName baseRefOid headRefOid body
        labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
        comments(first: 100) { """
    + ADMISSION_COMMENT_FIELDS
    + """ }
        commits(last: 1) { nodes { commit { oid status { contexts { context state } } } } }
      }
    }
  }
}
"""
)
ADMISSION_COMMENTS_QUERY = (
    """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { comments(first: 100, after: $after) { """
    + ADMISSION_COMMENT_FIELDS
    + """ } }
  }
}
"""
)


def _rest_check_run(run: dict, head_sha: str, app_id) -> dict:
    """A GraphQL `CheckRun` node in the REST `check_runs[]` shape (`eligibility.decode` reads that)."""
    conclusion = run.get("conclusion")
    return {
        "id": run.get("databaseId"),
        "name": run.get("name"),
        "app": {"id": app_id},
        "head_sha": head_sha,
        "status": str(run.get("status") or "").lower(),
        "conclusion": str(conclusion).lower() if conclusion else None,
        "external_id": run.get("externalId"),
        "completed_at": run.get("completedAt") or "",
        "html_url": run.get("permalink") or "",
    }


def _phase_status(entry: dict) -> str:
    """The port's phase status (`queued`, `in_progress`, `completed`, `skipped`)."""
    status = str(entry.get("status") or "")
    if status == "completed" and entry.get("conclusion") == "skipped":
        return "skipped"
    return status or "queued"


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


# ---------------------------------------------------------------------------------------------
# Polling fallback


@dataclasses.dataclass(frozen=True)
class CandidateRef:
    repo: str
    batch_id: int | str
    head_sha: str
    base_sha: str
    base_ref: str = "main"
    dispatched_at: float | None = None


class CandidatePoller:
    """Bounded polling behind the webhooks: at most one run lookup per candidate per `interval`.

    The controller hands over its pending candidates; each is looked up at most once per interval,
    and nothing is looked up while the remaining primary quota is below `reserve`. The verdict
    status for the candidate head is read in the same pass so a missed `status` webhook is covered.
    """

    def __init__(self, adapter: GitHubAdapter, *, interval: float = 60.0, reserve: int = 200, clock=time.time) -> None:
        self.adapter = adapter
        self.interval = interval
        self.reserve = reserve
        self._clock = clock
        self._last: dict[tuple[str, str, str], float] = {}
        self.polls = 0
        self.skipped = 0

    def due(self, ref: CandidateRef) -> bool:
        key = (ref.repo, str(ref.batch_id), ref.head_sha)
        return self._clock() - self._last.get(key, 0.0) >= self.interval

    def poll(
        self, refs: Iterable[CandidateRef], *, status_context: str = "build"
    ) -> list[tuple[CandidateRef, CandidateRun | None, Status | None]]:
        results = []
        for ref in refs:
            if not self.due(ref):
                continue
            remaining, _ = self.adapter.rate_limit()
            if remaining is not None and remaining < self.reserve:
                self.skipped += 1
                continue
            self._last[(ref.repo, str(ref.batch_id), ref.head_sha)] = self._clock()
            self.polls += 1
            run = self.adapter.find_candidate_run(
                ref.repo,
                batch_id=ref.batch_id,
                head_sha=ref.head_sha,
                base_sha=ref.base_sha,
                base_ref=ref.base_ref,
                since=ref.dispatched_at,
            )
            status = self.adapter.commit_status(ref.repo, ref.head_sha, status_context)
            results.append((ref, run, status))
        return results

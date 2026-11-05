"""The worker-side repair task of the AI merge queue (increment 1, stream G).

A worker claims a repair or rebase-conflict task from the queue service, prepares warm build
artifacts, runs the repair agent in a private worktree and returns a validated attributed chain. The
service alone adopts a chain; this module never publishes anything except the proposed tip under
the task's submission ref in the content repository, which the service fetches, and the green
tip's build outputs to the workers' own cache namespace (``repair_publish``), which CI never reads.

Task protocol (``docs/queue/repair-task.md``), all JSON over HTTP with
``Authorization: Bearer <GitHub token of the worker's account>``:

- ``POST /tasks/claim`` ``{worker}`` returns 204 when nothing is claimable, else the controller's
  ``Task`` record (``tauceti_queue/ports.py``: ``task_id``, ``kind``, ``base``, ``tip``, ``onto``,
  ``members``, ``diagnostics``, ``retained_refs``, ``submission_ref``, ``claim_token``, ``expires_at``,
  ``instruction``);
- ``POST /tasks/<id>/renew`` ``{claim_token}`` returns the new ``expires_at``;
- ``POST /tasks/<id>/result`` ``{claim_token, tip_sha, worker_notes}`` returns the ``Adoption``
  record (``accepted``, ``reason``, ``violations``, ``retained_ref``, ``version``);
- ``POST /tasks/<id>/release`` ``{claim_token, reason, error}`` gives the task back unfinished with a
  named reason (``artifacts_unavailable``, ``inference_unavailable``, ``agent_launch_failed``,
  ``agent_failed``, ``push_failed``); when the service has no such endpoint the worker lets the claim
  expire instead.

The proposed chain is pushed to the task's ``submission_ref``
(``refs/tauceti-queue/submissions/<task_id>/<claim_id>``) before ``result`` names its tip.

Authorization is the security audit's predicate (``security_audit.require_admin``): the account must
have admin permission on the repository, checked before any claim so a misconfigured worker fails
before it does work.

Phases, each checkpointed in ``<root>/tasks/<task_id>/task.json`` so a restarted runner can resume
the same model session instead of starting over: claimed, sources_ready, artifacts_ready,
agent_running, validated, pushed, submitted, then adopted / not_adopted / released / claim_lost.

CLI: ``python -m tauceti_queue.repair_task --service <url> [--once | --check]``. Without ``--once`` the
runner is a long-running worker (``serve``): it resumes checkpointed tasks, claims, runs and submits,
reports provider outcomes to ``POST /tasks/inference`` for the service's inference hold, sleeps the
poll interval when nothing is claimable, survives a task's failure, and on SIGTERM stops the running
agent while keeping its claim and checkpoint so the next start resumes the same session
(``services/queue/tauceti-queue-repair.service``). The same ``RunnerConfig`` and ``claim_and_run`` are
what the worker loop calls when the task is integrated as a work unit.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from tauceti_queue import repair, repair_context, repair_parallel, repair_publish, repair_sandbox, repair_stream
from tauceti_queue.repair import PromptContext, checkout_worktree, git, read_chain

SCHEMA = "tauceti-queue.repair-task/v1"
RESULT_REF_PREFIX = "refs/tauceti-queue/task"
SUBMISSION_PREFIX = "refs/tauceti-queue/submissions"
RELEASE_REASONS = (
    "artifacts_unavailable",
    "inference_unavailable",
    "agent_launch_failed",
    "agent_failed",
    "push_failed",
    "worker-stopped",  # the worker was stopped (SIGTERM) before the task reached a resumable phase
)
# Checkpoint states with the claim still ours and the task unfinished; everything else is settled.
IN_FLIGHT_STATES = (
    "claimed",
    "sources_ready",
    "artifacts_ready",
    "artifacts_unavailable",
    "agent_running",
    "validated",
    "pushed",
    "submitted",
    "interrupted",
)
# In-flight states a restarted worker continues instead of releasing: the agent's session exists.
RESUMABLE_STATES = ("agent_running", "interrupted")
# ``run_repair_agent`` statuses that release the task under the same name.
RELEASED_AGENT_STATUSES = ("inference_unavailable", "artifacts_unavailable", "agent_launch_failed")

PROVIDERS = repair.PROVIDERS  # claude | codex | api, as the chain names them
DEFAULT_PROVIDERS = ("claude", "codex", "api")
SUBSCRIPTION_PROVIDERS = ("claude", "codex")  # paced by the fleet's quota reader; ``api`` is not
# Checkpoint states before the agent has run: an error there is released as ``artifacts_unavailable``.
PRE_AGENT_PHASES = (None, "claimed", "sources_ready", "artifacts_ready", "artifacts_unavailable")
HERE = Path(__file__).resolve().parent.parent
GIT_SAFE_PUSH = HERE / "scripts" / "git-safe-push"
DEFAULT_ROOT = Path(
    os.environ.get("TAUCETI_QUEUE_TASK_ROOT")
    or (Path(os.environ["TAUCETI_ACTIVITY_DIR"]) / "queue-repair" if os.environ.get("TAUCETI_ACTIVITY_DIR") else "")
    or Path.home() / ".local" / "state" / "tauceti" / "queue-repair"
)


def log(message: str) -> None:
    try:
        from tauceti_worker.config import log as worker_log

        worker_log(f"queue-repair: {message}")
    except Exception:
        print(f"queue-repair: {message}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------------------------
# Task records and the service client


class ServiceError(Exception):
    """The service could not be reached or answered 5xx; the call may be retried."""


class ClaimRefused(Exception):
    """The service no longer recognises this claim (expired, superseded or never ours)."""


class EndpointMissing(Exception):
    """The service has no such endpoint (an older service without ``release``)."""


CLAIM_CODES = ("unknown-task", "unknown-claim", "claim-expired", "claim-superseded", "not-authorized")
TERMINAL_REJECTIONS = ("version-mismatch", "tip-unavailable", "not-authorized", "unknown-task")


@dataclasses.dataclass
class TaskMember:
    """A member as the controller's ``ports.TaskMember`` describes it."""

    source_pr: str
    number: int
    accepted_head: str
    commit: str | None = None
    provenance: str | None = None
    title: str = ""
    description: str = ""
    author: str = ""
    explanation: str = ""
    retained_ref: str = ""

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Task:
    """The controller's ``ports.Task`` as the worker reads it from the claim response.

    ``base`` and ``tip`` are the chain to start from; ``onto`` is the commit the replacement must be
    based on (the canonical commit for a rebase conflict, ``base`` itself for a repair);
    ``submission_ref`` is where the result is pushed; ``instruction`` is the agent instruction the
    service sends (the plan's, verbatim). ``renew_after`` is not part of the controller's record: it
    is ``extra.renew_after`` when the service sets it, else a third of the claim's lifetime.
    """

    task_id: str
    kind: str  # repair | rebase_conflict
    repository: str
    batch_id: str
    batch_version: int
    base: str
    tip: str
    onto: str
    members: list[TaskMember]
    diagnostics: list | dict
    retained_refs: list[str]
    submission_ref: str
    claim_token: str
    expires_at: float
    renew_after: float
    instruction: str = ""
    created_at: float | None = None
    extra: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> Task:
        for key in ("task_id", "kind", "repository", "base", "tip", "members", "claim_token", "expires_at"):
            if key not in data:
                raise ValueError(f"task record lacks {key!r}")
        if data["kind"] not in ("repair", "rebase_conflict"):
            raise ValueError(f"unknown task kind {data['kind']!r}")
        repository = str(data["repository"])
        members = []
        for raw in data["members"]:
            number = raw.get("number", raw.get("pr"))
            source_pr = raw.get("source_pr") or f"{repository}#{number}"
            members.append(
                TaskMember(
                    source_pr=str(source_pr),
                    number=int(number if number is not None else source_pr.rsplit("#", 1)[1]),
                    accepted_head=str(raw.get("accepted_head") or ""),
                    commit=raw.get("commit"),
                    provenance=raw.get("provenance"),
                    title=str(raw.get("title") or ""),
                    description=str(raw.get("description") or ""),
                    author=str(raw.get("author") or ""),
                    explanation=str(raw.get("explanation") or ""),
                    retained_ref=str(raw.get("retained_ref") or ""),
                )
            )
        extra = dict(data.get("extra") or {})
        expires_at = float(data["expires_at"])
        diagnostics = data.get("diagnostics")
        if not isinstance(diagnostics, (list, dict)):
            diagnostics = []
        conflict = (
            next(
                (d for d in diagnostics if isinstance(d, dict) and d.get("kind") == "conflict"),
                None,
            )
            if isinstance(diagnostics, list)
            else diagnostics.get("conflict")
        )
        onto = data.get("onto") or (conflict or {}).get("onto") or data["base"]
        task_id = str(data["task_id"])
        submission_ref = data.get("submission_ref") or (
            f"{SUBMISSION_PREFIX}/{task_id}/{extra.get('claim_id', data['claim_token'])}"
        )
        return cls(
            task_id=task_id,
            kind=str(data["kind"]),
            repository=repository,
            batch_id=str(data.get("batch_id", "")),
            batch_version=int(data.get("batch_version") or 0),
            base=str(data["base"]),
            tip=str(data["tip"]),
            onto=str(onto),
            members=members,
            diagnostics=diagnostics,
            retained_refs=list(data.get("retained_refs") or []),
            submission_ref=str(submission_ref),
            claim_token=str(data["claim_token"]),
            expires_at=expires_at,
            renew_after=float(
                data.get("renew_after") or extra.get("renew_after") or max(5.0, (expires_at - time.time()) / 3)
            ),
            instruction=str(data.get("instruction") or ""),
            created_at=float(data["created_at"]) if data.get("created_at") is not None else None,
            extra=extra,
        )

    def to_json(self) -> dict:
        record = dataclasses.asdict(self)
        record["members"] = [m.to_json() for m in self.members]
        return record

    @property
    def member_keys(self) -> list[str]:
        return [m.source_pr for m in self.members]

    @property
    def validation_base(self) -> str:
        """The base the result must sit on: ``onto`` (the batch base itself for a repair)."""
        return self.onto or self.base


class TaskClient:
    """The worker's side of the task protocol. Transport failures raise ``ServiceError``.

    Status codes: the service answers a claim that is no longer the caller's with 403/404/409/410, or
    (through stream F's generic error mapping of ``TaskError``) with a JSON ``error``/``code`` naming
    one of ``unknown-task``, ``unknown-claim``, ``claim-expired``, ``claim-superseded``,
    ``not-authorized``; both are ``ClaimRefused``. 5xx without such a code is ``ServiceError``.
    """

    def __init__(self, service: str, token: str, worker: str, timeout: float = 30.0):
        self.service = service.rstrip("/")
        self.token = token
        self.worker = worker
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> tuple[int, dict | None]:
        data = json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.service}{path}",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"tauceti-queue-repair-task/{self.worker}",
                "X-TauCeti-Worker": self.worker,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            with error:
                payload = error.read()
        except (urllib.error.URLError, OSError) as error:
            raise ServiceError(f"{path}: {error}") from error
        parsed = None
        if payload:
            try:
                parsed = json.loads(payload)
            except ValueError as error:
                if status >= 500:
                    raise ServiceError(f"{path}: HTTP {status}") from error
                raise ServiceError(f"{path}: response is not JSON") from error
        if status >= 400 and _claim_code(parsed):
            raise ClaimRefused(f"{path}: HTTP {status} {_claim_code(parsed)}: {_error_text(parsed)}".strip())
        if status >= 500:
            raise ServiceError(f"{path}: HTTP {status} {_error_text(parsed)}".strip())
        return status, parsed

    def claim(self) -> Task | None:
        status, payload = self._post("/tasks/claim", {"worker": self.worker})
        if status == 204 or (status == 200 and not payload):
            return None
        if status in (401, 403):
            raise ClaimRefused(f"claim refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200 or not isinstance(payload, dict):
            raise ServiceError(f"claim: unexpected HTTP {status} {_error_text(payload)}".strip())
        return Task.from_json(payload)

    def renew(self, task: Task) -> float:
        status, payload = self._post(
            f"/tasks/{task.task_id}/renew", {"worker": self.worker, "claim_token": task.claim_token}
        )
        if status in (401, 403, 404, 409, 410):
            raise ClaimRefused(f"renew refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200:
            raise ServiceError(f"renew: unexpected HTTP {status}")
        # The controller returns the new expiry as a number; a dict form ``{expires_at}`` is accepted too.
        value = payload.get("expires_at") if isinstance(payload, dict) else payload
        if not isinstance(value, (int, float)):
            raise ServiceError("renew: no expires_at in the response")
        return float(value)

    def submit(self, task: Task, tip: str, pushed_ref: str, notes: dict | None) -> dict:
        """``submit_task(task_id, claim_token, tip_sha, worker_notes)``; returns the Adoption record."""
        status, payload = self._post(
            f"/tasks/{task.task_id}/result",
            {
                "worker": self.worker,
                "claim_token": task.claim_token,
                "tip_sha": tip,
                "pushed_ref": pushed_ref,
                "worker_notes": json.dumps(notes or {}, sort_keys=True, default=str),
            },
        )
        if status in (401, 403, 404, 409, 410):
            raise ClaimRefused(f"result refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200 or not isinstance(payload, dict):
            raise ServiceError(f"result: unexpected HTTP {status}")
        if "accepted" not in payload and "adopted" in payload:
            payload["accepted"] = bool(payload["adopted"])
        return payload

    def release(self, task: Task, reason: str, error: str = "") -> dict:
        """``release_task(task_id, claim_token, reason, error)``."""
        status, payload = self._post(
            f"/tasks/{task.task_id}/release",
            {"worker": self.worker, "claim_token": task.claim_token, "reason": reason, "error": error or ""},
        )
        if status in (404, 405, 501) and not (isinstance(payload, dict) and payload.get("task_id")):
            raise EndpointMissing(f"release: HTTP {status}")
        if status in (401, 403, 409, 410):
            raise ClaimRefused(f"release refused: HTTP {status} {_error_text(payload)}".strip())
        if status not in (200, 204):
            raise ServiceError(f"release: unexpected HTTP {status}")
        return payload if isinstance(payload, dict) else {"released": True}

    def report_inference(self, ok: bool, error: str = "") -> None:
        """``report_inference(worker, ok, error)``: a provider outcome for the service's inference hold."""
        status, payload = self._post("/tasks/inference", {"worker": self.worker, "ok": ok, "error": error or ""})
        if status in (404, 405, 501):
            raise EndpointMissing(f"inference: HTTP {status}")
        if status in (401, 403):
            raise ClaimRefused(f"inference report refused: HTTP {status} {_error_text(payload)}".strip())
        if status not in (200, 204):
            raise ServiceError(f"inference: unexpected HTTP {status} {_error_text(payload)}".strip())


def _claim_code(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("code", "reason", "error"):
        value = payload.get(key)
        if isinstance(value, str):
            for code in CLAIM_CODES:
                if code in value:
                    return code
    return None


def _error_text(payload) -> str:
    if isinstance(payload, dict):
        for key in ("error", "reason", "detail"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return ""


def with_retries(call, *, attempts: int, delays: tuple[float, ...], what: str):
    """Retry a service call through transport failures; ``ClaimRefused`` is final."""
    last: Exception | None = None
    for index in range(attempts):
        try:
            return call()
        except ServiceError as error:
            last = error
            log(f"{what}: {error} (attempt {index + 1}/{attempts})")
            if index + 1 < attempts:
                time.sleep(delays[min(index, len(delays) - 1)] if delays else 0)
    assert last is not None
    raise last


# ----------------------------------------------------------------------------------------------
# Claim lease


class ClaimLease(threading.Thread):
    """Renews the claim at ``renew_after`` until stopped; sets ``cancel`` when the claim is lost.

    A transport failure does not lose the claim: the renewal is retried every ``retry_interval``
    seconds while the claim is unexpired, so the agent keeps running through a coordinator or
    network outage. The claim is lost when the service refuses the renewal or ``expires_at`` passes
    without a successful one; the agent is then stopped and its result discarded.
    """

    def __init__(
        self,
        client: TaskClient,
        task: Task,
        cancel: threading.Event | None = None,
        *,
        retry_interval: float = 15.0,
        clock=time.time,
    ):
        super().__init__(name=f"claim-lease-{task.task_id}", daemon=True)
        self.client = client
        self.task = task
        self.cancel = cancel if cancel is not None else threading.Event()
        self.lost = threading.Event()
        self.stopped = threading.Event()
        # Set while a result or release is in flight: the service frees the claim when it accepts
        # either, so a renewal refused during that window is not a loss; the submission's own
        # response decides, and the heartbeat simply ends.
        self.submitting = threading.Event()
        self.retry_interval = retry_interval
        self.clock = clock
        self.expires_at = task.expires_at
        self.renewals = 0
        self.errors = 0
        self.lost_reason: str | None = None

    def run(self) -> None:
        interval = max(0.05, float(self.task.renew_after))
        while not self.stopped.wait(interval):
            try:
                self.expires_at = self.client.renew(self.task)
                self.task.expires_at = self.expires_at
                self.renewals += 1
                interval = max(0.05, float(self.task.renew_after))
            except ClaimRefused as error:
                if self.submitting.is_set():
                    log(f"renewal refused while a submission is in flight; the submission decides ({error})")
                    return
                self._lose(str(error))
                return
            except ServiceError as error:
                self.errors += 1
                log(f"renew failed, keeping the agent running until the claim expires: {error}")
                interval = min(self.retry_interval, max(0.05, self.expires_at - self.clock()))
            if self.clock() >= self.expires_at:
                self._lose("claim expired without a successful renewal")
                return

    def _lose(self, reason: str) -> None:
        self.lost_reason = reason
        self.lost.set()
        self.cancel.set()
        log(f"claim lost: {reason}")

    def stop(self) -> None:
        self.stopped.set()


# ----------------------------------------------------------------------------------------------
# Authorization and identity


def token_source() -> str:
    """Where ``worker_token`` reads the token from (named, never the value): ``TAUCETI_QUEUE_TOKEN_FILE``
    (a mode 0600 file, the service unit's choice), the environment, else gh's store."""
    path = os.environ.get("TAUCETI_QUEUE_TOKEN_FILE")
    if path:
        return f"file {path}"
    for key in ("TAUCETI_QUEUE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(key):
            return f"${key}"
    return "gh auth token"


def worker_token() -> str:
    """The GitHub token of the worker's account: a token file, the usual environment, else gh's store."""
    from tauceti_worker.config import Die

    path = os.environ.get("TAUCETI_QUEUE_TOKEN_FILE")
    if path:
        try:
            token = Path(path).read_text().strip()
        except OSError as error:
            raise Die(f"cannot read the queue token file {path}: {error.strerror}") from error
        if not token:
            raise Die(f"the queue token file {path} is empty")
        return token
    for key in ("TAUCETI_QUEUE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, check=False)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise Die("no GitHub token for the queue service: set TAUCETI_QUEUE_TOKEN_FILE or log in with gh")
    return token


def verify_admin(repository: str) -> None:
    """The security audit's authorization predicate, applied to the task's repository."""
    from tauceti_worker.config import Die
    from tauceti_worker.security_audit import api

    if api(f"repos/{repository}").get("permissions", {}).get("admin") is not True:
        raise Die(f"the queue repair task requires repository-admin permission on {repository}")


def default_worker_identity() -> str:
    return (
        os.environ.get("TAUCETI_QUEUE_WORKER")
        or f"{os.environ.get('TAUCETI_WORKER_ID', 'default')}@{socket.gethostname()}"
    )


class Stop:
    """The worker's stop latch: set by the signal handlers, waited on by ``serve``.

    Events attached to it (a running task's ``cancel``) are set with it, so a SIGTERM reaches the agent
    through the same path a lost claim does; the task then records itself as interrupted instead of
    lost, keeping its claim and checkpoint for the next start to resume.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._attached: list[threading.Event] = []
        self.reason: str | None = None

    def set(self, reason: str = "stop") -> None:
        with self._lock:
            if not self._event.is_set():
                self.reason = reason
            self._event.set()
            attached = list(self._attached)
        for event in attached:
            event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def attach(self, event: threading.Event) -> None:
        with self._lock:
            self._attached.append(event)
            fire = self._event.is_set()
        if fire:
            event.set()

    def detach(self, event: threading.Event) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._attached.remove(event)


def install_signal_handlers(stop: Stop) -> dict:
    """SIGTERM and SIGINT set ``stop``; returns the previous handlers (main thread only)."""

    def handler(signum: int, _frame) -> None:
        stop.set(signal.Signals(signum).name)

    return {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}


# ----------------------------------------------------------------------------------------------
# Runner configuration and checkpoints


@dataclasses.dataclass
class RunnerConfig:
    service: str
    clone: Path  # the worker's content checkout (``agents.prepare_checkout`` path)
    root: Path = DEFAULT_ROOT  # task state, logs, worktrees and the private artifact pool
    worker: str = dataclasses.field(default_factory=default_worker_identity)
    repository: str = repair.DEFAULT_REPOSITORY
    profile: object = "claude"  # the claude authoring profile the workers use for repair (claude and api)
    # The provider chain, in the order tried (``TAUCETI_QUEUE_PROVIDERS`` / ``--providers``): ``claude``
    # (the Claude subscription), ``codex`` (the Codex subscription), ``api`` (Claude billed to the API
    # with ``api_key``). A subscription is eligible for a task only while the pacer says it is under
    # pace; a provider that fails before doing any work hands the task to the next one.
    providers: tuple[str, ...] = DEFAULT_PROVIDERS
    # The pacing curve the eligibility check applies (``TAUCETI_QUEUE_RATE`` / ``--rate``, the fleet's
    # ``--pace`` spec, ``time%:budget%`` points); None keeps the pacer's default (``60:40``).
    rate: str | None = None
    api_key: str | None = dataclasses.field(default=None, repr=False)  # ANTHROPIC_API_KEY for ``api``
    token: str | None = None
    agent_timeout: float = 3 * 3600
    # The agent's foreground shell limit (``TAUCETI_SHELL_LIMIT_SECONDS``): a repair runs whole-library
    # builds in the foreground, so it gets two hours where the fleet's authoring rounds keep ten minutes.
    shell_limit: float = repair.SHELL_LIMIT_SECONDS
    build_timeout: float = 3 * 3600
    agent_attempts: int = 2  # ordinary agent failures or locally invalid chains
    validation_retries: int = 1  # ``adopted: false`` with a validation reason
    inference_attempts: int = 3
    inference_delays: tuple[float, ...] = (30.0, 120.0)
    service_attempts: int = 5
    service_delays: tuple[float, ...] = (5.0, 15.0, 60.0, 120.0)
    lease_retry_interval: float = 15.0
    keep_worktree: bool = False
    push_remote: str | None = None  # default: https://github.com/<task.repository>
    mathlib_cache: bool = True  # ``lake exe cache get`` before the base cache fetch
    seed_packages: Path | None = None  # share one extracted Mathlib instead (the experiment's method)
    lake: tuple[str, ...] = ("lake",)
    # Independent failing groups (``repair_parallel.plan_groups``) go to this many concurrent agents,
    # each in its own worktree (``TAUCETI_QUEUE_PARALLEL_AGENTS``, default 3); 0 or 1 keeps every
    # repair sequential.
    parallel_agents: int = dataclasses.field(default_factory=repair_parallel.parallel_agents_default)
    poll_seconds: float = 60.0
    admin_recheck_seconds: float = 3600.0
    # Run the first whole-library build in the background and launch the agent at its first error
    # (``repair_stream``; ``TAUCETI_QUEUE_STREAM_REPAIR``, ``--no-stream-repair``). Off: the agent runs it.
    stream_repair: bool = True
    # The service publishes a task's retained refs through its outbox a moment after creating the
    # task, so a ref the remote does not have yet is retried for this long before it is an error.
    fetch_retry_seconds: float = 120.0
    fetch_retry_delay: float = 10.0
    # A release the service could not confirm (unreachable, 5xx) is retried from the checkpoint on
    # every loop pass, waiting these many seconds between attempts (the last value repeats).
    release_backoff: tuple[float, ...] = (5.0, 15.0, 60.0, 120.0, 300.0)
    # After a repair task is released as ``artifacts_unavailable`` the loop waits this long before its
    # next claim, doubling on each consecutive such release up to the second value, so a base with no
    # published cache is not released and re-claimed within seconds.
    artifacts_backoff: tuple[float, float] = (60.0, 600.0)
    # The workers' own cache namespace: read before the public cache, written after a green repair.
    publish: repair_publish.Settings = dataclasses.field(default_factory=repair_publish.Settings.from_env)

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"


class TaskState:
    """The task checkpoint: ``task.json`` in the task's state directory, replaced atomically."""

    def __init__(self, directory: Path, task: Task):
        self.directory = directory
        self.path = directory / "task.json"
        self.session_path = directory / "session.json"
        self.task = task
        self.record = self.read()
        if not self.record:
            self.record = {"schema": SCHEMA, "task": task.to_json(), "history": []}

    def read(self) -> dict:
        from tauceti_worker.runtime_status import read_json

        return read_json(self.path)

    def write(self, state: str, **fields) -> None:
        from tauceti_worker.runtime_status import atomic_json

        self.record.update(fields)
        self.record["state"] = state
        self.record["updated_at"] = time.time()
        self.record["task"] = self.task.to_json()
        self.record.setdefault("history", []).append({"state": state, "at": self.record["updated_at"]})
        atomic_json(self.path, self.record)

    @property
    def state(self) -> str | None:
        return self.record.get("state")

    @property
    def resumable(self) -> bool:
        """A checkpoint a restarted runner continues: mid-agent, or interrupted by a stop."""
        return self.state in ("agent_running", "interrupted")

    @property
    def agent_started(self) -> bool:
        """The agent had been launched when the checkpoint was written (resume its session)."""
        return self.state == "agent_running" or (
            self.state == "interrupted" and self.record.get("phase") == "agent_running"
        )

    def session(self) -> str | None:
        from tauceti_worker.runtime_status import read_json

        value = read_json(self.session_path).get("session")
        return str(value) if value else None

    def session_provider(self) -> str:
        """The chain provider (``claude``, ``codex`` or ``api``) whose session the checkpoint records. A
        checkpoint from before the chain existed names none; its session is the Claude subscription's,
        the only provider there was, unless ``session.json``'s profile says otherwise."""
        from tauceti_worker.runtime_status import read_json

        value = self.record.get("provider")
        if value in PROVIDERS:
            return str(value)
        profile = read_json(self.session_path).get("profile")
        provider = profile.get("provider") if isinstance(profile, dict) else None
        return str(provider) if provider in SUBSCRIPTION_PROVIDERS else "claude"


# ----------------------------------------------------------------------------------------------
# Phases


class SourcesUnavailable(RuntimeError):
    """The task's retained refs or commits could not be fetched within the retry window."""


FETCH_CHUNK = 200  # refspecs per `git fetch`: one round trip each, well under the command-line limit


def retained_refspecs(task: Task) -> list[tuple[str, str, str]]:
    """``(refspec, private ref, remote ref)`` for each retained ref of the task."""
    specs = []
    for ref in task.retained_refs:
        name = ref.removeprefix("refs/heads/").removeprefix("refs/")
        private = f"{RESULT_REF_PREFIX}/{task.task_id}/retained/{name}"
        specs.append((f"+{ref}:{private}", private, ref))
    return specs


def _git_failure(error: BaseException) -> str:
    """git's exit status and stderr from a ``git()`` failure, without the (long) refspec list."""
    text = " ".join(str(error).split())
    _, found, tail = text.partition(" failed (")
    return (f"exit {tail}" if found else text)[:600]


def _fetch_chunk(clone: Path, task_id: str, group: list[tuple[str, str, str]]) -> tuple[list[str], dict[str, str]]:
    """Fetch one chunk of refspecs in one ``git fetch``; returns the private refs fetched and the remote
    refs that are missing, by name, with git's error.

    git stops at the first ref the remote lacks and names only that one, so when the chunk fails the
    remote is asked once (``ls-remote`` of the chunk's refs) which of them exist, the existing ones
    are fetched in one call and the rest are reported missing. When that listing fails too, or the
    trimmed fetch still fails (a transient server error), the chunk is halved and each half retried,
    so one bad ref costs log(n) fetches instead of n.
    """
    try:
        git(clone, "fetch", "--quiet", "origin", *(spec for spec, _, _ in group))
        return [private for _, private, _ in group], {}
    except RuntimeError as error:
        detail = _git_failure(error)
        if len(group) == 1:
            return [], {group[0][2]: detail}
        log(f"task {task_id}: warning: fetch of {len(group)} retained refs failed: {detail}")
    try:
        listed = git(clone, "ls-remote", "origin", *(remote for _, _, remote in group))
        present = {line.split("\t", 1)[1].strip() for line in listed.splitlines() if "\t" in line}
    except RuntimeError as error:
        log(
            f"task {task_id}: warning: ls-remote of the chunk failed ({' '.join(str(error).split())[:300]}); halving it"
        )
    else:
        missing = {remote: "not on the remote" for _, _, remote in group if remote not in present}
        if missing:
            keep = [entry for entry in group if entry[2] not in missing]
            log(
                f"task {task_id}: {len(missing)} of {len(group)} retained refs are not on the remote yet; fetching the other {len(keep)}"
            )
            if not keep:
                return [], missing
            fetched, more = _fetch_chunk(clone, task_id, keep)
            return fetched, {**missing, **more}
        log(f"task {task_id}: the remote has every ref of the failed chunk; halving it")
    half = len(group) // 2
    fetched_a, missing_a = _fetch_chunk(clone, task_id, group[:half])
    fetched_b, missing_b = _fetch_chunk(clone, task_id, group[half:])
    return fetched_a + fetched_b, {**missing_a, **missing_b}


def _fetch_retained_once(
    clone: Path,
    task: Task,
    chunk: int = FETCH_CHUNK,
    done: set[str] | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Fetch the retained refs a chunk of refspecs per ``git fetch`` (a task can retain a thousand of
    them: per ref, production took 1.3 s each). ``done`` holds the private refs already fetched by an
    earlier attempt of the same call, which are not fetched again; the refs the remote lacks are
    raised together by name."""
    done = done if done is not None else set()
    specs = retained_refspecs(task)
    missing: dict[str, str] = {}
    pending = [entry for entry in specs if entry[1] not in done]
    for start in range(0, len(pending), max(1, chunk)):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("fetch cancelled")
        fetched, absent = _fetch_chunk(clone, task.task_id, pending[start : start + max(1, chunk)])
        done.update(fetched)
        missing.update(absent)
    if missing:
        names = sorted(missing)
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        raise RuntimeError(f"{len(names)} retained ref(s) not fetchable: {shown} ({missing[names[0]]})")
    for sha in (task.base, task.tip, task.validation_base):
        if git(clone, "cat-file", "-t", sha, check=False).strip() != "commit":
            git(clone, "fetch", "--quiet", "origin", sha, check=False)
        if git(clone, "cat-file", "-t", sha, check=False).strip() != "commit":
            raise RuntimeError(f"commit {sha[:12]} is not in the content repository after fetching the retained refs")
    git(clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/input", task.tip)
    return {"refs": [private for _, private, _ in specs], "base": task.base, "tip": task.tip}


def fetch_retained(
    clone: Path,
    task: Task,
    *,
    retry_seconds: float = 0.0,
    retry_delay: float = 10.0,
    cancel: threading.Event | None = None,
    chunk: int = FETCH_CHUNK,
) -> dict:
    """Fetch the task's retained refs into private refs of the clone and check base and tip exist.

    A ref the remote does not have yet (the service publishes them through its outbox a moment after
    creating the task) or a transient fetch failure is retried every ``retry_delay`` seconds for
    ``retry_seconds``, each attempt fetching only the refs the earlier ones did not get; afterwards
    ``SourcesUnavailable`` carries the last error.
    """
    deadline = time.monotonic() + max(0.0, retry_seconds)
    attempt = 0
    done: set[str] = set()
    while True:
        attempt += 1
        try:
            fetched = _fetch_retained_once(clone, task, chunk, done, cancel)
        except RuntimeError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel is not None and cancel.is_set()):
                raise SourcesUnavailable(f"{error} (after {attempt} attempt(s))") from error
            wait = max(0.0, min(retry_delay, remaining))
            log(
                f"task {task.task_id}: sources not fetchable yet ({' '.join(str(error).split())[:300]}); retrying in {wait:.0f}s"
            )
            if cancel is not None:
                if cancel.wait(wait):
                    raise SourcesUnavailable(f"{error} (cancelled after {attempt} attempt(s))") from error
            else:
                time.sleep(wait)
            continue
        fetched["attempts"] = attempt
        return fetched


def task_findings(task: Task) -> dict:
    return repair.findings_from_diagnostics(task.diagnostics)


ARTIFACT_ANCESTORS = 50  # first-parent ancestors of the build revision asked for a published cache mapping


def artifact_revisions(task: Task) -> list[str]:
    """The revisions whose published cache to try, in order. A rebase conflict replays the chain onto
    ``onto`` and builds there, so its artifacts come first and the stale batch base is the fallback;
    a repair builds on its base and falls back to ``onto`` when that differs. ``prepare_artifacts``
    walks the first one's first-parent ancestors (``ARTIFACT_ANCESTORS`` of them) before a fallback
    off that walk, so the artifacts come from the newest revision the cache has."""
    order = [task.onto, task.base] if task.kind == "rebase_conflict" else [task.base, task.onto]
    revisions: list[str] = []
    for revision in order:
        if revision and revision not in revisions:
            revisions.append(revision)
    return revisions


def describe_cache_revision(task: Task, artifacts: dict) -> str:
    """The restored revision and its relation to the task, for the worker log: ``85f3eb5579df (the rebase
    target)``, ``5f78b5307b93 (the batch base)``, ``67aa17161ba1 (3 commits behind the rebase target
    85f3eb5579df)``."""
    revision = artifacts["cache_revision"]
    rebase = task.kind == "rebase_conflict"
    if revision == task.base:
        role = "the batch base" if rebase else "the base"
    elif revision == task.onto:
        role = "the rebase target"
    else:
        target = f"{'the rebase target' if rebase else 'the base'} {artifact_revisions(task)[0][:12]}"
        distance = artifacts.get("cache_distance")
        role = (
            f"{distance} commit{'s' if distance != 1 else ''} behind {target}"
            if distance
            else f"a fallback for {target}"
        )
    return f"{revision[:12]} ({role})"


def artifacts_cold(task: Task, artifacts: dict | None) -> bool:
    """Whether the agent builds without warm artifacts: a rebase conflict whose preparation found no
    cache (a repair without artifacts is released instead, so it never gets this far)."""
    if not artifacts:
        return False
    return bool(artifacts.get("cold")) or (task.kind == "rebase_conflict" and not artifacts.get("available", True))


def prompt_context(
    task: Task,
    feedback: str | None,
    findings: dict | None = None,
    *,
    artifacts: dict | None = None,
    listings: Path | None = None,
    shell_limit: int = repair.SHELL_LIMIT_SECONDS,
) -> PromptContext:
    """``artifacts`` is the ``prepare_artifacts`` record (the prompt says when the build is cold);
    ``listings`` is where the member listings and the check script go (``repair.listings_path``);
    ``shell_limit`` is the agent's foreground shell limit in seconds, as ``run_repair_agent`` sets it."""
    findings = findings if findings is not None else task_findings(task)
    conflict = dict(findings.get("conflict") or {}) or None
    if conflict is not None:
        conflict.setdefault("onto", task.onto)
        conflict.setdefault("previous_base", task.base)
    cold = artifacts_cold(task, artifacts)
    return PromptContext(
        repository=task.repository,
        base=task.validation_base,
        kind=task.kind,
        conflict=conflict,
        feedback=feedback,
        instruction=task.instruction or repair.AGENT_INSTRUCTION,
        members=[
            {
                "source_pr": m.source_pr,
                "accepted_head": m.accepted_head,
                "provenance": m.provenance,
                "title": m.title,
                # The service's flag for a PR that landed on ``main`` outside the queue, once the member
                # record carries it; without it the prompt derives the list from the target's history.
                "landed_externally": getattr(m, "landed_externally", None),
            }
            for m in task.members
        ],
        artifacts_cold=cold,
        artifacts_reason=(artifacts or {}).get("unavailable_reason") if cold else None,
        listings_path=listings,
        shell_limit=shell_limit,
    )


def format_report(report: dict) -> str:
    lines = []
    for problem in report.get("problems") or []:
        lines.append(f"- {problem}")
    if report.get("unexplained"):
        lines.append("- content changed without an appended explanation: " + ", ".join(report["unexplained"]))
    if report.get("dirty"):
        lines.append("- the working tree was left with uncommitted changes")
    if report.get("unchanged"):
        lines.append("- HEAD is still the chain the task started from; nothing was repaired")
    return "\n".join(lines) or json.dumps(report, indent=1, default=str)[:4000]


def format_service_validation(response: dict) -> str:
    """The Adoption record's ``violations`` (chain tool findings) as prompt text."""
    violations = response.get("violations")
    if isinstance(violations, list) and violations:
        return "\n".join(
            f"- {v.get('code', 'violation')}: {v.get('detail', '')}" if isinstance(v, dict) else f"- {v}"
            for v in violations
        )
    validation = response.get("validation")
    if isinstance(validation, str):
        return validation
    if isinstance(validation, dict):
        return json.dumps(validation, indent=1, default=str)[:4000]
    return f"- {response.get('reason', 'the service did not adopt the chain')}"


def rejected_members(response: dict) -> list[str]:
    """The members an ``explanation-required`` rejection names."""
    members = []
    for violation in response.get("violations") or []:
        if isinstance(violation, dict) and violation.get("source_pr"):
            members.append(str(violation["source_pr"]))
    return members


def read_telemetry(logdir: Path, provider: str) -> dict:
    from tauceti_worker.runtime_status import read_json

    return read_json(logdir / f"agent-{provider}-telemetry.json")


# ----------------------------------------------------------------------------------------------
# Providers


def parse_providers(spec: str | None) -> tuple[str, ...]:
    """The provider chain from a comma list (``TAUCETI_QUEUE_PROVIDERS`` / ``--providers``): names from
    ``PROVIDERS`` in the order given, each at most once. An empty spec is the default chain."""
    names = [token.strip().lower() for token in (spec or "").split(",") if token.strip()]
    if not names:
        if (spec or "").strip():
            raise ValueError(f"no providers in {spec!r} (want a comma list of {', '.join(PROVIDERS)})")
        return DEFAULT_PROVIDERS
    for name in names:
        if name not in PROVIDERS:
            raise ValueError(f"unknown provider {name!r} (one of {', '.join(PROVIDERS)})")
    if len(set(names)) != len(names):
        raise ValueError(f"a provider is repeated in {spec!r}")
    return tuple(names)


def provider_profile(cfg: RunnerConfig, provider: str):
    """The authoring profile a chain provider runs: the repository's Claude profile for ``claude`` and
    ``api`` (``cfg.profile`` when the caller pinned one), the repository's Codex profile for ``codex``."""
    if provider == "codex":
        return repair.codex_profile()
    return repair.claude_profile() if cfg.profile == "claude" else cfg.profile


@dataclasses.dataclass(frozen=True)
class _QuotaHome:
    """What ``tauceti_worker.quota.Quota`` reads of a worker configuration: whose credentials (the login
    home, through ``$CLAUDE_CONFIG_DIR`` / ``$CODEX_HOME``) and where to cache usage responses."""

    home: Path
    quota_cache: Path


def _window_summary(windows: list) -> str:
    parts = []
    for window in windows:
        used = "?" if window.used is None else f"{window.used:.0f}%"
        budget = "" if window.budget is None else f" of {window.budget:.0f}% budget"
        parts.append(f"{window.name} {used} used{budget}")
    return "; ".join(parts) or "no windows reported"


def subscription_status(cfg: RunnerConfig, provider: str) -> tuple[bool, str]:
    """Whether a subscription provider may start a task now, and why: the fleet pacer's verdict for the
    credential on this host (``Quota.claude`` / ``Quota.codex``: the usage endpoint's session and weekly
    windows against the pacing curve), under ``cfg.rate`` when the operator set one. The read never
    spends a request and never rotates a token. An unreadable or missing credential, a telemetry failure
    and a malformed usage response are all "not eligible", with the pacer's reason."""
    from tauceti_worker.quota import Quota, _unavail_reason
    from tauceti_worker.repair_recovery import environment

    home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    cache = cfg.root / "quota-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with environment(TAUCETI_PACE=cfg.rate):
            quota = Quota(_QuotaHome(home, cache))
            verdict = quota.codex() if provider == "codex" else quota.claude()
    except Exception as error:  # the pacer must never take the task down with it
        return False, f"usage unmeasurable: {type(error).__name__}: {error}"
    if verdict.available:
        return True, f"under pace ({_window_summary(verdict.windows)})"
    if verdict.error:
        return False, str(verdict.error)
    _soft, reason = _unavail_reason(verdict)
    return False, reason


def select_providers(cfg: RunnerConfig, *, pinned: str | None = None) -> tuple[list[str], list[dict]]:
    """The providers of ``cfg.providers`` that may run a task now, in chain order, and the records of
    the ones skipped (``{provider, status: "skipped", reason, seconds: 0}``): a subscription whose
    rate-limit status is not under the pace the worker's ``--pace`` flag uses, or cannot be measured,
    and the ``api`` provider without a key. ``pinned`` is the provider of a session being resumed: it
    goes first and is not re-checked, since it continues work the pacer already admitted."""
    eligible: list[str] = [pinned] if pinned in PROVIDERS else []
    skipped: list[dict] = []
    for provider in cfg.providers:
        if provider == pinned:
            continue
        if provider in SUBSCRIPTION_PROVIDERS:
            ok, reason = subscription_status(cfg, provider)
        elif provider == "api":
            ok, reason = bool(cfg.api_key), "ANTHROPIC_API_KEY set" if cfg.api_key else "no ANTHROPIC_API_KEY"
        else:
            ok, reason = False, f"unknown provider {provider!r}"
        if ok:
            eligible.append(provider)
            continue
        log(f"provider {provider} skipped: {reason}")
        skipped.append({"provider": provider, "status": "skipped", "reason": reason, "seconds": 0.0})
    return eligible, skipped


# Codex reports a refused request as a structured ``error`` / ``turn.failed`` event whose message names
# the condition; these are its usage-limit, rate-limit and authentication conditions.
_CODEX_REFUSAL = re.compile(
    r"usage_limit|rate_limit|quota_exceeded|credits_depleted|usage limit|rate limit|too many requests"
    r"|\b(?:401|403|429)\b|unauthori[sz]ed|not logged in|sign in again|access token could not be refreshed"
    r"|invalid (?:api key|token|authentication)",
    re.I,
)
_CLAUDE_REFUSAL = re.compile(r"API Error:\s*(401|403|429|5\d\d)\b|hit your .*limit|usage limit reached", re.I)
# The agent runs under the worker's parent-bound guardian, so an executable that is not on PATH is the
# guardian's traceback and exit 1 rather than an OSError from Popen (which is ``AgentLaunchUnavailable``).
_NO_EXECUTABLE = re.compile(
    r"(?:FileNotFoundError|PermissionError|OSError):.*(?:No such file or directory|Permission denied)"
)
LAUNCH_FAILURE = "could not start"


def prework_failure(provider: str, rc: int, run: dict | None, infra_reason: str | None) -> str | None:
    """Why a provider refused the task before any work, or None when the agent did work (its exit, good
    or bad, is then the task's). For Claude: a ``rate_limit_event`` status other than ``allowed`` /
    ``allowed_warning``, or an API error on the first turn; for Codex: a usage-limit, rate-limit or
    authentication error event; for either: an infrastructure failure the fleet classifier names."""
    if rc == 0 or run is None or run.get("saw_work"):
        return None
    for line in run.get("tail") or []:
        if _NO_EXECUTABLE.search(str(line)):
            return f"{LAUNCH_FAILURE}: {str(line).strip()[:200]}"
    status = run.get("rate_limit_status")
    if provider != "codex" and status not in (None, "allowed", "allowed_warning"):
        return f"rate limit status {status}"
    failure = str(run.get("terminal_failure") or "")
    refusal = _CODEX_REFUSAL if provider == "codex" else _CLAUDE_REFUSAL
    if failure and refusal.search(failure):
        return f"refused before any work: {failure.strip().splitlines()[0][:200]}"
    if infra_reason:
        return infra_reason
    if failure:
        return f"failed before any work: {failure.strip().splitlines()[0][:200]}"
    return f"exited {rc} before any work"


def run_repair_agent(
    cfg: RunnerConfig,
    task: Task,
    worktree: Path,
    prompt: str,
    logdir: Path,
    cancel: threading.Event,
    state: TaskState,
    env_lake: dict,
    *,
    resume: str | None = None,
    resume_provider: str | None = None,
    label: str = "repair",
    preselected: list[str] | None = None,
) -> dict:
    """One agent run through the worker's host path, over the task's provider chain, with bounded
    retries on inference unavailability.

    ``preselected`` is an eligible chain the caller already chose (the parallel path chooses once for
    every group agent, in the worker, and hands it to each group's child process); without it the
    chain is ``select_providers`` here.

    The chain is ``select_providers``: each eligible provider in order, a resumed session's provider
    first. A provider that fails to launch, or exits before doing any work with a rate-limit,
    authentication or infrastructure error (``prework_failure``), hands the task to the next provider
    at once; every provider tried or skipped is recorded in ``provider_attempts`` (``provider``,
    ``status``, ``reason``, ``seconds``). When the whole chain fails before any work the pass is retried
    after ``inference_delays`` up to ``inference_attempts`` times.

    Returns ``{"status": "ok"}`` on a clean exit, ``"cancelled"`` when the claim was lost, ``"failed"``
    for the agent's own failure, ``"inference_unavailable"`` after the bounded retries,
    ``"agent_launch_failed"`` when no provider's process could be created at all (a missing executable,
    an argv the kernel rejects), and ``"artifacts_unavailable"`` when the worker's cache preparation
    refused to start inference. ``provider`` names the provider whose agent ran last.
    """
    from tauceti_worker.agents import (
        AgentLaunchUnavailable,
        BuildCacheUnavailable,
        run_agent_host,
        take_last_agent_infra_failure,
        take_last_agent_run,
    )
    from tauceti_worker.config import NoProgress
    from tauceti_worker.repair_recovery import environment

    if resume and resume_provider not in PROVIDERS:
        log("the recorded session names no provider; starting afresh")
        resume = None
    if preselected:
        eligible, provider_attempts = list(preselected), []
    else:
        eligible, provider_attempts = select_providers(cfg, pinned=resume_provider if resume else None)
    sessions: dict[str, str | None] = {resume_provider: resume} if resume else {}
    attempts: list[dict] = []
    reason = None
    provider = None
    prompt_path = logdir / f"prompt-{label}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)
    if not eligible:
        reason = "no eligible provider: " + "; ".join(f"{s['provider']} {s['reason']}" for s in provider_attempts)
        return {
            "status": "inference_unavailable",
            "reason": reason,
            "attempts": attempts,
            "provider_attempts": provider_attempts,
        }

    def result(status: str, **fields) -> dict:
        return {
            "status": status,
            "attempts": attempts,
            "provider": provider,
            "provider_attempts": provider_attempts,
            **fields,
        }

    for index in range(1, cfg.inference_attempts + 1):
        launch_failures = 0
        for provider in eligible:
            if cancel.is_set():
                return result("cancelled")
            profile = provider_profile(cfg, provider)
            telemetry_provider = getattr(profile, "provider", "claude")
            changes = repair.isolation_env(cfg.root, env_lake, provider, api_key=cfg.api_key)
            changes["TAUCETI_ROUND_DEADLINE"] = str(time.time() + cfg.agent_timeout)
            changes["TAUCETI_SHELL_LIMIT_SECONDS"] = str(int(cfg.shell_limit))
            started = time.monotonic()
            session_resume = sessions.get(provider)
            state.write(
                "agent_running",
                agent_label=label,
                agent_attempt=index,
                provider=provider,
                prompt=str(prompt_path),
                resume=session_resume,
            )
            # The worker's cap on one attempt; the plan gives the task itself no time budget.
            expired = threading.Event()
            timer = threading.Timer(cfg.agent_timeout, _expire_attempt, args=(expired, cancel))
            timer.daemon = True
            timer.start()
            run = None
            prework = None
            try:
                with environment(**changes):
                    rc = run_agent_host(
                        worktree,
                        prompt,
                        profile,
                        logdir,
                        cancel_event=cancel,
                        resume=session_resume,
                        session_path=state.session_path,
                        session_actor="queue-repair-task",
                        session_scope=task.task_id,
                    )
            except AgentLaunchUnavailable as error:
                # Its own outcome, not the cache's: the artifacts were ready and the process never existed,
                # so neither the artifacts backoff nor a retry with the same argv is the right response.
                rc, reason, prework = None, str(error), f"{LAUNCH_FAILURE}: {error}"
            except BuildCacheUnavailable as error:
                return result("artifacts_unavailable", reason=str(error))
            except NoProgress as error:
                if cancel.is_set() and not expired.is_set():
                    return result("cancelled")
                rc, reason = 75, f"agent could not start: {error}"
                prework = reason
            else:
                reason = take_last_agent_infra_failure()
                run = take_last_agent_run()
                prework = prework_failure(provider, rc, run, reason)
            finally:
                timer.cancel()
            seconds = round(time.monotonic() - started, 1)
            if expired.is_set():
                cancel.clear()  # the lease is still live; only this attempt is over
            if prework is not None and not expired.is_set():
                if prework.startswith(LAUNCH_FAILURE):
                    launch_failures += 1
                    reason = prework
                log(f"provider {provider} failed before any work ({prework}); trying the next provider")
                provider_attempts.append(
                    {"provider": provider, "status": "failed", "reason": prework, "seconds": seconds}
                )
                if rc is not None:
                    attempts.append(
                        {
                            "provider": provider,
                            "rc": rc,
                            "seconds": seconds,
                            "session": None,
                            "infrastructure_failure": reason,
                            "telemetry": read_telemetry(logdir, telemetry_provider),
                        }
                    )
                continue
            provider_attempts.append({"provider": provider, "status": "ran", "reason": None, "seconds": seconds})
            session = state.session()
            sessions[provider] = session
            record = {
                "provider": provider,
                "rc": rc,
                "seconds": seconds,
                "session": session,
                "infrastructure_failure": reason,
                "telemetry": read_telemetry(logdir, telemetry_provider),
            }
            usage = (record["telemetry"] or {}).get("usage") or {}
            record["estimated_cost_usd"] = repair.price_usage(
                telemetry_provider, getattr(profile, "model", None), usage
            )
            attempts.append(record)
            if expired.is_set():
                record["timed_out"] = True
                return result("failed", session=session, rc=rc, timed_out=True)
            if cancel.is_set():
                return result("cancelled", session=session)
            if rc == 0:
                return result("ok", session=session)
            if not reason:
                return result("failed", session=session, rc=rc)
            # The agent worked and then the provider failed under it: continue its session with the same
            # provider on the next pass, ahead of the rest of the chain.
            log(f"inference unavailable ({reason}); attempt {index}/{cfg.inference_attempts}")
            eligible = [provider, *(p for p in eligible if p != provider)]
            break
        else:
            if launch_failures == len(eligible):
                return result("agent_launch_failed", reason=reason)
            log(f"every provider failed before any work; attempt {index}/{cfg.inference_attempts}")
        if index < cfg.inference_attempts:
            delay = cfg.inference_delays[min(index - 1, len(cfg.inference_delays) - 1)] if cfg.inference_delays else 0
            if cancel.wait(delay):
                return result("cancelled")
    return result("inference_unavailable", reason=reason)


def _expire_attempt(expired: threading.Event, cancel: threading.Event) -> None:
    expired.set()
    cancel.set()


def run_parallel_groups(
    cfg: RunnerConfig,
    task: Task,
    worktree: Path,
    chain: list,
    findings: dict,
    ownership,
    logdir: Path,
    cancel: threading.Event,
    state: TaskState,
    env_lake: dict,
    stream: repair_stream.StreamingBuild | None = None,
) -> tuple[dict, dict | None]:
    """The parallel path of a repair (``repair_parallel``): split the findings into independent groups
    and, with two or more and an eligible provider, run one agent per group in its own worktree and
    child process, fold their fixups into the chain and run the verification build. With a streaming
    build (``stream``) the path first waits for it to finish and takes the groups from its complete
    error list, ``errors.jsonl``, merged with the task's diagnostics. Returns the record for the outcome
    and the findings the sequential path still has to repair (None when nothing changes for it)."""
    record: dict = {"source": "diagnostics"}
    if stream is not None:
        waited = time.monotonic()
        while not stream.finished.wait(0.5):
            if cancel.is_set():
                return {"status": "cancelled", "detail": "cancelled while the streaming build finished"}, None
        record["stream_wait_seconds"] = round(time.monotonic() - waited, 1)
    errors = repair_parallel.read_errors(repair.listings_path(worktree).parent)
    if errors is not None:
        findings = repair_parallel.merge_errors(findings, errors)
        record.update(source=repair_stream.ERRORS_FILE, stream_errors=len(errors))
    groups, reason = repair_parallel.plan_groups(cfg.clone, chain, findings, worktree)
    if len(groups) < 2:
        log(f"task {task.task_id}: sequential repair ({reason})")
        return {**record, "status": "sequential", "reason": reason}, None
    eligible, skipped = select_providers(cfg)
    if not eligible:
        log(f"task {task.task_id}: sequential repair (no eligible provider for the group agents)")
        return {**record, "status": "sequential", "reason": "no eligible provider", "provider_attempts": skipped}, None
    log(
        f"task {task.task_id}: {len(groups)} independent groups, up to {cfg.parallel_agents} agents: "
        + "; ".join(f"{g.name} PRs {g.members} {g.paths}" for g in groups)
    )
    state.write("agent_running", agent_label="parallel", groups=[g.summary() for g in groups])
    context = prompt_context(
        task, None, findings, artifacts=state.record.get("artifacts"), listings=None, shell_limit=int(cfg.shell_limit)
    )
    context.repair_context = ownership
    record.update(
        repair_parallel.repair_groups(
            cfg, task, worktree, chain, groups, findings, context, logdir, cancel, env_lake, providers=eligible
        )
    )
    record["provider_attempts"] = skipped
    state.write("agent_running", agent_label="parallel", parallel=_parallel_summary(record))
    log(
        f"task {task.task_id}: parallel repair {record['status']} in {record.get('seconds')}s: "
        + ", ".join(f"{g['name']} {g['status']} ({g.get('seconds')}s)" for g in record["groups"])
    )
    return record, record.get("remaining")


def _parallel_summary(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("remaining", "report", "attempts")}


def push_result(cfg: RunnerConfig, task: Task, worktree: Path, tip: str, logdir: Path, expect: str | None) -> dict:
    """Push the proposed tip to the task's ``submission_ref`` through ``git-safe-push``.

    The ref is ``refs/tauceti-queue/submissions/<task_id>/<claim_id>``, private to this claim; the
    controller fetches it when the result is submitted. ``expect`` is the oid this runner last pushed
    there (ref CAS); the first push is create-only and, when the ref already exists (a previous
    incarnation of this runner under the same claim), is retried once against the observed oid.
    """
    remote = cfg.push_remote or f"https://github.com/{task.repository}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TAUCETI_PUSH_", "TAUCETI_CLAIM_", "GIT_CONFIG_"))}
    env.update(
        TAUCETI_PUSH_REF=task.submission_ref,
        TAUCETI_PUSH_EXPECT=expect or "",
        TAUCETI_PUSH_REMOTE=remote,
        TAUCETI_PUSH_OID=tip,
        TAUCETI_PUSH_SCOPE=f"queue-task-{task.task_id}",
    )
    env.pop("TAUCETI_RECOVERY_CONTINUATION", None)
    record = repair.run_logged([str(GIT_SAFE_PUSH)], worktree, logdir / "push.log", env=env, timeout=600)
    if record["rc"] == 78 and not expect:
        observed = git(worktree, "ls-remote", remote, task.submission_ref, check=False).split()
        if observed:
            env["TAUCETI_PUSH_EXPECT"] = observed[0]
            record = repair.run_logged([str(GIT_SAFE_PUSH)], worktree, logdir / "push.log", env=env, timeout=600)
            record["superseded"] = observed[0]
    record["ref"] = task.submission_ref
    record["remote"] = remote
    record["ok"] = record["rc"] == 0
    return record


def release_task(
    cfg: RunnerConfig,
    client: TaskClient,
    task: Task,
    state: TaskState,
    reason: str,
    detail,
    outcome: dict | None = None,
    lease: ClaimLease | None = None,
) -> dict:
    """Give the task back with a reason; without a ``release`` endpoint the claim is left to expire."""
    assert reason in RELEASE_REASONS
    if lease is not None:
        lease.submitting.set()
    error_text = str(detail or "")[:2000]
    outcome = outcome if outcome is not None else {"task_id": task.task_id}
    outcome.update(status="released", reason=reason, detail=detail)
    try:
        outcome["response"] = with_retries(
            lambda: client.release(task, reason, error_text),
            attempts=cfg.service_attempts,
            delays=cfg.service_delays,
            what="release",
        )
    except EndpointMissing:
        outcome["status"] = "release_unsupported"
        log(f"the service has no release endpoint; letting the claim expire ({reason}: {detail})")
    except ClaimRefused as error:
        outcome["status"] = "claim_lost"
        outcome["detail"] = f"{detail}; release refused: {error}"
    except ServiceError as error:
        outcome["status"] = "release_failed"
        outcome["detail"] = f"{detail}; release failed: {error}"
        # The claim is still ours and the service does not know we gave up: the checkpoint keeps the
        # release for ``retry_releases`` to confirm from the loop, with the claim token it already holds.
        pending = {
            "reason": reason,
            "error": error_text,
            "attempts": 1,
            "next_at": time.time() + cfg.release_backoff[0],
        }
        state.write(
            outcome["status"], reason=reason, detail=outcome["detail"], outcome=outcome, pending_release=pending
        )
        log(f"task {task.task_id}: release not confirmed; will retry from the checkpoint ({error})")
        return outcome
    state.write(outcome["status"], reason=reason, detail=outcome["detail"], outcome=outcome, pending_release=None)
    return outcome


def run_task(
    cfg: RunnerConfig,
    task: Task,
    client: TaskClient,
    *,
    resume_state: TaskState | None = None,
    stop: Stop | None = None,
) -> dict:
    """Run one claimed task to its outcome. The claim is renewed throughout and the result discarded
    if it is lost. Returns the outcome record (also written to the task's state directory).

    ``stop`` is the worker's stop latch: when it is set while the agent runs, the agent is stopped and
    the task recorded as ``interrupted`` with its claim and worktree kept, so ``resume_pending`` on
    the next start renews the claim and continues the same session. A stop after validation lets the
    push and submission finish; they are quick and end the task.
    """
    state_dir = cfg.tasks_dir / task.task_id
    logdir = state_dir / "logs"
    state = resume_state or TaskState(state_dir, task)
    resuming = resume_state is not None and resume_state.agent_started
    cancel = threading.Event()
    if stop is not None:
        stop.attach(cancel)
    lease = ClaimLease(client, task, cancel, retry_interval=cfg.lease_retry_interval)
    lease.start()
    worktree = cfg.root / "worktrees" / f"task-{task.task_id}"
    outcome: dict = {"task_id": task.task_id, "status": "running", "worker": cfg.worker}
    streaming = cfg.stream_repair and task.kind == "repair" and not resuming
    stream: repair_stream.StreamingBuild | None = None
    started = time.monotonic()

    def discard(detail: str) -> dict:
        outcome.update(status="claim_lost", detail=detail)
        state.write("claim_lost", detail=detail, outcome=outcome)
        return outcome

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    def cancelled(detail: str) -> dict:
        """The agent was cancelled: by the claim being lost (discard), or by the worker stopping. A
        stop with the agent's session in hand renews the claim and checkpoints the task for the next
        start to resume; before that the claim is released as ``worker-stopped`` so the service
        re-exposes the task at once instead of at the claim's expiry."""
        if not stopping() or lease.lost.is_set():
            return discard(lease.lost_reason or detail)
        phase = state.state
        if phase == "agent_running" or state.session():
            lease.stop()
            try:
                task.expires_at = client.renew(task)
                renewed = f"claim renewed to {time.strftime('%H:%M:%S', time.gmtime(task.expires_at))}"
            except (ClaimRefused, ServiceError) as error:
                renewed = f"claim not renewed ({error})"
            outcome.update(status="interrupted", detail=detail, phase=phase)
            state.write("interrupted", phase=phase, detail=detail, outcome=outcome)
            log(f"task {task.task_id}: interrupted while {phase}; {renewed}; the next start resumes the session")
            return outcome
        log(f"task {task.task_id}: stopped while {phase}, before the agent ran; releasing the claim (worker-stopped)")
        return release_task(
            cfg, client, task, state, "worker-stopped", f"worker stopped while {phase}: {detail}", outcome, lease
        )

    try:
        state.write("claimed", worker=cfg.worker, service=cfg.service, resumed=resuming)
        if task.repository != cfg.repository:
            verify_admin(task.repository)
        env_lake = repair.lake_env(cfg.root)
        repair_publish.configure(env_lake, cfg.publish)
        if not resuming:
            try:
                outcome["sources"] = fetch_retained(
                    cfg.clone,
                    task,
                    retry_seconds=cfg.fetch_retry_seconds,
                    retry_delay=cfg.fetch_retry_delay,
                    cancel=cancel,
                )
            except SourcesUnavailable:
                if stopping() and not lease.lost.is_set():
                    return cancelled("the worker stopped while the sources were fetched")
                raise
            outcome["sources"]["worktree_reused"] = checkout_worktree(cfg.clone, task.tip, worktree)
            if cfg.seed_packages:
                repair.link_shared_packages(worktree, cfg.seed_packages)
            state.write("sources_ready", worktree=str(worktree), sources=outcome["sources"])
            revisions = artifact_revisions(task)
            artifacts = repair.prepare_artifacts(
                worktree,
                revisions[0],
                env_lake,
                logdir,
                cfg.build_timeout,
                repository=task.repository,
                fallbacks=tuple(revisions[1:]),
                ancestors=ARTIFACT_ANCESTORS,
                mathlib_cache=cfg.mathlib_cache and cfg.seed_packages is None,
                lake=cfg.lake,
                preferred=repair_publish.preferred(cfg.publish, task),
                build=not streaming,
            )
            if artifacts["cache_revision"]:
                skipped = "; ".join(artifacts.get("cache_skipped") or ())
                log(
                    f"task {task.task_id}: warm artifacts from {describe_cache_revision(task, artifacts)}"
                    + (f"; skipped: {skipped}" if skipped else "")
                )
            outcome["artifacts"] = {k: v for k, v in artifacts.items() if k != "incremental_build"} | {
                "incremental_build": {
                    k: v for k, v in (artifacts.get("incremental_build") or {}).items() if "load" not in k
                }
            }
            state.write("artifacts_ready" if artifacts["available"] else "artifacts_unavailable", artifacts=artifacts)
            if stopping() and not lease.lost.is_set():
                return cancelled("the worker stopped while the artifacts were prepared")
            if not artifacts["available"]:
                if task.kind != "rebase_conflict":
                    return release_task(
                        cfg,
                        client,
                        task,
                        state,
                        "artifacts_unavailable",
                        artifacts["unavailable_reason"],
                        outcome,
                        lease,
                    )
                # The rebase itself needs no build; its validation build runs cold here or in the
                # service's CI, so the agent is run rather than the task released.
                outcome["artifacts"]["cold"] = True
                log(
                    f"task {task.task_id}: no warm artifacts ({artifacts['unavailable_reason']}); "
                    "running the rebase without them"
                )
        elif not worktree.is_dir():
            return release_task(
                cfg, client, task, state, "agent_failed", "resume: the worktree is gone", outcome, lease
            )
        chain_before = read_chain(cfg.clone, task.base, task.tip)
        ownership = repair_context.compute(cfg.clone, chain_before, task.validation_base, worktree)
        outcome["repair_context"] = ownership.summary()
        if streaming and (outcome.get("artifacts") or {}).get("available"):
            stream = repair_stream.start_build(
                worktree,
                repair.listings_path(worktree).parent,
                env_lake,
                chain=chain_before,
                repository=task.repository,
                lake=cfg.lake,
                timeout=cfg.build_timeout,
                copy_log_to=logdir / "stream-build.log",
            )
            if not stream.wait_for_agent(cancel):
                return cancelled("cancelled while the build streamed")
            outcome["stream_build"] = stream.summary()
        findings = task_findings(task)
        feedback: str | None = None
        pushed: str | None = None
        attempt = 0
        validation_retries = 0
        explanation_resubmits = 0
        resubmit = False
        agent_records: list[dict] = []
        resume_session = state.session() if resuming else None
        resume_provider = state.session_provider() if resuming else None
        skip_agent = False
        while True:
            attempt += 1
            if cancel.is_set() or stopping():
                return cancelled("cancelled before the agent started")
            if attempt == 1 and not resuming and task.kind == "repair" and cfg.parallel_agents > 1:
                # Independent failing groups are repaired by concurrent agents first; what they leave
                # (nothing, a group that failed, a build that still fails) goes to the usual agent below.
                parallel, remaining = run_parallel_groups(
                    cfg, task, worktree, chain_before, findings, ownership, logdir, cancel, state, env_lake, stream
                )
                outcome["parallel"] = {k: v for k, v in parallel.items() if k != "remaining"}
                if parallel["status"] == "cancelled":
                    return cancelled("the parallel agents were cancelled")
                if parallel["status"] in RELEASED_AGENT_STATUSES:
                    return release_task(
                        cfg, client, task, state, parallel["status"], parallel.get("reason"), outcome, lease
                    )
                if parallel["status"] in ("ok", "partial"):
                    agent = {"status": "ok", "parallel": True, "attempts": parallel.get("attempts", [])}
                    agent_records.append(agent)
                    outcome["agent"] = agent_records
                    skip_agent = parallel["status"] == "ok"
                    findings = remaining if remaining is not None else findings
            if resubmit:
                resubmit = False  # the explanation turn already ran; check and submit its result
            elif skip_agent:
                skip_agent = False  # every group folded and the verification build passed; check and submit
            else:
                context = prompt_context(
                    task,
                    feedback,
                    findings,
                    artifacts=outcome.get("artifacts") or state.record.get("artifacts"),
                    listings=repair.listings_path(worktree),
                    shell_limit=int(cfg.shell_limit),
                )
                context.repair_context = ownership
                prompt = repair.compose_prompt(context, findings, chain_before, cfg.clone)
                if stream is not None:
                    prompt = stream.annotate(prompt)
                if resume_session:
                    prompt = (
                        "The worker running this task was interrupted and has resumed your session; the worktree is "
                        "as you left it. Continue the task from where you were, and finish with the summary the "
                        "original instructions ask for.\n\n" + prompt
                    )
                agent = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    prompt,
                    logdir,
                    cancel,
                    state,
                    env_lake,
                    resume=resume_session,
                    resume_provider=resume_provider,
                    label=f"attempt-{attempt}",
                )
                resume_session = None
                agent_records.append(agent)
                outcome["agent"] = agent_records
                if agent["status"] == "cancelled":
                    return cancelled("the agent was cancelled")
                if agent["status"] in RELEASED_AGENT_STATUSES:
                    return release_task(cfg, client, task, state, agent["status"], agent.get("reason"), outcome, lease)
            tip = git(worktree, "rev-parse", "HEAD").strip()
            git(cfg.clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/attempt-{attempt}", tip)
            report = repair.chain_report(
                cfg.clone, task.validation_base, task.tip, tip, task.member_keys, prior_base=task.base
            )
            if report["ok"] and report["unexplained"] and not cancel.is_set():
                follow = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    repair.explanation_prompt(report["unexplained"], chain_before),
                    logdir,
                    cancel,
                    state,
                    env_lake,
                    resume=agent.get("session"),
                    resume_provider=agent.get("provider"),
                    label=f"attempt-{attempt}-explanation",
                )
                agent_records.append(follow)
                if follow["status"] == "cancelled":
                    return cancelled("the explanation turn was cancelled")
                if follow["status"] in RELEASED_AGENT_STATUSES:
                    return release_task(
                        cfg, client, task, state, follow["status"], follow.get("reason"), outcome, lease
                    )
                tip = git(worktree, "rev-parse", "HEAD").strip()
                git(cfg.clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/attempt-{attempt}", tip)
                report = repair.chain_report(
                    cfg.clone, task.validation_base, task.tip, tip, task.member_keys, prior_base=task.base
                )
            report["dirty"] = bool(git(worktree, "status", "--porcelain", "--untracked-files=no").strip())
            report["unchanged"] = tip == task.tip
            report["agent_failed"] = agent["status"] == "failed"
            state.write("validated", attempt=attempt, tip=tip, report=report)
            acceptable = report["ok"] and not report["dirty"] and not report["unchanged"]
            if not acceptable:
                detail = format_report(report)
                if agent.get("timed_out"):
                    detail = f"agent exceeded the worker's {cfg.agent_timeout:.0f}s cap\n{detail}"
                elif agent["status"] == "failed":
                    detail = f"agent exited {agent.get('rc')}\n{detail}"
                if attempt < cfg.agent_attempts:
                    feedback = (
                        "Local validation of the previous attempt's chain:\n"
                        + detail
                        + f"\n\nThe chain the task started from is retained at `{RESULT_REF_PREFIX}/{task.task_id}/input` "
                        f"(tip `{task.tip[:12]}`, on base `{task.validation_base[:12]}`)."
                    )
                    log(f"attempt {attempt} not acceptable; retrying once with the report")
                    continue
                return release_task(cfg, client, task, state, "agent_failed", detail, outcome, lease)
            if lease.lost.is_set():
                return discard(lease.lost_reason or "claim lost before the push")
            if (outcome.get("publish") or {}).get("revision") != tip:
                outcome["publish"] = repair_publish.publish_outputs(
                    worktree,
                    tip,
                    settings=cfg.publish,
                    env=env_lake,
                    logdir=logdir,
                    repository=task.repository,
                    lake=cfg.lake,
                )
            push = push_result(cfg, task, worktree, tip, logdir, pushed)
            outcome["push"] = {k: v for k, v in push.items() if "load" not in k}
            if not push["ok"]:
                return release_task(
                    cfg, client, task, state, "push_failed", f"git-safe-push exited {push['rc']}", outcome, lease
                )
            pushed = tip
            state.write("pushed", tip=tip, pushed_ref=push["ref"])
            if lease.lost.is_set():
                return discard(lease.lost_reason or "claim lost after the push")
            notes = {
                "worker": cfg.worker,
                "attempt": attempt,
                "edited": report.get("edited"),
                "agent_seconds": sum(a.get("seconds", 0) for r in agent_records for a in r.get("attempts", [])),
                "estimated_cost_usd": sum(
                    a.get("estimated_cost_usd") or 0 for r in agent_records for a in r.get("attempts", [])
                ),
            }
            lease.submitting.set()
            try:
                response = with_retries(
                    functools.partial(client.submit, task, tip, push["ref"], notes),
                    attempts=cfg.service_attempts,
                    delays=cfg.service_delays,
                    what="result",
                )
            except ClaimRefused as error:
                return discard(f"result refused: {error}")
            state.write("submitted", attempt=attempt, response=response)
            outcome["response"] = response
            reason = str(response.get("reason") or "")
            if response.get("accepted"):
                lease.stop()
                outcome.update(status="adopted", tip=tip, pushed_ref=push["ref"], version=response.get("version"))
                state.write("adopted", outcome=outcome)
                return outcome
            if reason in CLAIM_CODES:
                return discard(f"result rejected: {reason}")
            lease.submitting.clear()
            if reason == "explanation-required" and explanation_resubmits < cfg.validation_retries:
                # The controller asks the same claim to add the explanation and resubmit.
                explanation_resubmits += 1
                named = rejected_members(response) or report.get("edited") or task.member_keys
                follow = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    repair.explanation_prompt(named, chain_before),
                    logdir,
                    cancel,
                    state,
                    env_lake,
                    resume=agent.get("session"),
                    resume_provider=agent.get("provider"),
                    label=f"attempt-{attempt}-explanation-resubmit",
                )
                agent_records.append(follow)
                if follow["status"] == "cancelled":
                    return cancelled("the explanation turn was cancelled")
                if follow["status"] in RELEASED_AGENT_STATUSES:
                    return release_task(
                        cfg, client, task, state, follow["status"], follow.get("reason"), outcome, lease
                    )
                attempt -= 1  # the resubmission continues this attempt
                feedback = None
                resubmit = True
                continue
            if reason == "invalid-chain" and validation_retries < cfg.validation_retries:
                validation_retries += 1
                feedback = "The queue's validation of the previous attempt's chain:\n" + format_service_validation(
                    response
                )
                log(f"chain not adopted ({reason}); retrying with the validation report")
                continue
            outcome.update(status="not_adopted", tip=tip, pushed_ref=push["ref"], reason=reason)
            state.write("not_adopted", outcome=outcome)
            return outcome
    except Exception as error:  # the state record must say what happened
        phase = state.state
        outcome.update(status="error", error=f"{type(error).__name__}: {error}", phase=phase)
        with contextlib.suppress(Exception):
            state.write("error", error=outcome["error"], phase=phase, outcome=outcome)
        # Give the claim back so the task is re-exposed now rather than at the claim's expiry. A
        # failure before the agent ran is the sources' or artifacts' fault; later it is the attempt's.
        # When the service cannot confirm, the checkpoint keeps the release for ``retry_releases``.
        if not lease.lost.is_set():
            if stopping():
                reason = "worker-stopped"  # the stop interrupted the step (a cancelled fetch, a killed build)
            else:
                reason = "artifacts_unavailable" if phase in PRE_AGENT_PHASES else "agent_failed"
            with contextlib.suppress(Exception):
                release_task(cfg, client, task, state, reason, outcome["error"], dict(outcome), lease)
                outcome["release"] = state.state
            log(f"task {task.task_id}: failed while {phase}; claim {state.state} ({reason})")
        raise
    finally:
        lease.stop()
        if stop is not None:
            stop.detach(cancel)
        if stream is not None:
            stream.stop()
            outcome["stream_build"] = stream.summary()
        outcome["seconds"] = round(time.monotonic() - started, 1)
        outcome["lease"] = {"renewals": lease.renewals, "errors": lease.errors, "lost": lease.lost.is_set()}
        # An interrupted task keeps its worktree: the next start resumes the agent in it.
        if not cfg.keep_worktree and outcome.get("status") not in ("error", "interrupted"):
            with contextlib.suppress(Exception):
                repair.remove_worktree(cfg.clone, worktree)
        with contextlib.suppress(Exception):
            repair.write_record(state_dir / "outcome.json", outcome)
    return outcome


# ----------------------------------------------------------------------------------------------
# Loop


class AdminCheck:
    """Caches the admin verification for ``admin_recheck_seconds`` to spare the shared API quota."""

    def __init__(self, cfg: RunnerConfig):
        self.cfg = cfg
        self.verified_at: float | None = None

    def ensure(self) -> None:
        if self.verified_at is None or time.time() - self.verified_at > self.cfg.admin_recheck_seconds:
            verify_admin(self.cfg.repository)
            self.verified_at = time.time()


def make_client(cfg: RunnerConfig) -> TaskClient:
    return TaskClient(cfg.service, cfg.token or worker_token(), cfg.worker)


def claim_and_run(
    cfg: RunnerConfig, client: TaskClient, admin: AdminCheck | None = None, *, stop: Stop | None = None
) -> dict | None:
    """Verify the account, claim one task and run it. None when nothing was claimable."""
    (admin or AdminCheck(cfg)).ensure()
    task = with_retries(client.claim, attempts=cfg.service_attempts, delays=cfg.service_delays, what="claim")
    if task is None and release_stale_claims(cfg, client):
        task = with_retries(client.claim, attempts=cfg.service_attempts, delays=cfg.service_delays, what="claim")
    if task is None:
        return None
    log(
        f"claimed {task.kind} task {task.task_id} ({len(task.members)} members, expires in {task.expires_at - time.time():.0f}s)"
    )
    outcome = run_task(cfg, task, client, stop=stop)
    log(f"task {task.task_id}: {outcome['status']}" + (f" ({outcome.get('reason')})" if outcome.get("reason") else ""))
    return outcome


def resume_pending(cfg: RunnerConfig, client: TaskClient, *, stop: Stop | None = None) -> list[dict]:
    """Continue tasks a previous runner left mid-agent, or interrupted by a stop, while their claim is
    still live.

    The checkpoint names the task, the claim token and the provider session; the claim is renewed
    first, and if the service refuses the task is recorded as lost. A live claim resumes the same
    model session in the same worktree instead of restarting the attempt; a task interrupted before
    its agent started is run again from its sources.
    """
    outcomes = []
    for directory, record in in_flight_checkpoints(cfg):
        if stop is not None and stop.is_set():
            break
        try:
            task = Task.from_json(record["task"])
        except (ValueError, KeyError) as error:
            log(f"{directory.name}: unreadable checkpoint ({error})")
            continue
        state = TaskState(directory, task)
        try:
            task.expires_at = client.renew(task)
        except ClaimRefused as error:
            state.write("claim_lost", detail=f"resume: {error}")
            log(f"task {task.task_id}: claim no longer ours; not resumed")
            continue
        except ServiceError as error:
            log(f"task {task.task_id}: renew failed during resume ({error}); will retry on the next start")
            continue
        if record.get("state") not in RESUMABLE_STATES:
            # Claimed and stopped before the agent ran (or past the agent, mid-submission): nothing
            # to continue; give the claim back now and claim fresh rather than hold it to expiry.
            log(
                f"task {task.task_id}: claim still ours but stopped while {record.get('state')}; releasing (worker-stopped)"
            )
            outcomes.append(
                release_task(
                    cfg, client, task, state, "worker-stopped", f"worker restarted while {record.get('state')}"
                )
            )
            continue
        log(f"resuming task {task.task_id} (session {state.session() or 'unknown'})")
        outcomes.append(run_task(cfg, task, client, resume_state=state, stop=stop))
    return outcomes


def in_flight_checkpoints(cfg: RunnerConfig) -> list[tuple[Path, dict]]:
    """Checkpoints of this worker whose task is unfinished and whose claim may still be ours."""
    from tauceti_worker.runtime_status import read_json

    found = []
    if not cfg.tasks_dir.is_dir():
        return found
    for directory in sorted(cfg.tasks_dir.iterdir()):
        record = read_json(directory / "task.json")
        if record.get("state") not in IN_FLIGHT_STATES or not isinstance(record.get("task"), dict):
            continue
        if record.get("worker") not in (None, cfg.worker):
            continue
        found.append((directory, record))
    return found


def release_stale_claims(cfg: RunnerConfig, client: TaskClient) -> int:
    """Release every in-flight claim this worker still holds on paper. Called when a claim comes back
    empty: the service refuses a claim for concurrency while our own earlier claim (a stopped run that
    never released it) is live, and we hold its token. Returns how many the service confirmed."""
    released = 0
    for directory, record in in_flight_checkpoints(cfg):
        try:
            task = Task.from_json(record["task"])
        except (ValueError, KeyError):
            continue
        state = TaskState(directory, task)
        try:
            client.release(task, "worker-stopped", f"stale claim from a run stopped while {record.get('state')}")
        except ClaimRefused as error:
            state.write("claim_lost", detail=f"stale claim: {error}")
            continue
        except (EndpointMissing, ServiceError) as error:
            log(f"task {task.task_id}: stale claim could not be released ({error})")
            continue
        state.write(
            "released", reason="worker-stopped", detail=f"stale claim from a run stopped while {record.get('state')}"
        )
        log(f"task {task.task_id}: released our stale claim left by a stopped run ({record.get('state')})")
        released += 1
    return released


# ----------------------------------------------------------------------------------------------
# Serve: the long-running worker


HEARTBEAT_SCHEMA = "tauceti-queue.repair-worker/v1"


def heartbeat_path(cfg: RunnerConfig) -> Path:
    return cfg.root / "worker.json"


def write_heartbeat(cfg: RunnerConfig, summary: dict, state: str, **fields) -> None:
    """``<root>/worker.json``: what the worker is doing, for the installer's check and the operator."""
    from tauceti_worker.runtime_status import atomic_json

    summary.update(fields)
    summary["state"] = state
    summary["updated_at"] = time.time()
    with contextlib.suppress(Exception):
        atomic_json(heartbeat_path(cfg), summary, durable=False)


def provider_attempts_of(outcome: dict) -> list[dict]:
    """Every provider tried or skipped across the task's agent runs, in order."""
    return [dict(entry) for run in outcome.get("agent") or [] for entry in run.get("provider_attempts") or []]


def provider_of(outcome: dict) -> str | None:
    """The provider whose agent ran last for the task, if any did."""
    for run in reversed(outcome.get("agent") or []):
        if run.get("provider"):
            return run["provider"]
    return None


def inference_report_for(outcome: dict) -> tuple[bool, str] | None:
    """What an outcome says about the provider, for ``report_inference``: the agent answered (even
    with a chain the service or the worker refused) is a success that lifts a hold; a release for
    ``inference_unavailable`` is already counted by the service unless the release never reached it."""
    status = outcome.get("status")
    reason = outcome.get("reason")
    if status in ("adopted", "not_adopted") or reason == "agent_failed":
        return True, ""
    if reason == "inference_unavailable" and status in ("release_unsupported", "release_failed"):
        return False, str(outcome.get("detail") or "")[:2000]
    return None


def report_inference(cfg: RunnerConfig, client: TaskClient, outcome: dict) -> None:
    report = inference_report_for(outcome)
    if report is None:
        return
    ok, error = report
    try:
        with_retries(
            lambda: client.report_inference(ok, error),
            attempts=cfg.service_attempts,
            delays=cfg.service_delays,
            what="inference",
        )
    except EndpointMissing:
        log("the service has no inference endpoint; provider outcomes are not reported")
    except (ClaimRefused, ServiceError) as error:
        log(f"inference report failed: {error}")


def pending_release(record: dict) -> dict | None:
    """The release a checkpoint still owes the service, if any.

    ``pending_release`` is written by ``release_task`` when the service could not confirm. A
    checkpoint in ``error`` without one (written before releases were retried) owes a release too:
    its claim token is in the record, and the service answers ``claim-expired`` if it is long gone.
    """
    pending = record.get("pending_release")
    if isinstance(pending, dict) and pending.get("reason") in RELEASE_REASONS:
        return pending
    if record.get("state") == "error" and "pending_release" not in record:
        reason = "artifacts_unavailable" if record.get("phase") in PRE_AGENT_PHASES else "agent_failed"
        return {"reason": reason, "error": str(record.get("error") or "")[:2000], "attempts": 0, "next_at": 0.0}
    return None


def retry_releases(cfg: RunnerConfig, client: TaskClient, *, stop: Stop | None = None) -> list[dict]:
    """Confirm the releases earlier runs could not: for every checkpoint of this worker that still
    holds an unreleased claim, call ``release`` once (backing off between passes) until the service
    confirms it, says the claim is gone, or has no such endpoint. Run before each claim, so a claim
    the service refuses for concurrency because of our own stale claim is given back instead of
    polled around for its whole TTL. Returns the checkpoints whose release was settled this pass."""
    from tauceti_worker.runtime_status import read_json

    settled = []
    if not cfg.tasks_dir.is_dir():
        return settled
    for directory in sorted(cfg.tasks_dir.iterdir()):
        if stop is not None and stop.is_set():
            break
        record = read_json(directory / "task.json")
        pending = pending_release(record)
        if pending is None or not isinstance(record.get("task"), dict):
            continue
        if record.get("worker") not in (None, cfg.worker):
            continue  # another worker's checkpoint in a shared root
        if float(pending.get("next_at") or 0) > time.time():
            continue
        try:
            task = Task.from_json(record["task"])
        except (ValueError, KeyError) as error:
            log(f"{directory.name}: unreadable checkpoint ({error})")
            continue
        state = TaskState(directory, task)
        reason, error = pending["reason"], str(pending.get("error") or "")
        try:
            response = client.release(task, reason, error)
        except EndpointMissing:
            state.write("release_unsupported", pending_release=None, detail=f"{error}; no release endpoint")
            log(f"task {task.task_id}: the service has no release endpoint; letting the claim expire")
        except ClaimRefused as refused:
            state.write("claim_lost", pending_release=None, detail=f"{error}; release refused: {refused}")
            log(f"task {task.task_id}: pending release settled; the claim is no longer ours ({refused})")
        except ServiceError as failure:
            attempts = int(pending.get("attempts") or 0) + 1
            delay = cfg.release_backoff[min(attempts - 1, len(cfg.release_backoff) - 1)] if cfg.release_backoff else 0
            pending = {**pending, "attempts": attempts, "next_at": time.time() + delay, "last_error": str(failure)}
            state.write("release_failed", pending_release=pending, reason=reason)
            log(f"task {task.task_id}: release still unconfirmed ({failure}); attempt {attempts}, next in {delay:.0f}s")
            continue
        else:
            state.write("released", pending_release=None, reason=reason, detail=error, response=response)
            log(f"task {task.task_id}: pending release confirmed ({reason})")
        settled.append({"task_id": task.task_id, "state": state.state, "reason": reason})
    return settled


def serve(
    cfg: RunnerConfig, client: TaskClient, stop: Stop, admin: AdminCheck | None = None, *, resume: bool = True
) -> dict:
    """Claim, run and submit tasks until ``stop`` is set; nothing a task does ends the loop.

    Each pass verifies the account (cached), claims one task and runs it, reporting the provider
    outcome to the service's inference hold; when nothing is claimable the loop sleeps ``poll_seconds``
    (woken by ``stop``). A failure of the task, the service or the authorization check is logged,
    counted and retried on the next pass: ``Restart=always`` in the unit is for crashes of the
    interpreter, not the ordinary way out of a bad task.
    """
    from tauceti_worker.config import Die, NoProgress

    admin = admin or AdminCheck(cfg)
    summary: dict = {
        "schema": HEARTBEAT_SCHEMA,
        "pid": os.getpid(),
        "worker": cfg.worker,
        "service": cfg.service,
        "root": str(cfg.root),
        "started_at": time.time(),
        "tasks": 0,
        "outcomes": {},
        "errors": 0,
        "last_error": None,
        "last_outcome": None,
        "providers": {},  # per provider: how often it ran, was skipped, or failed before any work
    }

    def record(outcome: dict) -> None:
        summary["tasks"] += 1
        summary["outcomes"][outcome["status"]] = summary["outcomes"].get(outcome["status"], 0) + 1
        provider_attempts = provider_attempts_of(outcome)
        for entry in provider_attempts:
            counts = summary["providers"].setdefault(entry["provider"], {"ran": 0, "skipped": 0, "failed": 0})
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        summary["last_outcome"] = {
            "task_id": outcome.get("task_id"),
            "status": outcome.get("status"),
            "reason": outcome.get("reason"),
            "provider": provider_of(outcome),
            "provider_attempts": provider_attempts,
            "at": time.time(),
        }
        report_inference(cfg, client, outcome)

    def failed(what: str, error: BaseException, *, trace: bool = False) -> None:
        summary["errors"] += 1
        summary["last_error"] = {"what": what, "error": f"{type(error).__name__}: {error}"[:2000], "at": time.time()}
        detail = f"{type(error).__name__}: {error}"
        if trace:
            detail += "\n" + "".join(traceback.format_exception(error)).rstrip()
        log(f"{what}: {detail}; next attempt in {cfg.poll_seconds:.0f}s")

    artifacts_wait = 0.0

    def artifacts_backoff(outcome: dict) -> None:
        """Wait before the next claim when a task was released for want of artifacts; the service
        re-exposes the same task at once, and nothing publishes its cache within seconds."""
        nonlocal artifacts_wait
        if outcome.get("reason") != "artifacts_unavailable":
            artifacts_wait = 0.0
            return
        first, cap = cfg.artifacts_backoff
        artifacts_wait = min(artifacts_wait * 2, cap) if artifacts_wait else min(first, cap)
        summary["artifacts_backoff"] = artifacts_wait
        log(
            f"task {outcome.get('task_id')}: artifacts unavailable ({outcome.get('detail')}); "
            f"waiting {artifacts_wait:.0f}s before the next claim"
        )
        write_heartbeat(cfg, summary, "idle")
        stop.wait(artifacts_wait)

    write_heartbeat(cfg, summary, "starting")
    log(f"serving {cfg.service} as {cfg.worker} (poll {cfg.poll_seconds:.0f}s, root {cfg.root})")
    if resume:
        try:
            for outcome in resume_pending(cfg, client, stop=stop):
                record(outcome)
        except Exception as error:
            failed("resume", error, trace=True)
    while not stop.is_set():
        outcome = None
        try:
            settled = retry_releases(cfg, client, stop=stop)
            if settled:
                summary["releases_settled"] = summary.get("releases_settled", 0) + len(settled)
            write_heartbeat(cfg, summary, "claiming")
            outcome = claim_and_run(cfg, client, admin, stop=stop)
        except (Die, NoProgress) as error:
            failed("authorization", error)  # not admin, no token, or GitHub unreachable: not fatal
        except ClaimRefused as error:
            failed("claim", error)  # the service does not accept this account
        except ServiceError as error:
            failed("service", error)
        except Exception as error:
            failed("task", error, trace=True)
        if outcome is not None:
            record(outcome)
            artifacts_backoff(outcome)
            write_heartbeat(cfg, summary, "running" if not stop.is_set() else "stopping")
            continue
        write_heartbeat(cfg, summary, "idle")
        stop.wait(cfg.poll_seconds)
    write_heartbeat(cfg, summary, "stopped", stopped_at=time.time(), stop_reason=stop.reason)
    return summary


# ----------------------------------------------------------------------------------------------
# Preflight


def preflight(cfg: RunnerConfig, *, min_free_gib: float = 20.0) -> list[dict]:
    """What the host must have before serving, each ``{check, ok, detail}``; secrets are named, never
    printed. Fails fast where the loop would otherwise retry forever."""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    tools = ["git", "gh", "lake"]
    if {"claude", "api"} & set(cfg.providers):
        tools.append("claude")
    if "codex" in cfg.providers:
        tools.append("codex")
    for tool in tools:
        where = shutil.which(tool)
        check(f"tool {tool}", where is not None, where or "not on PATH")
    try:
        from tauceti_worker.quota import parse_pace_curve

        curve = parse_pace_curve(cfg.rate)
        check("providers", bool(cfg.providers), ", ".join(cfg.providers) + f"; rate {cfg.rate or 'default'} ({curve})")
    except ValueError as error:
        check("providers", False, f"rate {cfg.rate!r}: {error}")
    try:
        cfg.token or worker_token()
        check("github token", True, f"from {token_source()}")
    except Exception as error:
        check("github token", False, str(error))
    try:
        from tauceti_worker.security_audit import api

        login = str(api("user").get("login") or "")
        admin = api(f"repos/{cfg.repository}").get("permissions", {}).get("admin") is True
        check("repository admin", admin, f"{login or 'unknown login'} on {cfg.repository}")
    except Exception as error:
        check("repository admin", False, str(error))
    try:
        request = urllib.request.Request(
            f"{cfg.service.rstrip('/')}/healthz",
            headers={"Accept": "application/json", "User-Agent": f"tauceti-queue-repair-task/{cfg.worker}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read() or b"{}")
        revision = payload.get("revision") or (payload.get("service") or {}).get("revision") or "?"
        check("service", True, f"{cfg.service} answers (revision {str(revision)[:12]})")
    except Exception as error:
        check("service", False, f"{cfg.service}: {error}")
    try:
        remote = git(cfg.clone, "remote", "get-url", "origin").strip()
        check("content clone", bool(remote), f"{cfg.clone} (origin {remote})")
    except Exception as error:
        check("content clone", False, f"{cfg.clone}: {error}")
    home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    if "claude" in cfg.providers:
        try:
            from tauceti_worker.quota import claude_dir

            credentials = claude_dir(home) / ".credentials.json"
            check("claude credentials", credentials.is_file(), str(credentials))
        except Exception as error:
            check("claude credentials", False, str(error))
    if "codex" in cfg.providers:
        try:
            from tauceti_worker.quota import codex_dir

            credentials = codex_dir(home) / "auth.json"
            check("codex credentials", credentials.is_file(), str(credentials))
        except Exception as error:
            check("codex credentials", False, str(error))
    if "api" in cfg.providers:
        check("anthropic api key", bool(cfg.api_key), "ANTHROPIC_API_KEY " + ("set" if cfg.api_key else "not set"))
    try:
        check("build sandbox", *repair_sandbox.check(cfg.root))
    except OSError as error:
        check("build sandbox", False, str(error))
    try:
        cfg.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(cfg.root).free / 2**30
        check("task root", free >= min_free_gib, f"{cfg.root}: {free:.0f} GiB free (need {min_free_gib:.0f})")
    except OSError as error:
        check("task root", False, f"{cfg.root}: {error}")
    return checks


def stream_repair_enabled(no_stream_repair: bool = False, environ: dict | None = None) -> bool:
    """``--no-stream-repair`` or ``TAUCETI_QUEUE_STREAM_REPAIR`` set to 0/false/no/off turn streaming off."""
    value = (os.environ if environ is None else environ).get("TAUCETI_QUEUE_STREAM_REPAIR", "1").strip().lower()
    return not no_stream_repair and value not in ("0", "false", "no", "off")


def _env_path(key: str) -> Path | None:
    value = os.environ.get(key)
    return Path(value).expanduser() if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_task",
        description=__doc__.split("\n\n")[0]
        + " Defaults come from the environment the service unit's EnvironmentFile sets: TAUCETI_QUEUE_SERVICE,"
        " TAUCETI_QUEUE_WORKER, TAUCETI_QUEUE_CLONE, TAUCETI_QUEUE_TASK_ROOT, TAUCETI_QUEUE_POLL_SECONDS,"
        " TAUCETI_QUEUE_SEED_PACKAGES, TAUCETI_QUEUE_STREAM_REPAIR, TAUCETI_QUEUE_TOKEN_FILE (or TAUCETI_QUEUE_TOKEN),"
        " TAUCETI_QUEUE_PROVIDERS,"
        " TAUCETI_QUEUE_RATE and ANTHROPIC_API_KEY (the api provider's key).",
    )
    parser.add_argument(
        "--providers",
        default=os.environ.get("TAUCETI_QUEUE_PROVIDERS") or ",".join(DEFAULT_PROVIDERS),
        metavar="NAME[,NAME...]",
        help="the provider chain, tried in order for each task: claude (the Claude subscription), codex "
        "(the Codex subscription), api (Claude through ANTHROPIC_API_KEY); a subscription over its pace is "
        f"skipped ($TAUCETI_QUEUE_PROVIDERS; default {','.join(DEFAULT_PROVIDERS)})",
    )
    parser.add_argument(
        "--rate",
        default=os.environ.get("TAUCETI_QUEUE_RATE") or None,
        metavar="T:B[,T:B...]",
        help="the pacing curve a subscription must be under to take a task, as the fleet's --pace spec "
        "(time%%:budget%% points, e.g. 60:40) ($TAUCETI_QUEUE_RATE; default: the pacer's default curve)",
    )
    parser.add_argument(
        "--service",
        default=os.environ.get("TAUCETI_QUEUE_SERVICE"),
        help="queue service base URL ($TAUCETI_QUEUE_SERVICE)",
    )
    parser.add_argument("--once", action="store_true", help="claim at most one task, then exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the host checks (tools, token, admin, service, clone, credentials, build sandbox, disk) and exit",
    )
    parser.add_argument(
        "--worker", default=default_worker_identity(), help="worker identity string ($TAUCETI_QUEUE_WORKER)"
    )
    parser.add_argument(
        "--clone",
        type=Path,
        default=_env_path("TAUCETI_QUEUE_CLONE"),
        help="content repository checkout ($TAUCETI_QUEUE_CLONE; default: the worker's checkout)",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="task state, logs, worktrees and artifact pool")
    parser.add_argument("--repository", default=repair.DEFAULT_REPOSITORY)
    parser.add_argument(
        "--poll",
        type=float,
        default=float(os.environ.get("TAUCETI_QUEUE_POLL_SECONDS") or 60.0),
        help="seconds between claims when nothing is claimable ($TAUCETI_QUEUE_POLL_SECONDS)",
    )
    parser.add_argument("--agent-timeout", type=float, default=3 * 3600)
    parser.add_argument("--build-timeout", type=float, default=3 * 3600)
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument(
        "--seed-packages",
        type=Path,
        default=_env_path("TAUCETI_QUEUE_SEED_PACKAGES"),
        help="share this extracted .lake/packages instead of lake exe cache get ($TAUCETI_QUEUE_SEED_PACKAGES)",
    )
    parser.add_argument(
        "--parallel-agents",
        type=int,
        default=repair_parallel.parallel_agents_default(),
        help="concurrent agents for independent failing groups, 0 or 1 for sequential ($TAUCETI_QUEUE_PARALLEL_AGENTS)",
    )
    parser.add_argument("--no-resume", action="store_true", help="do not continue checkpointed tasks on start")
    parser.add_argument(
        "--no-stream-repair",
        action="store_true",
        help="let the agent run the first whole-library build itself instead of the worker streaming it to the agent"
        " as it runs ($TAUCETI_QUEUE_STREAM_REPAIR=0)",
    )
    args = parser.parse_args(argv)
    if not args.service:
        parser.error("--service (or TAUCETI_QUEUE_SERVICE) is required")
    try:
        providers = parse_providers(args.providers)
    except ValueError as error:
        parser.error(f"--providers: {error}")
    if args.rate:
        from tauceti_worker.quota import parse_pace_curve

        try:
            parse_pace_curve(args.rate)
        except ValueError as error:
            parser.error(f"--rate: {error}")
    # The api provider's key, from the environment file; taken out of this process's environment so
    # only the api provider's agent ever sees it (and nothing logs it).
    api_key = (os.environ.pop("ANTHROPIC_API_KEY", "") or "").strip() or None

    clone = args.clone
    if clone is None:
        from tauceti_worker.config import Config

        clone = Config.resolve().checkout
    cfg = RunnerConfig(
        service=args.service,
        clone=clone,
        root=args.root,
        worker=args.worker,
        repository=args.repository,
        agent_timeout=args.agent_timeout,
        build_timeout=args.build_timeout,
        keep_worktree=args.keep_worktree,
        seed_packages=args.seed_packages,
        poll_seconds=args.poll,
        parallel_agents=args.parallel_agents,
        providers=providers,
        rate=args.rate or None,
        api_key=api_key,
        stream_repair=stream_repair_enabled(args.no_stream_repair),
    )
    if args.check:
        checks = preflight(cfg)
        for entry in checks:
            print(f"{'ok  ' if entry['ok'] else 'FAIL'} {entry['check']}: {entry['detail']}")
        return 0 if all(entry["ok"] for entry in checks) else 1
    client = make_client(cfg)
    admin = AdminCheck(cfg)
    if not args.once:
        stop = Stop()
        install_signal_handlers(stop)
        summary = serve(cfg, client, stop, admin, resume=not args.no_resume)
        log(f"stopped ({stop.reason or 'loop ended'}) after {summary['tasks']} task(s)")
        return 0
    admin.ensure()
    outcomes: list[dict] = []
    retry_releases(cfg, client)
    if not args.no_resume:
        outcomes += resume_pending(cfg, client)
    outcome = claim_and_run(cfg, client, admin)
    if outcome is not None:
        outcomes.append(outcome)
        print(json.dumps(outcome, indent=1, default=str))
    else:
        log("nothing claimable")
    if not outcomes:
        return 0
    return (
        0
        if all(o.get("status") in ("adopted", "released", "release_unsupported", "not_adopted") for o in outcomes)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())

"""The worker-side repair task of the AI merge queue (increment 1, stream G).

A worker claims a repair or rebase-conflict task from the queue service, prepares warm build
artifacts, runs the repair agent in a private worktree and returns a validated attributed chain. The
service alone adopts a chain; this module never publishes anything except the proposed tip under
``refs/heads/queue/task-<task_id>`` in the content repository, which the service reads.

Task protocol (``docs/queue/repair-task.md``), all JSON over HTTP with
``Authorization: Bearer <GitHub token of the worker's account>``:

- ``POST /tasks/claim`` ``{worker}`` returns 204 when nothing is claimable, else a Task;
- ``POST /tasks/<id>/renew`` ``{claim_token}`` returns ``{expires_at}``;
- ``POST /tasks/<id>/result`` ``{claim_token, tip, pushed_ref, notes}`` returns
  ``{adopted, reason, validation}``;
- ``POST /tasks/<id>/release`` ``{claim_token, reason, notes}`` gives the task back unfinished with a
  named reason (``artifacts_unavailable``, ``inference_unavailable``, ``agent_failed``,
  ``push_failed``); when the service has no such endpoint the worker lets the claim expire instead.

Authorization is the security audit's predicate (``security_audit.require_admin``): the account must
have admin permission on the repository, checked before any claim so a misconfigured worker fails
before it does work.

Phases, each checkpointed in ``<root>/tasks/<task_id>/task.json`` so a restarted runner can resume
the same model session instead of starting over: claimed, sources_ready, artifacts_ready,
agent_running, validated, pushed, submitted, then adopted / not_adopted / released / claim_lost.

CLI: ``python -m tauceti_queue.repair_task --service <url> [--once]``. The same ``RunnerConfig`` and
``claim_and_run`` are what the worker loop calls when the task is integrated as a work unit.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from tauceti_queue import repair
from tauceti_queue.repair import PromptContext, checkout_worktree, git, read_chain

SCHEMA = "tauceti-queue.repair-task/v1"
RESULT_REF_PREFIX = "refs/tauceti-queue/task"
PUSH_BRANCH = "queue/task-{task_id}"
RELEASE_REASONS = ("artifacts_unavailable", "inference_unavailable", "agent_failed", "push_failed")
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


@dataclasses.dataclass
class TaskMember:
    source_pr: str
    pr: int
    accepted_head: str
    commit: str | None = None
    provenance: str | None = None

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Task:
    task_id: str
    kind: str  # repair | rebase_conflict
    repository: str
    batch_id: str
    batch_version: int
    base: str
    tip: str
    members: list[TaskMember]
    diagnostics: dict
    retained_refs: list[str]
    claim_token: str
    expires_at: float
    renew_after: float

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
            source_pr = raw.get("source_pr") or f"{repository}#{raw['pr']}"
            pr = int(raw.get("pr") or source_pr.rsplit("#", 1)[1])
            members.append(
                TaskMember(
                    source_pr=str(source_pr),
                    pr=pr,
                    accepted_head=str(raw.get("accepted_head") or ""),
                    commit=raw.get("commit"),
                    provenance=raw.get("provenance"),
                )
            )
        return cls(
            task_id=str(data["task_id"]),
            kind=str(data["kind"]),
            repository=repository,
            batch_id=str(data.get("batch_id", "")),
            batch_version=int(data.get("batch_version") or 0),
            base=str(data["base"]),
            tip=str(data["tip"]),
            members=members,
            diagnostics=dict(data.get("diagnostics") or {}),
            retained_refs=list(data.get("retained_refs") or []),
            claim_token=str(data["claim_token"]),
            expires_at=float(data["expires_at"]),
            renew_after=float(data.get("renew_after") or max(5.0, (float(data["expires_at"]) - time.time()) / 3)),
        )

    def to_json(self) -> dict:
        record = dataclasses.asdict(self)
        record["members"] = [m.to_json() for m in self.members]
        return record

    @property
    def member_keys(self) -> list[str]:
        return [m.source_pr for m in self.members]

    @property
    def conflict(self) -> dict | None:
        conflict = self.diagnostics.get("conflict")
        return dict(conflict) if isinstance(conflict, dict) else None

    @property
    def validation_base(self) -> str:
        """The base the result must sit on: for a rebase conflict the target base, else the batch base."""
        if self.kind == "rebase_conflict" and self.conflict and self.conflict.get("onto"):
            return str(self.conflict["onto"])
        return self.base

    @property
    def push_branch(self) -> str:
        return PUSH_BRANCH.format(task_id=self.task_id)


class TaskClient:
    """The worker's side of the task protocol. Transport failures raise ``ServiceError``."""

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
            if status >= 500:
                raise ServiceError(f"{path}: HTTP {status}") from error
        except (urllib.error.URLError, OSError) as error:
            raise ServiceError(f"{path}: {error}") from error
        if not payload:
            return status, None
        try:
            return status, json.loads(payload)
        except ValueError as error:
            raise ServiceError(f"{path}: response is not JSON") from error

    def claim(self) -> Task | None:
        status, payload = self._post("/tasks/claim", {"worker": self.worker})
        if status == 204 or (status == 200 and not payload):
            return None
        if status in (401, 403):
            raise ClaimRefused(f"claim refused: HTTP {status} {(payload or {}).get('reason', '')}".strip())
        if status != 200 or not isinstance(payload, dict):
            raise ServiceError(f"claim: unexpected HTTP {status}")
        return Task.from_json(payload)

    def renew(self, task: Task) -> float:
        status, payload = self._post(f"/tasks/{task.task_id}/renew", {"claim_token": task.claim_token})
        if status in (401, 403, 404, 409, 410):
            raise ClaimRefused(f"renew refused: HTTP {status} {(payload or {}).get('reason', '')}".strip())
        if status != 200 or not isinstance(payload, dict) or "expires_at" not in payload:
            raise ServiceError(f"renew: unexpected HTTP {status}")
        return float(payload["expires_at"])

    def submit(self, task: Task, tip: str, pushed_ref: str, notes: dict | None) -> dict:
        status, payload = self._post(
            f"/tasks/{task.task_id}/result",
            {"claim_token": task.claim_token, "tip": tip, "pushed_ref": pushed_ref, "notes": notes or {}},
        )
        if status in (401, 403, 404, 409, 410):
            raise ClaimRefused(f"result refused: HTTP {status} {(payload or {}).get('reason', '')}".strip())
        if status != 200 or not isinstance(payload, dict):
            raise ServiceError(f"result: unexpected HTTP {status}")
        return payload

    def release(self, task: Task, reason: str, notes: dict | None = None) -> dict:
        status, payload = self._post(
            f"/tasks/{task.task_id}/release",
            {"claim_token": task.claim_token, "reason": reason, "notes": notes or {}},
        )
        if status in (404, 405, 501) and not (isinstance(payload, dict) and payload.get("task_id")):
            raise EndpointMissing(f"release: HTTP {status}")
        if status in (401, 403, 409, 410):
            raise ClaimRefused(f"release refused: HTTP {status} {(payload or {}).get('reason', '')}".strip())
        if status not in (200, 204):
            raise ServiceError(f"release: unexpected HTTP {status}")
        return payload if isinstance(payload, dict) else {"released": True}


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


def worker_token() -> str:
    """The GitHub token of the worker's account: explicit, the usual environment, else gh's store."""
    for key in ("TAUCETI_QUEUE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, check=False)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        from tauceti_worker.config import Die

        raise Die("no GitHub token for the queue service: set TAUCETI_QUEUE_TOKEN or log in with gh")
    return token


def verify_admin(repository: str) -> None:
    """The security audit's authorization predicate, applied to the task's repository."""
    from tauceti_worker.config import Die
    from tauceti_worker.security_audit import api

    if api(f"repos/{repository}").get("permissions", {}).get("admin") is not True:
        raise Die(f"the queue repair task requires repository-admin permission on {repository}")


def default_worker_identity() -> str:
    return f"{os.environ.get('TAUCETI_WORKER_ID', 'default')}@{socket.gethostname()}"


# ----------------------------------------------------------------------------------------------
# Runner configuration and checkpoints


@dataclasses.dataclass
class RunnerConfig:
    service: str
    clone: Path  # the worker's content checkout (``agents.prepare_checkout`` path)
    root: Path = DEFAULT_ROOT  # task state, logs, worktrees and the private artifact pool
    worker: str = dataclasses.field(default_factory=default_worker_identity)
    repository: str = repair.DEFAULT_REPOSITORY
    profile: object = "claude"  # the claude authoring profile the workers use for repair
    token: str | None = None
    agent_timeout: float = 3 * 3600
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
    poll_seconds: float = 60.0
    admin_recheck_seconds: float = 3600.0

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

    def session(self) -> str | None:
        from tauceti_worker.runtime_status import read_json

        value = read_json(self.session_path).get("session")
        return str(value) if value else None


# ----------------------------------------------------------------------------------------------
# Phases


def fetch_retained(clone: Path, task: Task) -> dict:
    """Fetch the task's retained refs into private refs of the clone and check base and tip exist."""
    fetched = []
    for ref in task.retained_refs:
        name = ref.removeprefix("refs/heads/").removeprefix("refs/")
        private = f"{RESULT_REF_PREFIX}/{task.task_id}/retained/{name}"
        git(clone, "fetch", "--quiet", "origin", f"+{ref}:{private}")
        fetched.append(private)
    for sha in (task.base, task.tip, task.validation_base):
        if git(clone, "cat-file", "-t", sha, check=False).strip() != "commit":
            git(clone, "fetch", "--quiet", "origin", sha, check=False)
        if git(clone, "cat-file", "-t", sha, check=False).strip() != "commit":
            raise RuntimeError(f"commit {sha[:12]} is not in the content repository after fetching the retained refs")
    git(clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/input", task.tip)
    return {"refs": fetched, "base": task.base, "tip": task.tip}


def task_findings(task: Task) -> dict:
    return repair.normalise_findings(task.diagnostics)


def prompt_context(task: Task, feedback: str | None) -> PromptContext:
    conflict = task.conflict
    if conflict is not None:
        conflict.setdefault("previous_base", task.base)
    return PromptContext(
        repository=task.repository,
        base=task.validation_base,
        kind=task.kind,
        origin=f"The queue exposed it as {task.kind.replace('_', ' ')} task {task.task_id} "
        f"(batch {task.batch_id or '?'}, version {task.batch_version}).",
        conflict=conflict,
        feedback=feedback,
        members=[
            {"source_pr": m.source_pr, "accepted_head": m.accepted_head, "provenance": m.provenance}
            for m in task.members
        ],
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
    validation = response.get("validation")
    if isinstance(validation, dict):
        violations = validation.get("violations")
        if isinstance(violations, list) and violations:
            return "\n".join(
                f"- {v.get('code', 'violation')}: {v.get('detail', '')}" if isinstance(v, dict) else f"- {v}"
                for v in violations
            )
        return json.dumps(validation, indent=1, default=str)[:4000]
    if isinstance(validation, str):
        return validation
    return f"- {response.get('reason', 'the service did not adopt the chain')}"


def read_telemetry(logdir: Path, provider: str) -> dict:
    from tauceti_worker.runtime_status import read_json

    return read_json(logdir / f"agent-{provider}-telemetry.json")


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
    label: str = "repair",
) -> dict:
    """One agent run through the worker's host path, with bounded retries on inference unavailability.

    Returns ``{"status": "ok"}`` on a clean exit, ``"cancelled"`` when the claim was lost, ``"failed"``
    for the agent's own failure, ``"inference_unavailable"`` after the bounded retries, and
    ``"artifacts_unavailable"`` when the worker's cache preparation refused to start inference.
    """
    from tauceti_worker.agents import BuildCacheUnavailable, run_agent_host, take_last_agent_infra_failure
    from tauceti_worker.config import NoProgress
    from tauceti_worker.repair_recovery import environment

    profile = repair.claude_profile() if cfg.profile == "claude" else cfg.profile
    provider = getattr(profile, "provider", "claude")
    attempts: list[dict] = []
    reason = None
    prompt_path = logdir / f"prompt-{label}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)
    for index in range(1, cfg.inference_attempts + 1):
        if cancel.is_set():
            return {"status": "cancelled", "attempts": attempts}
        changes = repair.isolation_env(cfg.root, env_lake)
        changes["TAUCETI_ROUND_DEADLINE"] = str(time.time() + cfg.agent_timeout)
        started = time.monotonic()
        state.write("agent_running", agent_label=label, agent_attempt=index, prompt=str(prompt_path), resume=resume)
        # The worker's cap on one attempt; the plan gives the task itself no time budget.
        expired = threading.Event()
        timer = threading.Timer(cfg.agent_timeout, _expire_attempt, args=(expired, cancel))
        timer.daemon = True
        timer.start()
        try:
            with environment(**changes):
                rc = run_agent_host(
                    worktree,
                    prompt,
                    profile,
                    logdir,
                    cancel_event=cancel,
                    resume=resume,
                    session_path=state.session_path,
                    session_actor="queue-repair-task",
                    session_scope=task.task_id,
                )
        except BuildCacheUnavailable as error:
            return {"status": "artifacts_unavailable", "reason": str(error), "attempts": attempts}
        except NoProgress as error:
            if cancel.is_set() and not expired.is_set():
                return {"status": "cancelled", "attempts": attempts}
            rc, reason = 75, f"agent could not start: {error}"
        else:
            reason = take_last_agent_infra_failure()
        finally:
            timer.cancel()
        if expired.is_set():
            cancel.clear()  # the lease is still live; only this attempt is over
        session = state.session()
        record = {
            "rc": rc,
            "seconds": round(time.monotonic() - started, 1),
            "session": session,
            "infrastructure_failure": reason,
            "telemetry": read_telemetry(logdir, provider),
        }
        usage = (record["telemetry"] or {}).get("usage") or {}
        record["estimated_cost_usd"] = repair.price_usage(provider, getattr(profile, "model", None), usage)
        attempts.append(record)
        if expired.is_set():
            record["timed_out"] = True
            return {"status": "failed", "attempts": attempts, "session": session, "rc": rc, "timed_out": True}
        if cancel.is_set():
            return {"status": "cancelled", "attempts": attempts, "session": session}
        if rc == 0:
            return {"status": "ok", "attempts": attempts, "session": session}
        if not reason:
            return {"status": "failed", "attempts": attempts, "session": session, "rc": rc}
        log(f"inference unavailable ({reason}); attempt {index}/{cfg.inference_attempts}")
        resume = session or resume  # a session that exists is continued, not restarted
        if index < cfg.inference_attempts:
            delay = cfg.inference_delays[min(index - 1, len(cfg.inference_delays) - 1)] if cfg.inference_delays else 0
            if cancel.wait(delay):
                return {"status": "cancelled", "attempts": attempts, "session": session}
    return {"status": "inference_unavailable", "reason": reason, "attempts": attempts}


def _expire_attempt(expired: threading.Event, cancel: threading.Event) -> None:
    expired.set()
    cancel.set()


def push_result(cfg: RunnerConfig, task: Task, worktree: Path, tip: str, logdir: Path, expect: str | None) -> dict:
    """Push the proposed tip to ``refs/heads/queue/task-<id>`` through ``git-safe-push``.

    ``expect`` is the oid this task last pushed (branch CAS); the first push of a task is create-only
    and, when the branch already exists from an earlier claimant of the same task, is retried once
    against the oid observed on the remote: the service re-exposed the task, so that chain is stale.
    """
    remote = cfg.push_remote or f"https://github.com/{task.repository}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TAUCETI_PUSH_", "TAUCETI_CLAIM_", "GIT_CONFIG_"))}
    env.update(
        TAUCETI_PUSH_REF=task.push_branch,
        TAUCETI_PUSH_EXPECT=expect or "",
        TAUCETI_PUSH_REMOTE=remote,
        TAUCETI_PUSH_OID=tip,
        TAUCETI_PUSH_SCOPE=f"queue-task-{task.task_id}",
    )
    env.pop("TAUCETI_RECOVERY_CONTINUATION", None)
    record = repair.run_logged([str(GIT_SAFE_PUSH)], worktree, logdir / "push.log", env=env, timeout=600)
    if record["rc"] == 78 and not expect:
        observed = git(worktree, "ls-remote", remote, f"refs/heads/{task.push_branch}", check=False).split()
        if observed:
            env["TAUCETI_PUSH_EXPECT"] = observed[0]
            record = repair.run_logged([str(GIT_SAFE_PUSH)], worktree, logdir / "push.log", env=env, timeout=600)
            record["superseded"] = observed[0]
    record["ref"] = f"refs/heads/{task.push_branch}"
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
    notes = {"worker": cfg.worker, "detail": detail}
    outcome = outcome if outcome is not None else {"task_id": task.task_id}
    outcome.update(status="released", reason=reason, detail=detail)
    try:
        outcome["response"] = with_retries(
            lambda: client.release(task, reason, notes),
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
    state.write(outcome["status"], reason=reason, detail=outcome["detail"], outcome=outcome)
    return outcome


def run_task(cfg: RunnerConfig, task: Task, client: TaskClient, *, resume_state: TaskState | None = None) -> dict:
    """Run one claimed task to its outcome. The claim is renewed throughout and the result discarded
    if it is lost. Returns the outcome record (also written to the task's state directory)."""
    state_dir = cfg.tasks_dir / task.task_id
    logdir = state_dir / "logs"
    state = resume_state or TaskState(state_dir, task)
    resuming = resume_state is not None and resume_state.state == "agent_running"
    cancel = threading.Event()
    lease = ClaimLease(client, task, cancel, retry_interval=cfg.lease_retry_interval)
    lease.start()
    worktree = cfg.root / "worktrees" / f"task-{task.task_id}"
    outcome: dict = {"task_id": task.task_id, "status": "running", "worker": cfg.worker}
    started = time.monotonic()

    def discard(detail: str) -> dict:
        outcome.update(status="claim_lost", detail=detail)
        state.write("claim_lost", detail=detail, outcome=outcome)
        return outcome

    try:
        state.write("claimed", worker=cfg.worker, service=cfg.service, resumed=resuming)
        if task.repository != cfg.repository:
            verify_admin(task.repository)
        env_lake = repair.lake_env(cfg.root)
        if not resuming:
            outcome["sources"] = fetch_retained(cfg.clone, task)
            outcome["sources"]["worktree_reused"] = checkout_worktree(cfg.clone, task.tip, worktree)
            if cfg.seed_packages:
                repair.link_shared_packages(worktree, cfg.seed_packages)
            state.write("sources_ready", worktree=str(worktree), sources=outcome["sources"])
            artifacts = repair.prepare_artifacts(
                worktree,
                task.base,
                env_lake,
                logdir,
                cfg.build_timeout,
                repository=task.repository,
                mathlib_cache=cfg.mathlib_cache and cfg.seed_packages is None,
                lake=cfg.lake,
            )
            outcome["artifacts"] = {k: v for k, v in artifacts.items() if k != "incremental_build"} | {
                "incremental_build": {
                    k: v for k, v in (artifacts.get("incremental_build") or {}).items() if "load" not in k
                }
            }
            state.write("artifacts_ready" if artifacts["available"] else "artifacts_unavailable", artifacts=artifacts)
            if not artifacts["available"]:
                return release_task(
                    cfg, client, task, state, "artifacts_unavailable", artifacts["unavailable_reason"], outcome, lease
                )
        elif not worktree.is_dir():
            return release_task(
                cfg, client, task, state, "agent_failed", "resume: the worktree is gone", outcome, lease
            )
        chain_before = read_chain(cfg.clone, task.base, task.tip)
        findings = task_findings(task)
        feedback: str | None = None
        pushed: str | None = None
        attempt = 0
        validation_retries = 0
        agent_records: list[dict] = []
        resume_session = state.session() if resuming else None
        while True:
            attempt += 1
            if cancel.is_set():
                return discard(lease.lost_reason or "cancelled before the agent started")
            if resume_session:
                prompt = (
                    "The worker running this task was interrupted and has resumed your session; the worktree is as "
                    "you left it. Continue the task from where you were, and finish with the summary the original "
                    "instructions ask for.\n\n"
                    + repair.compose_prompt(prompt_context(task, feedback), findings, chain_before, cfg.clone)
                )
            else:
                prompt = repair.compose_prompt(prompt_context(task, feedback), findings, chain_before, cfg.clone)
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
                label=f"attempt-{attempt}",
            )
            resume_session = None
            agent_records.append(agent)
            outcome["agent"] = agent_records
            if agent["status"] == "cancelled":
                return discard(lease.lost_reason or "the agent was cancelled")
            if agent["status"] == "inference_unavailable":
                return release_task(
                    cfg, client, task, state, "inference_unavailable", agent.get("reason"), outcome, lease
                )
            if agent["status"] == "artifacts_unavailable":
                return release_task(
                    cfg, client, task, state, "artifacts_unavailable", agent.get("reason"), outcome, lease
                )
            tip = git(worktree, "rev-parse", "HEAD").strip()
            git(cfg.clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/attempt-{attempt}", tip)
            report = repair.chain_report(cfg.clone, task.validation_base, task.tip, tip, task.member_keys)
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
                    label=f"attempt-{attempt}-explanation",
                )
                agent_records.append(follow)
                if follow["status"] == "cancelled":
                    return discard(lease.lost_reason or "the explanation turn was cancelled")
                if follow["status"] == "inference_unavailable":
                    return release_task(
                        cfg, client, task, state, "inference_unavailable", follow.get("reason"), outcome, lease
                    )
                tip = git(worktree, "rev-parse", "HEAD").strip()
                git(cfg.clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/attempt-{attempt}", tip)
                report = repair.chain_report(cfg.clone, task.validation_base, task.tip, tip, task.member_keys)
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
            if cancel.is_set():
                return discard(lease.lost_reason or "claim lost before the push")
            push = push_result(cfg, task, worktree, tip, logdir, pushed)
            outcome["push"] = {k: v for k, v in push.items() if "load" not in k}
            if not push["ok"]:
                return release_task(
                    cfg, client, task, state, "push_failed", f"git-safe-push exited {push['rc']}", outcome, lease
                )
            pushed = tip
            state.write("pushed", tip=tip, pushed_ref=push["ref"])
            if cancel.is_set():
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
            if response.get("adopted"):
                lease.stop()
                outcome.update(status="adopted", tip=tip, pushed_ref=push["ref"])
                state.write("adopted", outcome=outcome)
                return outcome
            if response.get("reason") == "validation" and validation_retries < cfg.validation_retries:
                lease.submitting.clear()
                validation_retries += 1
                feedback = "The queue's validation of the previous attempt's chain:\n" + format_service_validation(
                    response
                )
                log(f"chain not adopted ({response.get('reason')}); retrying with the validation report")
                continue
            outcome.update(status="not_adopted", tip=tip, pushed_ref=push["ref"], reason=response.get("reason"))
            state.write("not_adopted", outcome=outcome)
            return outcome
    except Exception as error:  # the state record must say what happened
        outcome.update(status="error", error=f"{type(error).__name__}: {error}")
        with contextlib.suppress(Exception):
            state.write("error", error=outcome["error"], outcome=outcome)
        raise
    finally:
        lease.stop()
        outcome["seconds"] = round(time.monotonic() - started, 1)
        outcome["lease"] = {"renewals": lease.renewals, "errors": lease.errors, "lost": lease.lost.is_set()}
        if not cfg.keep_worktree and outcome.get("status") != "error":
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


def claim_and_run(cfg: RunnerConfig, client: TaskClient, admin: AdminCheck | None = None) -> dict | None:
    """Verify the account, claim one task and run it. None when nothing was claimable."""
    (admin or AdminCheck(cfg)).ensure()
    task = with_retries(client.claim, attempts=cfg.service_attempts, delays=cfg.service_delays, what="claim")
    if task is None:
        return None
    log(
        f"claimed {task.kind} task {task.task_id} ({len(task.members)} members, expires in {task.expires_at - time.time():.0f}s)"
    )
    outcome = run_task(cfg, task, client)
    log(f"task {task.task_id}: {outcome['status']}" + (f" ({outcome.get('reason')})" if outcome.get("reason") else ""))
    return outcome


def resume_pending(cfg: RunnerConfig, client: TaskClient) -> list[dict]:
    """Continue tasks a previous runner left mid-agent while their claim is still live.

    The checkpoint names the task, the claim token and the provider session; the claim is renewed
    first, and if the service refuses the task is recorded as lost. A live claim resumes the same
    model session in the same worktree instead of restarting the attempt.
    """
    outcomes = []
    if not cfg.tasks_dir.is_dir():
        return outcomes
    for directory in sorted(cfg.tasks_dir.iterdir()):
        from tauceti_worker.runtime_status import read_json

        record = read_json(directory / "task.json")
        if record.get("state") != "agent_running" or not isinstance(record.get("task"), dict):
            continue
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
        log(f"resuming task {task.task_id} (session {state.session() or 'unknown'})")
        outcomes.append(run_task(cfg, task, client, resume_state=state))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tauceti_queue.repair_task", description=__doc__.split("\n\n")[0])
    parser.add_argument("--service", required=True, help="queue service base URL")
    parser.add_argument("--once", action="store_true", help="claim at most one task, then exit")
    parser.add_argument("--worker", default=default_worker_identity(), help="worker identity string")
    parser.add_argument("--clone", type=Path, help="content repository checkout (default: the worker's checkout)")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="task state, logs, worktrees and artifact pool")
    parser.add_argument("--repository", default=repair.DEFAULT_REPOSITORY)
    parser.add_argument("--poll", type=float, default=60.0, help="seconds between claims when nothing is claimable")
    parser.add_argument("--agent-timeout", type=float, default=3 * 3600)
    parser.add_argument("--build-timeout", type=float, default=3 * 3600)
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument(
        "--seed-packages", type=Path, help="share this extracted .lake/packages instead of lake exe cache get"
    )
    parser.add_argument("--no-resume", action="store_true", help="do not continue checkpointed tasks on start")
    args = parser.parse_args(argv)

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
    )
    client = make_client(cfg)
    admin = AdminCheck(cfg)
    admin.ensure()
    outcomes: list[dict] = []
    if not args.no_resume:
        outcomes += resume_pending(cfg, client)
    while True:
        outcome = claim_and_run(cfg, client, admin)
        if outcome is not None:
            outcomes.append(outcome)
            print(json.dumps(outcome, indent=1, default=str))
            if args.once:
                break
        elif args.once:
            log("nothing claimable")
            break
        else:
            time.sleep(cfg.poll_seconds)
    if not outcomes:
        return 0
    return (
        0
        if all(o.get("status") in ("adopted", "released", "release_unsupported", "not_adopted") for o in outcomes)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())

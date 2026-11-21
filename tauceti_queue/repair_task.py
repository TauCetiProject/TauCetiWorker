"""The worker-side repair task of the AI merge queue (increment 1, stream G).

A worker claims a repair or rebase-conflict task from the queue service, prepares warm build
artifacts, runs the repair agent in a private worktree and returns a validated attributed chain. The
service alone adopts a chain; this module never publishes anything except the proposed tip under
the task's submission ref in the content repository, which the service fetches, and the green
tip's build outputs to the workers' own cache namespace (``repair_publish``), which CI never reads.

Task protocol (``docs/queue/repair-task.md``), all JSON over HTTP with
``Authorization: Bearer <GitHub token of the worker's account>``:

- ``POST /tasks/claim`` ``{worker, revision}`` returns 204 when nothing is claimable, else the controller's
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
before it does work. The positive answer is cached in ``<root>/admin-check.json`` for
``admin_recheck_seconds`` (six hours); a check that cannot be made (rate limit, 5xx, network) keeps the
cached answer, or without one claims anyway, since the service enforces admin on every claim. Only the
API saying the account is not an admin stops the worker.

Phases, each recorded in ``<root>/tasks/<task_id>/task.json``: claimed, sources_ready, artifacts_ready,
agent_running, validated, pushed, submitted, then adopted / not_adopted / released / claim_lost. A task
is all-or-nothing: one stopped or killed before its result was delivered is abandoned, its claim
released and its worktree removed. Only the delivery of a validated result (the push to the
submission ref and the ``result`` call) is retried by a later start.

CLI: ``python -m tauceti_queue.repair_task --service <url> [--once | --check]``. Without ``--once`` the
runner is a long-running worker (``serve``): it retries pending deliveries, claims, runs and submits,
reports provider outcomes to ``POST /tasks/inference`` for the service's inference hold, sleeps the
poll interval when nothing is claimable, survives a task's failure, and on SIGTERM stops the running
agent and releases its claim (``services/queue/tauceti-queue-repair.service``). The same ``RunnerConfig`` and ``claim_and_run`` are
what the worker loop calls when the task is integrated as a work unit.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from tauceti_queue import chain as chain_tool
from tauceti_queue import (
    repair,
    repair_audit,
    repair_context,
    repair_lookahead,
    repair_numproj,
    repair_parallel,
    repair_publish,
    repair_sandbox,
    repair_stream,
    runtime,
    task_transport,
)
from tauceti_queue.ports import REPAIR_CHECKPOINT
from tauceti_queue.repair import PromptContext, checkout_worktree, git, read_chain
from tauceti_queue.task_identity import (
    TASK_SCHEMA,
    NativeTaskExecutionUnavailable,
    TaskBinding,
    TaskBindingMismatch,
    validate_native_task,
)

SCHEMA = "tauceti-queue.repair-task/v1"
RESULT_REF_PREFIX = "refs/tauceti-queue/task"
SUBMISSION_PREFIX = "refs/tauceti-queue/submissions"
# A batch's accepted conflict resolutions, kept until one of its rebase tasks is answered so a rerun or a
# later task of the batch resumes after them:
# ``<prefix>/<batch_id>/<conflicting commit>-<clean prefix tree>`` (``store_resolution``).
RESOLUTION_PREFIX = "refs/tauceti-queue/batch-resolutions"
# ``prepare`` builds an open batch on its own base and repairs what fails before the batch is the head.
TASK_KINDS = ("repair", *repair.REBASE_KINDS, "prepare")
RELEASE_REASONS = (
    "artifacts_unavailable",
    "inference_unavailable",
    "agent_launch_failed",
    "agent_failed",
    "push_failed",
    "submission_unavailable",  # preflight failed before inference; retry without settling preparation
    "worker-stopped",  # the worker was stopped (SIGTERM) or restarted before the result was delivered
    "prepared_clean",  # a ``prepare`` task's batch built without an error: nothing to repair
)
# Checkpoint states with the claim still ours and the task unfinished; everything else is settled.
IN_FLIGHT_STATES = (
    "claimed",
    "sources_ready",
    "artifacts_ready",
    "artifacts_unavailable",
    "agent_running",
    "validated",
    "conflict_validated",  # a rebase task between conflicts: nothing to deliver yet
    "delivery_pending",
    "verifying",
    "pushed",
    "submitted",
    "interrupted",
)
# In-flight states whose validated result a restarted worker still delivers; any other in-flight
# task is abandoned and its claim released. ``interrupted`` is only ever read from older records.
DELIVERY_STATES = ("delivery_pending", "pushed", "submitted")
# ``run_repair_agent`` statuses that release the task under the same name.
RELEASED_AGENT_STATUSES = ("inference_unavailable", "artifacts_unavailable", "agent_launch_failed")

PROVIDERS = repair.PROVIDERS  # claude | codex | api, as the chain names them
DEFAULT_PROVIDERS = ("claude", "codex", "api")
SUBSCRIPTION_PROVIDERS = ("claude", "codex")  # paced by the fleet's quota reader; ``api`` is not
# Checkpoint states before the agent has run: an error there is released as ``artifacts_unavailable``.
PRE_AGENT_PHASES = (None, "claimed", "sources_ready", "artifacts_ready", "artifacts_unavailable")
HERE = Path(__file__).resolve().parent.parent
STARTUP_REVISION = runtime.running_revision().sha
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


class NativeTaskDeliveryUncertain(ServiceError):
    """A native POST may have committed; reconcile it instead of resending."""


class ClaimRefused(Exception):
    """The service no longer recognises this claim (expired, superseded or never ours)."""


class EndpointMissing(Exception):
    """The service has no such endpoint (an older service without ``release``)."""


CLAIM_CODES = ("unknown-task", "unknown-claim", "claim-expired", "claim-superseded", "not-authorized")
TERMINAL_REJECTIONS = ("version-mismatch", "tip-unavailable", "not-authorized", "unknown-task", "policy-changed")


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
    no_repair: bool = False  # the PR opted out of repair: never edited, only emptied with an attribution
    withdrawn: bool = False  # already withdrawn that way: it stays an empty commit
    declaration_names: dict = dataclasses.field(default_factory=dict)
    source_identity: str | dict = ""
    large_refactor: bool = False  # an admin's label lifts the review-size caps for its commit

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
    kind: str  # repair | rebase_conflict | pre_rebase | prepare
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
    warm_revs: list[str] = dataclasses.field(default_factory=list)  # repair outputs to restore, in order
    binding: TaskBinding | None = None
    mixed_binding: dict | None = None

    @classmethod
    def from_json(cls, data: dict) -> Task:
        if not isinstance(data, dict):
            raise ValueError("task record must be an object")
        binding = None
        mixed_binding = None
        from .mixed_tasks import SCHEMA as MIXED_SCHEMA
        from .mixed_tasks import validate as validate_mixed

        if data.get("schema") == MIXED_SCHEMA:
            from .mixed_identity import decode

            data = json.loads(json.dumps(data), object_hook=decode)
            validate_mixed(data)
            mixed_binding = data["mixed_binding"]
        elif "mixed_binding" in data or any(
            isinstance(member, dict)
            and (
                isinstance(member.get("source_identity"), dict)
                or str(member.get("source_pr", "")).startswith("forgejo:")
                and "@" in str(member.get("source_pr", ""))
            )
            for member in data.get("members", [])
        ):
            raise TaskBindingMismatch("mixed task identity schema was removed")
        elif "binding" in data or "schema" in data:
            if data.get("schema") != TASK_SCHEMA or "repository" in data:
                raise TaskBindingMismatch("unknown or downgraded task identity schema")
            binding = TaskBinding.parse(data.get("binding"))
        for key in ("task_id", "kind", "base", "tip", "members", "claim_token", "expires_at"):
            if key not in data:
                raise ValueError(f"task record lacks {key!r}")
        if binding is None and "repository" not in data:
            raise ValueError("task record lacks 'repository'")
        if data["kind"] not in TASK_KINDS:
            raise ValueError(f"unknown task kind {data['kind']!r}")
        repository = "" if binding is not None else str(data["repository"])
        if binding is not None:
            validate_native_task(data)
        members = []
        for raw in data["members"]:
            if binding is not None:
                if not isinstance(raw, dict):
                    raise TaskBindingMismatch("invalid qualified task member")
                identity = binding.pull_key(raw.get("number"))
                if raw.get("source_identity") != identity or raw.get("source_pr") not in {
                    f"{binding.source.repository.name}#{raw['number']}",
                    f"{binding.source.repository.key}#{raw['number']}",
                    identity,
                }:
                    raise TaskBindingMismatch("task member belongs to another qualified source")
            number = raw.get("number", raw.get("pr"))
            if mixed_binding is not None:
                from tauceti_tools.ci_candidate import RepositoryIdentity

                from .mixed_identity import SourceNumber

                source = RepositoryIdentity.parse(raw["source_identity"])
                if source.repository.forge.kind == "forgejo":
                    number = SourceNumber(number, source)
            source_pr = raw.get("source_pr") or f"{repository}#{number}"
            members.append(
                TaskMember(
                    source_pr=str(source_pr),
                    number=number
                    if mixed_binding is not None
                    else int(number if number is not None else source_pr.rsplit("#", 1)[1]),
                    accepted_head=str(raw.get("accepted_head") or ""),
                    commit=raw.get("commit"),
                    provenance=raw.get("provenance"),
                    title=str(raw.get("title") or ""),
                    description=str(raw.get("description") or ""),
                    author=str(raw.get("author") or ""),
                    explanation=str(raw.get("explanation") or ""),
                    retained_ref=str(raw.get("retained_ref") or ""),
                    no_repair=bool(raw.get("no_repair")),
                    withdrawn=bool(raw.get("withdrawn")),
                    declaration_names=raw.get("declaration_names")
                    if isinstance(raw.get("declaration_names"), dict)
                    else {},
                    source_identity=raw.get("source_identity", "")
                    if binding is not None or mixed_binding is not None
                    else "",
                    large_refactor=raw.get("large_refactor") is True,
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
                data.get("renew_after")
                or extra.get("renew_after")
                or (
                    min(300.0, max(5.0, (expires_at - time.time()) / 3))
                    if binding is not None
                    else max(5.0, (expires_at - time.time()) / 3)
                )
            ),
            instruction=str(data.get("instruction") or ""),
            created_at=float(data["created_at"]) if data.get("created_at") is not None else None,
            extra=extra,
            warm_revs=[str(r) for r in (data.get("warm_revs") or extra.get("warm_revs") or []) if r],
            binding=binding,
            mixed_binding=mixed_binding,
        )

    def to_json(self) -> dict:
        record = dataclasses.asdict(self)
        record["members"] = [m.to_json() for m in self.members]
        record.pop("binding")
        record.pop("mixed_binding")
        if self.mixed_binding is not None:
            from .mixed_tasks import SCHEMA

            record.update(schema=SCHEMA, mixed_binding=self.mixed_binding)
            for member in record["members"]:
                member["number"] = int(member["number"])
            from .mixed_identity import encode

            record = encode(record)
        elif self.binding is not None:
            record.pop("repository")
            record.update(schema=TASK_SCHEMA, binding=self.binding.record())
        else:
            for member in record["members"]:
                member.pop("source_identity")
        return record

    @property
    def member_keys(self) -> list[str]:
        return [m.source_pr for m in self.members]

    @property
    def large_refactor_heads(self) -> frozenset[tuple]:
        """``(number, accepted_head)`` of the members whose review-size caps an admin's label lifts."""
        return frozenset((m.number, m.accepted_head) for m in self.members if m.large_refactor)

    @property
    def validation_base(self) -> str:
        """The base the result must sit on: ``onto`` (the batch base itself for a repair)."""
        return self.onto or self.base


class _NoTaskRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TaskClient:
    """The worker's side of the task protocol. Transport failures raise ``ServiceError``.

    Status codes: the service answers a claim that is no longer the caller's with 403/404/409/410, or
    (through stream F's generic error mapping of ``TaskError``) with a JSON ``error``/``code`` naming
    one of ``unknown-task``, ``unknown-claim``, ``claim-expired``, ``claim-superseded``,
    ``not-authorized``; both are ``ClaimRefused``. 5xx without such a code is ``ServiceError``.
    """

    def __init__(
        self,
        service: str,
        token: str | Callable[[], str],
        worker: str,
        timeout: float = 30.0,
        *,
        binding: TaskBinding | None = None,
    ):
        self.service = service.rstrip("/")
        self.token = token
        self.worker = worker
        self.timeout = timeout
        if binding is not None and not isinstance(binding, TaskBinding):
            raise ValueError("a trusted qualified task binding is required")
        self.binding = binding
        if binding is not None:
            task_transport.check_timeout(timeout)
            if (
                not isinstance(worker, str)
                or not 0 < len(worker) <= 256
                or not all(33 <= ord(c) <= 126 for c in worker)
            ):
                raise ValueError("native task worker must be a bounded printable ASCII header value")
            url = urllib.parse.urlsplit(self.service)
            _ = url.port  # Validate malformed ports before constructing a credential-bearing request.
            if (
                not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}))
            ):
                raise ValueError("native task service requires HTTPS or an explicit loopback fixture")
            if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
                raise ValueError("an explicit native queue bearer is required")

    def _require_task(self, task):
        if getattr(task, "binding", None) != self.binding:
            raise TaskBindingMismatch("task source/execution policy differs from the trusted client binding")

    def _post(self, path: str, body: dict) -> tuple[int, dict | None]:
        if self.binding is not None:
            body = {**body, "schema": TASK_SCHEMA, "task_binding": self.binding.record()}
        data = json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.service}{path}",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token() if callable(self.token) else self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"tauceti-queue-repair-task/{self.worker}",
                "X-TauCeti-Worker": self.worker,
            },
        )
        try:
            if self.binding is not None:
                status, payload = task_transport.post(
                    request.full_url, data, dict(request.header_items()), timeout=self.timeout
                )
            else:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    payload = response.read()
        except task_transport.TaskTransportError as error:
            failure = NativeTaskDeliveryUncertain if error.delivery_uncertain else ServiceError
            raise failure(f"{path}: {error}") from None
        except urllib.error.HTTPError as error:
            status = error.code
            with error:
                payload = error.read(16 * 1024 * 1024 + 1) if self.binding is not None else error.read()
        except (urllib.error.URLError, OSError) as error:
            raise ServiceError(f"{path}: {error}") from error
        parsed = None
        if self.binding is not None and len(payload) > 16 * 1024 * 1024:
            raise ServiceError("native task response exceeds its byte bound")
        if payload:
            try:
                if self.binding is not None:
                    from tauceti_tools.ci_candidate import read_json

                    parsed = read_json(payload, max_bytes=16 * 1024 * 1024)
                else:
                    parsed = json.loads(payload)
            except ValueError as error:
                if self.binding is not None:
                    raise NativeTaskDeliveryUncertain(f"{path}: response is not JSON; delivery is uncertain") from None
                if status >= 500:
                    raise ServiceError(f"{path}: HTTP {status}") from error
                raise ServiceError(f"{path}: response is not JSON") from error
        if status >= 400 and _claim_code(parsed):
            raise ClaimRefused(f"{path}: HTTP {status} {_claim_code(parsed)}: {_error_text(parsed)}".strip())
        if status >= 500:
            if self.binding is not None:
                raise NativeTaskDeliveryUncertain(f"{path}: HTTP {status}; delivery is uncertain")
            raise ServiceError(f"{path}: HTTP {status} {_error_text(parsed)}".strip())
        return status, parsed

    def claim(self) -> Task | None:
        body = {"worker": self.worker}
        if re.fullmatch(r"[0-9a-f]{7,64}", STARTUP_REVISION or ""):
            body["revision"] = STARTUP_REVISION  # a stalled repair is offered at once to code that has not tried it
        if self.binding is None:
            from .mixed_tasks import SCHEMA

            body["task_schemas"] = [SCHEMA]
        status, payload = self._post("/tasks/claim", body)
        if status == 204 or (self.binding is None and status == 200 and not payload):
            return None
        if status in (401, 403):
            raise ClaimRefused(f"claim refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200 or not isinstance(payload, dict):
            if self.binding is not None:
                raise NativeTaskDeliveryUncertain(
                    f"claim: unexpected HTTP {status} or native response; delivery is uncertain"
                )
            raise ServiceError(f"claim: unexpected HTTP {status} {_error_text(payload)}".strip())
        try:
            task = Task.from_json(payload)
        except TaskBindingMismatch:
            raise
        except (TypeError, ValueError, KeyError):
            if self.binding is None:
                raise
            raise NativeTaskDeliveryUncertain("claim: invalid native task record; delivery is uncertain") from None
        self._require_task(task)
        return task

    def renew(self, task: Task) -> float:
        self._require_task(task)
        status, payload = self._post(
            f"/tasks/{task.task_id}/renew", {"worker": self.worker, "claim_token": task.claim_token}
        )
        if status == 401:
            # The service could not authenticate the worker (its GitHub identity lookup failing, as
            # during a quota outage), which says nothing about the claim: a refused claim answers 409
            # with its code (raised by `_post`). The lease keeps retrying until the claim expires.
            raise ServiceError(f"renew: HTTP 401 {_error_text(payload)}".strip())
        if status in (403, 404, 409, 410):
            raise ClaimRefused(f"renew refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200:
            raise ServiceError(f"renew: unexpected HTTP {status}")
        # The controller returns the new expiry as a number; a dict form ``{expires_at}`` is accepted too.
        value = payload.get("expires_at") if isinstance(payload, dict) else payload
        if self.binding is not None and (type(value) not in (int, float) or not 0 < value < 2**53):
            raise ServiceError("renew: invalid native claim expiry")
        if not isinstance(value, (int, float)):
            raise ServiceError("renew: no expires_at in the response")
        if isinstance(payload, dict):
            interval = payload.get("renew_after")
            if type(interval) in (int, float) and 0 < interval <= 300:
                task.renew_after = float(interval)
        return float(value)

    def submit(self, task: Task, tip: str, pushed_ref: str, notes: dict | None, *, checkpoint: bool = False) -> dict:
        """``submit_task(task_id, claim_token, tip_sha, worker_notes)``; returns the Adoption record."""
        self._require_task(task)
        status, payload = self._post(
            f"/tasks/{task.task_id}/{'checkpoint' if checkpoint else 'result'}",
            {
                "worker": self.worker,
                "claim_token": task.claim_token,
                "tip_sha": tip,
                "pushed_ref": pushed_ref,
                "worker_notes": json.dumps(notes or {}, sort_keys=True, default=str),
            },
        )
        if checkpoint and status == 404:
            raise EndpointMissing("repair checkpoint endpoint unavailable")
        if status in (401, 403, 404, 409, 410):
            raise ClaimRefused(f"result refused: HTTP {status} {_error_text(payload)}".strip())
        if status != 200 or not isinstance(payload, dict):
            raise ServiceError(f"result: unexpected HTTP {status}")
        if "accepted" not in payload and "adopted" in payload:
            payload["accepted"] = bool(payload["adopted"])
        return payload

    def release(self, task: Task, reason: str, error: str = "") -> dict:
        """``release_task(task_id, claim_token, reason, error)``."""
        self._require_task(task)
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
        except NativeTaskDeliveryUncertain:
            raise
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

    A transport failure, a 5xx or a 401 (the service unable to authenticate the worker) does not
    lose the claim: the renewal is retried every ``retry_interval`` seconds while the claim is
    unexpired, so the agent keeps running through a coordinator, network or GitHub outage, and a
    renewal that succeeds before the expiry continues the claim. The claim is lost when the service refuses the renewal or ``expires_at`` passes
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


# The installation-token permissions a worker acting as the queue App asks for: pushing the submission
# ref (contents, and workflows for a chain whose members touch `.github/workflows`), and reading the
# repository for its access check.
WORKER_APP_PERMISSIONS = {"contents": "write", "workflows": "write", "metadata": "read"}


class WorkerApp:
    """The queue's GitHub App as a co-located repair worker uses it (``TAUCETI_QUEUE_GITHUB_APP``).

    The worker mints its own installation tokens from the App's private key (narrowed to
    ``WORKER_APP_PERMISSIONS``, held in memory only, refreshed before they expire) for its GitHub calls
    and pushes, and authenticates to the queue service with ``github_app.local_worker_bearer``, which
    the service accepts for the worker endpoints only."""

    def __init__(self, credentials, transport=None, *, clock=time.time):
        from tauceti_queue import github_app

        if transport is None:
            from tauceti_queue.github_adapter import UrllibTransport

            transport = UrllibTransport()
        self.credentials = credentials
        self.login = github_app.local_worker_login(credentials)
        self.tokens = github_app.InstallationTokenSource(
            credentials, transport, clock=clock, permissions=WORKER_APP_PERMISSIONS
        )
        self._clock = clock

    @classmethod
    def load(cls, directory) -> WorkerApp:
        from tauceti_queue import github_app

        return cls(github_app.AppCredentials.load(directory, webhook=False))

    def bearer(self) -> str:
        from tauceti_queue import github_app

        return github_app.local_worker_bearer(self.credentials, self._clock())

    def github_token(self) -> str:
        return self.tokens.token()


_worker_app: WorkerApp | None = None


def worker_app() -> WorkerApp | None:
    """The queue App when ``TAUCETI_QUEUE_GITHUB_APP`` names its credential directory, else None."""
    global _worker_app
    directory = os.environ.get("TAUCETI_QUEUE_GITHUB_APP")
    if not directory:
        return None
    if _worker_app is None:
        from tauceti_queue import github_app
        from tauceti_worker.config import Die

        if os.environ.get("TAUCETI_QUEUE_TOKEN_FILE") or os.environ.get("TAUCETI_QUEUE_TOKEN"):
            raise Die("set either TAUCETI_QUEUE_GITHUB_APP or a worker token (TAUCETI_QUEUE_TOKEN_FILE), not both")
        try:
            _worker_app = WorkerApp.load(directory)
        except (github_app.CredentialsError, OSError) as error:
            raise Die(f"cannot load the queue App credentials from {directory}: {error}") from error
        pin_app_credentials()
    return _worker_app


_app_git_config: tuple[tuple[str, str], ...] = ()
# Every git process holding a GitHub credential runs without hooks: a hook in an agent's worktree (its
# own ``.git/hooks``, a ``core.hooksPath`` or a ``core.fsmonitor`` in its config) would otherwise run
# with the token in reach. Command scope wins over the repository's config, and git finds no hook
# under /dev/null.
_NO_GIT_HOOKS = (("core.hooksPath", os.devnull), ("core.fsmonitor", "false"))


def pin_app_credentials() -> None:
    """Make the worker's gh and git use only the App installation token in ``GH_TOKEN``.

    gh reads an empty private config, so it never falls back to the host's gh login. git reads a
    private global config that includes the user's own, and every GitHub credential helper and extra
    header is reset in command scope (``GIT_CONFIG_COUNT``), which git reads after the system, global
    and repository configs, leaving ``gh auth git-credential`` alone: no personal helper answers a
    fetch or push first, no storing helper keeps an installation token, and no Authorization header
    left in a repository's own config (as actions/checkout leaves one) travels with the request."""
    import tempfile

    global _app_git_config
    private = Path(tempfile.mkdtemp(prefix="tauceti-queue-gh-"))
    (private / "gh").mkdir()
    original = os.environ.get("GIT_CONFIG_GLOBAL") or str(Path.home() / ".gitconfig")
    include = f"[include]\n\tpath = {original}\n" if Path(original).is_file() else ""
    gh = shutil.which("gh") or "gh"
    _app_git_config = (
        ("credential.https://github.com.helper", ""),
        ("credential.https://github.com.helper", f"!{shlex.quote(gh)} auth git-credential"),
        ("http.extraHeader", ""),
        ("http.https://github.com/.extraHeader", ""),
    )
    # The same resets in the global config too: agents' isolation replaces the command-scope config,
    # and they must not get the host's own helpers and headers back through the include.
    (private / "gitconfig").write_text(
        include
        + '[credential "https://github.com"]\n\thelper =\n'
        + f"\thelper = !{shlex.quote(gh)} auth git-credential\n"
        + '[http]\n\textraHeader =\n[http "https://github.com/"]\n\textraHeader =\n'
    )
    try:
        count = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        count = 0
    for index, (key, value) in enumerate(_app_git_config, count):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(count + len(_app_git_config))
    os.environ["GH_CONFIG_DIR"] = str(private / "gh")
    os.environ["GIT_CONFIG_GLOBAL"] = str(private / "gitconfig")
    os.environ.pop("GH_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)


def app_git_environment(url: str | None = None) -> dict[str, str]:
    """The ``GIT_CONFIG_*`` variables of a git process that pushes as the App and inherits no other
    command-scope config: the hook reset, and the pins of ``pin_app_credentials`` (none outside App
    mode). With ``url``, also reset the extra headers for exactly that URL, which a repository's
    config for that URL would otherwise win over (git prefers the longest matching URL)."""
    pins = _app_git_config
    if pins and url:
        pins = (*pins, (f"http.{url}.extraHeader", ""))
    pins = (*_NO_GIT_HOOKS, *pins)
    env = {"GIT_CONFIG_COUNT": str(len(pins))}
    for index, (key, value) in enumerate(pins):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def refresh_github_token() -> str | None:
    """Point the worker's own ``gh`` and git calls at a current App installation token (App mode
    only); the reason when one cannot be minted now, else None."""
    app = worker_app()
    if app is None:
        return None
    from tauceti_queue import github_app

    try:
        use_worker_token(app.github_token())
    except (github_app.CredentialsError, OSError, ValueError) as error:
        return f"cannot mint a queue App installation token: {error}"
    return None


def token_source() -> str:
    """Where ``worker_token`` reads the token from (named, never the value): ``TAUCETI_QUEUE_TOKEN_FILE``
    (a mode 0600 file, the service unit's choice), else ``TAUCETI_QUEUE_TOKEN``."""
    if os.environ.get("TAUCETI_QUEUE_GITHUB_APP"):
        return f"the queue App in {os.environ['TAUCETI_QUEUE_GITHUB_APP']}"
    path = os.environ.get("TAUCETI_QUEUE_TOKEN_FILE")
    if path:
        return f"file {path}"
    if os.environ.get("TAUCETI_QUEUE_TOKEN"):
        return "$TAUCETI_QUEUE_TOKEN"
    return "none configured"


def worker_token() -> str:
    """The GitHub token of the worker's dedicated account: the token file, else ``TAUCETI_QUEUE_TOKEN``.

    There is deliberately no fallback to ``GH_TOKEN``, ``GITHUB_TOKEN`` or ``gh auth token``: on a
    shared host those are the operator's personal token, whose quota every agent on the account
    shares, and the worker must never draw on it silently."""
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
    value = (os.environ.get("TAUCETI_QUEUE_TOKEN") or "").strip()
    if value:
        return value
    raise Die(
        "no GitHub credential for the queue repair worker: set TAUCETI_QUEUE_GITHUB_APP to the queue App's"
        " credential directory, or TAUCETI_QUEUE_TOKEN_FILE to a mode 0600 file holding the token of the"
        " worker's dedicated repository-admin account (GH_TOKEN and gh's login are not used)"
    )


def use_worker_token(token: str) -> None:
    """Make the worker's own ``gh`` calls (admin checks, submission pushes) use its dedicated token.

    Agents never inherit it: their environment clears ``GH_TOKEN`` and points gh at an empty config."""
    disable_git_hooks()
    os.environ["GH_TOKEN"] = token
    os.environ.pop("GITHUB_TOKEN", None)


def disable_git_hooks() -> None:
    """Add the hook resets to the worker's own command-scope git config, once: every git process the
    worker runs (checkouts, status, fetches, ref updates, pushes) then holds its token without
    running hooks."""
    try:
        count = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        count = 0
    last = {}
    for index in range(count):
        last[os.environ.get(f"GIT_CONFIG_KEY_{index}", "").lower()] = os.environ.get(f"GIT_CONFIG_VALUE_{index}")
    if all(last.get(key.lower()) == value for key, value in _NO_GIT_HOOKS):
        return
    for index, (key, value) in enumerate(_NO_GIT_HOOKS, count):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(count + len(_NO_GIT_HOOKS))


def verify_admin(repository: str) -> None:
    """The security audit's authorization predicate, applied to the task's repository.

    ``Die`` is the authoritative negative (the API answered and the account is not an admin, or cannot
    see the repository); ``NoProgress`` means the check could not be made (rate limit, 5xx, network).
    """
    from tauceti_worker.config import Die, NoProgress
    from tauceti_worker.security_audit import api

    app = worker_app()
    failure = refresh_github_token()
    if failure:
        raise NoProgress(failure)
    try:
        answer = api(f"repos/{repository}")
    except NoProgress as error:
        if re.search(r"\bHTTP 404\b|\bNot Found\b", str(error)):
            raise Die(f"the queue repair task requires repository-admin permission on {repository}: {error}") from error
        raise
    if app is not None:
        # An installation token has no admin bit on the repository; the App acts on it when the
        # installation covers it (the read above) with contents write. The service trusts the App's
        # local-worker bearer by its key, not by this check.
        if app.tokens.granted_permissions().get("contents") != "write":
            raise Die(f"the queue App's installation token has no contents write on {repository}")
        return
    if answer.get("permissions", {}).get("admin") is not True:
        raise Die(f"the queue repair task requires repository-admin permission on {repository}")


def default_worker_identity() -> str:
    return (
        os.environ.get("TAUCETI_QUEUE_WORKER")
        or f"{os.environ.get('TAUCETI_WORKER_ID', 'default')}@{socket.gethostname()}"
    )


class Stop:
    """The worker's stop latch: set by the signal handlers, waited on by ``serve``.

    Events attached to it (a running task's ``cancel``) are set with it, so a SIGTERM reaches the agent
    through the same path a lost claim does; the task then releases its claim as ``worker-stopped``.
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
    # builds in the foreground, so it gets two hours where the fleet's authoring rounds default to an hour per command.
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
    # Helpers resolving a rebase task's later conflicts on its target meanwhile (``repair_lookahead``,
    # ``TAUCETI_QUEUE_LOOKAHEAD_HELPERS``, default 16, half for background work); 0 resolves every
    # conflict in turn.
    lookahead_helpers: int = dataclasses.field(default_factory=repair_lookahead.helpers_default)
    poll_seconds: float = 60.0
    # A positive admin answer is cached on disk (``admin-check.json``) this long; a check that could not
    # be made is retried after ``admin_retry_seconds``, not on every poll.
    admin_recheck_seconds: float = 6 * 3600.0
    admin_retry_seconds: float = 900.0
    # Run the first whole-library build in the background and launch the agent at its first error
    # (``repair_stream``; ``TAUCETI_QUEUE_STREAM_REPAIR``, ``--no-stream-repair``). Off: the agent runs it.
    stream_repair: bool = True
    # CI's audits and changed-module lint after every green whole-library build the worker runs, and the
    # worker's own verification build before a repair is submitted (``repair_audit``;
    # ``TAUCETI_QUEUE_WORKER_AUDITS``, ``--no-worker-audits``).
    worker_audits: bool = dataclasses.field(default_factory=repair_audit.enabled)
    verify_retries: int = 2  # agent rounds for the findings of a failed verification before release
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
    # Supplied by trusted runner configuration, never inferred from a claim.
    binding: TaskBinding | None = None

    def __post_init__(self):
        if self.binding is not None and not isinstance(self.binding, TaskBinding):
            raise ValueError("runner task binding must come from trusted configuration")

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
        self.progress_outcome: dict | None = None  # Live accounting; never copied into the checkpoint.
        self.record = self.read()
        recorded_task = self.record.get("task") or {}
        if recorded_task and recorded_task.get("claim_token") != task.claim_token:
            # A new claim can continue the retained chain, but the previous
            # worker's in-flight delivery and agent session belong to its lease.
            self.record = {
                "schema": SCHEMA,
                "task": task.to_json(),
                "history": self.record.get("history", []),
                "pending_release": None,
            }
            self.session_path.unlink(missing_ok=True)
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
        self.write_progress()

    def write_progress(self, final=None) -> None:
        from .task_progress import write

        write(self.directory, self.record, final if final is not None else self.progress_outcome)

    @property
    def state(self) -> str | None:
        return self.record.get("state")

    def session(self) -> str | None:
        from tauceti_worker.runtime_status import read_json

        value = read_json(self.session_path).get("session")
        return str(value) if value else None


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
    if failure := refresh_github_token():  # a public origin needs none; a private one does
        log(f"task {task.task_id}: {failure}")
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


def require_task_execution(*, cfg=None, task=None):
    if cfg is not None and task is not None and cfg.binding != task.binding:
        raise TaskBindingMismatch("task differs from the runner's trusted source/execution policy")
    if (cfg is not None and cfg.binding is not None) or (task is not None and task.binding is not None):
        raise NativeTaskExecutionUnavailable("native task source transport and runner adapters are not available")


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
    require_task_execution(task=task)
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


def known_check_failures(task: Task) -> bool:
    """An audit/lint repair already has located findings for its frozen candidate.

    Compiler failures still use discovery to collect independent failures. Predictions,
    generic failed statuses and unlocated check messages are not actionable evidence.
    This selects when inference starts; it never substitutes for final verification.
    """
    if task.kind != "repair":
        return False
    findings = task_findings(task)
    if findings["compiler_errors"]:
        return False

    def located(value):
        return isinstance(value, str) and (
            value.startswith("TauCeti.") or value.startswith("TauCeti/") and value.endswith(".lean")
        )

    return (
        any(located(f.get("path")) and f.get("declaration") for f in findings["lint"]["findings"])
        or any(f.get("declaration") and f.get("modules") for f in findings["duplicate_declarations"])
        or any(located(f.get("module")) and f.get("declaration") for f in findings["import_collisions"])
        or any(
            repair.DIAGNOSTIC_KINDS.get(f.get("kind")) == "audit"
            and located(f.get("module"))
            and f.get("message", "").strip()
            for f in findings.get("other", [])
        )
    )


def scope_only_repair(task: Task) -> bool:
    """A trusted scope-policy finding is not evidence that Lean needs an edit.

    Converted proactive predictions retain the controller's existing refresh
    guard. All other source findings still require a changed repair result.
    """
    if task.kind != "repair" or not isinstance(task.diagnostics, list):
        return False
    findings = []
    for entry in task.diagnostics:
        if not isinstance(entry, dict):
            return False
        if entry.get("kind") == "admission_predictions":
            if entry.get("was_proactive"):
                return False
        else:
            findings.append(entry)
    return bool(findings) and all(d.get("kind") == "status" and d.get("context") == "scope" for d in findings)


def admission_verification(task: Task, tip: str, outcome: dict, stream_findings: list) -> dict | None:
    """Telemetry from scripted checks, never permission to skip the candidate's CI."""
    if not task_findings(task).get("admission_predictions"):
        return None
    observed = set(outcome.get("observed_duplicate_names", []))
    for findings in (task_findings(task), repair.findings_from_diagnostics(stream_findings)):
        observed.update(d["declaration"] for d in findings.get("duplicate_declarations", []))
    checks_passed = any(
        v.get("tip") == tip and v.get("ok") is True and (v.get("checks") or {}).get("status") == "passed"
        for v in outcome.get("verification", [])
    )
    parallel = outcome.get("parallel") or {}
    checks_passed |= parallel.get("verified_tip") == tip and (parallel.get("checks") or {}).get("status") == "passed"
    return {"tip": tip, "checks_passed": checks_passed, "observed_duplicate_names": sorted(observed)}


ARTIFACT_ANCESTORS = 50  # first-parent ancestors of the build revision asked for a published cache mapping


def pre_rebased(task: Task) -> dict | None:
    """``extra.pre_rebased`` of a ``prepare`` task whose chain is an open batch's pre-rebase on the
    head's tip (``base`` and ``onto`` that tip): the batch tip it covers (``source``), the batch's own
    base, and the head's base; None for a preparation on the batch's own base."""
    record = task.extra.get("pre_rebased") if task.kind == "prepare" else None
    return record if isinstance(record, dict) else None


def artifact_revisions(task: Task) -> list[str]:
    """The revisions whose published cache to try, in order. A rebase conflict replays the chain onto
    ``onto`` and builds there, so its artifacts come first and the stale batch base is the fallback;
    a repair builds on its base and falls back to ``onto`` when that differs. A preparation of a
    pre-rebased chain builds on the head's tip, which the public cache never has (the repair cache
    under the head's tip is tried before all of these, ``repair_publish.preferred``), so the head's
    base and then the batch's own base follow it. ``prepare_artifacts`` walks the first one's
    first-parent ancestors (``ARTIFACT_ANCESTORS`` of them) before a fallback off that walk, so the
    artifacts come from the newest revision the cache has."""
    order = [task.onto, task.base] if task.kind in repair.REBASE_KINDS else [task.base, task.onto]
    stacked = pre_rebased(task)
    if stacked is not None:
        order += [str(stacked.get("head_base") or ""), str(stacked.get("batch_base") or "")]
    if task.kind == "pre_rebase" and task.warm_revs:
        # The head's tip and the commits of its chain are never in the public cache: start the walk at
        # the head's base, the last of the controller's warm revisions, which is a `main` commit.
        order = [task.warm_revs[-1], *order]
    revisions: list[str] = []
    for revision in order:
        if revision and revision not in revisions:
            revisions.append(revision)
    return revisions


def toolchain_changed(worktree: Path, base: str, tip: str) -> bool:
    """Whether the chain from ``base`` to ``tip`` changes ``lean-toolchain``.

    Lake scopes a published cache mapping by the toolchain of the checkout that asks for it
    (``.../tc/<toolchain>/<revision>.jsonl``), and the chain's checkout pins the new toolchain, so no
    revision on main can answer, and outputs built by the old toolchain would not be reused anyway.
    A repair of such a chain (a Lean or Mathlib bump) builds cold instead of being released as
    ``artifacts_unavailable`` on every claim until the controller escalates it.
    """
    if not base or not tip or base == tip:
        return False
    return bool(git(worktree, "diff", "--name-only", base, tip, "--", "lean-toolchain", check=False).strip())


def warm_tree(worktree: Path, task: Task, parked_at: str | None, start_tip: str) -> bool:
    """Whether an adopted batch tree (parked at ``parked_at``) is warm for this task: its build and
    dependencies hold for every revision the task builds, i.e. ``lean-toolchain`` and
    ``lake-manifest.json`` are unchanged. A rebase also builds on ``onto``."""
    if parked_at is None:
        return False
    targets = [start_tip, *([task.onto] if task.kind in repair.REBASE_KINDS else [])]
    try:
        return not any(
            git(
                worktree, "diff", "--name-only", parked_at, target, "--", "lean-toolchain", "lake-manifest.json"
            ).strip()
            for target in targets
        )
    except RuntimeError:
        return False


def dependencies_differ(worktree: Path, revision: str) -> bool:
    """Whether the worktree's files change ``lean-toolchain`` or ``lake-manifest.json`` from ``revision``."""
    return bool(
        git(
            worktree, "diff", "--name-only", revision, "--", "lean-toolchain", "lake-manifest.json", check=False
        ).strip()
    )


def parkable(cfg: RunnerConfig, task: Task, artifacts: dict | None) -> bool:
    """Whether the task's tree may be parked as warm: its artifacts were prepared, and its dependency
    files are still those prepared (``onto``'s for a rebase, the starting tip's otherwise). A cold
    tree, or one whose chain changed its dependencies after preparation, is removed instead."""
    if not (artifacts or {}).get("available"):
        return False
    worktree = cfg.root / "worktrees" / f"task-{task.task_id}"
    continuation = task.extra.get("repair_checkpoint") if cfg.worker_audits else None
    prepared = task.onto if task.kind in repair.REBASE_KINDS else (continuation or {}).get("tip") or task.tip
    return worktree.is_dir() and not dependencies_differ(worktree, prepared)


def retire_worktree(cfg: RunnerConfig, task: Task, artifacts: dict | None) -> str:
    """Park the task's tree as its batch's tree when it is ``parkable``, else remove it."""
    from tauceti_queue import repair_cleanup

    return repair_cleanup.retire(
        cfg.clone,
        cfg.root,
        task.batch_id,
        task.task_id,
        cfg.root / "worktrees" / f"task-{task.task_id}",
        keep=parkable(cfg, task, artifacts),
    )


@contextlib.contextmanager
def artifact_checkout(worktree: Path, task: Task, start_tip: str):
    """Prepare changed rebase dependencies on the target, then restore the agent's input chain.

    Lake populates the same private .lake directory for both checkouts. This hydrates
    new dependency Git objects before verification loses network access.
    """
    changed = (
        task.kind in repair.REBASE_KINDS
        and task.onto != start_tip
        and git(
            worktree,
            "diff",
            "--name-only",
            start_tip,
            task.onto,
            "--",
            "lake-manifest.json",
            "lean-toolchain",
            "lakefile.toml",
            "lakefile.lean",
        ).strip()
    )
    if not changed:
        yield
        return
    log(f"task {task.task_id}: preparing dependencies on rebase target {task.onto[:12]}")
    git(worktree, "checkout", "--quiet", "--force", "--detach", task.onto)
    try:
        yield
    finally:
        git(worktree, "checkout", "--quiet", "--force", "--detach", start_tip)


HEAD_KINDS = ("repair", "rebase_conflict")  # the head's own tasks, which warm from the head's tips


def background_task(task: Task) -> bool:
    """Work for a later batch (``prepare``, ``pre_rebase``, or any task the controller marks
    ``background``, such as an open batch's rebase onto ``main``), whose builds yield the CPU to the
    head's (``repair_sandbox.prioritized``). A proactive prediction repair is the head's own: when the
    head's CI fails the controller turns that same task into its required repair."""
    return task.kind in ("prepare", "pre_rebase") or bool(task.extra.get("background"))


def describe_cache_revision(task: Task, artifacts: dict) -> str:
    """The restored revision and its relation to the task, for the worker log: ``85f3eb5579df (the rebase
    target)``, ``5f78b5307b93 (the batch base)``, ``67aa17161ba1 (3 commits behind the rebase target
    85f3eb5579df)``."""
    revision = artifacts["cache_revision"]
    rebase = task.kind in repair.REBASE_KINDS
    stacked = pre_rebased(task) or {}
    if stacked and revision == task.base:
        role = "the head's tip"
    elif stacked and revision == stacked.get("head_base"):
        role = "the head's base"
    elif stacked and revision == stacked.get("batch_base"):
        role = "the batch's own base"
    elif revision == task.base:
        role = "the batch base" if rebase else "the base"
    elif revision == task.onto:
        role = "the rebase target"
    elif task.kind == "pre_rebase" and task.warm_revs and revision == task.warm_revs[-1]:
        role = "the head's base"
    elif task.kind in HEAD_KINDS and revision == task.tip:
        role = "the head's tip"
    elif task.kind in HEAD_KINDS and revision in task.warm_revs:
        # The head's tips come before its base in `warm_revs`, canonical main after it.
        after_base = task.base in task.warm_revs and task.warm_revs.index(revision) > task.warm_revs.index(task.base)
        role = "canonical main" if after_base else "an earlier tip of the head"
    elif revision in task.warm_revs:
        role = "an earlier tip of the head" if task.kind == "pre_rebase" else "an earlier tip of the batch"
    else:
        start = artifact_revisions(task)[0]
        name = (
            "the head's base"
            if task.warm_revs and start == task.warm_revs[-1] and task.kind == "pre_rebase"
            else "the rebase target"
            if rebase
            else "the base"
        )
        target = f"{name} {start[:12]}"
        distance = artifacts.get("cache_distance")
        role = (
            f"{distance} commit{'s' if distance != 1 else ''} behind {target}"
            if distance
            else f"a fallback for {target}"
        )
    return f"{revision[:12]} ({role})"


def artifacts_cold(task: Task, artifacts: dict | None) -> bool:
    """Whether the agent builds without warm artifacts: a rebase conflict whose preparation found no
    cache, or a repair of a chain that changes the toolchain (``toolchain_changed``); any other repair
    without artifacts is released instead, so it never gets this far."""
    if not artifacts:
        return False
    return bool(artifacts.get("cold")) or (task.kind in repair.REBASE_KINDS and not artifacts.get("available", True))


def prompt_context(
    task: Task,
    feedback: str | None,
    findings: dict | None = None,
    *,
    artifacts: dict | None = None,
    listings: Path | None = None,
    shell_limit: int = repair.SHELL_LIMIT_SECONDS,
    worker_verifies: bool = False,
) -> PromptContext:
    """``artifacts`` is the ``prepare_artifacts`` record (the prompt says when the build is cold);
    ``listings`` is where the member listings and the check script go (``repair.listings_path``);
    ``shell_limit`` is the agent's foreground shell limit in seconds, as ``run_repair_agent`` sets it."""
    findings = findings if findings is not None else task_findings(task)
    # Streaming/verification replaces errors, not the retained admission observations.
    knowledge = task_findings(task).get("admission_predictions")
    if knowledge:
        findings.setdefault("admission_predictions", knowledge)
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
                "no_repair": m.no_repair,
                "withdrawn": m.withdrawn,
                "large_refactor": m.large_refactor,
            }
            for m in task.members
        ],
        artifacts_cold=cold,
        artifacts_reason=(artifacts or {}).get("unavailable_reason") if cold else None,
        check_findings_known=known_check_failures(task),
        worker_verifies=worker_verifies and not cold,
        listings_path=listings,
        shell_limit=shell_limit,
        pre_rebased=pre_rebased(task) is not None,
        scope_failure_only=scope_only_repair(task),
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


def last_agent_conclusion(agent_records: list[dict], limit: int = 2000) -> str | None:
    """The last agent attempt's final message (what it concluded about the task), cut to ``limit``."""
    for record in reversed(agent_records or []):
        for attempt in reversed((record or {}).get("attempts") or []):
            text = (attempt or {}).get("final_text")
            if isinstance(text, str) and text.strip():
                return text.strip()[:limit]
    return None


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
    from tauceti_worker.config import environment
    from tauceti_worker.quota import PACE_PROVIDERS, Quota, _unavail_reason, pace_env

    home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    cache = cfg.root / "quota-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with environment(TAUCETI_PACE=cfg.rate, **{pace_env(name): None for name in PACE_PROVIDERS}):
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
# guardian's diagnostic rather than an OSError from Popen (which is ``AgentLaunchUnavailable``).
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
        if rc == 127 and str(line).strip() == "owned tool could not start":
            return LAUNCH_FAILURE
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
    require_task_execution(cfg=cfg, task=task)
    from tauceti_worker.agents import (
        AgentLaunchUnavailable,
        BuildCacheUnavailable,
        run_agent_host,
        take_last_agent_infra_failure,
        take_last_agent_run,
    )
    from tauceti_worker.config import NoProgress, environment
    from tauceti_worker.round_storage import RoundAbandoned

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
                        cache_note=False,
                        host_caches=False,  # the task prepared this tree; no per-round preparation or relinking
                    )
            except AgentLaunchUnavailable as error:
                # Its own outcome, not the cache's: the artifacts were ready and the process never existed,
                # so neither the artifacts backoff nor a retry with the same argv is the right response.
                rc, reason, prework = None, str(error), f"{LAUNCH_FAILURE}: {error}"
            except BuildCacheUnavailable as error:
                return result("artifacts_unavailable", reason=str(error))
            except RoundAbandoned as error:
                # Killed for private build bytes: the agent's own failure, not a reason to try another.
                return result("failed", reason=str(error))
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
                "final_text": (run or {}).get("final_text"),
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


def rewrite_projections(
    cfg: RunnerConfig,
    task: Task,
    worktree: Path,
    findings: dict,
    stream: repair_stream.StreamingBuild | None,
    logdir: Path,
    cancel: threading.Event,
    env_lake: dict,
) -> tuple[dict, dict] | None:
    """Rewrite the numeric projections the findings report, in the members that wrote them
    (``repair_numproj.rewrite_chain``), before an agent sees them. ``stream`` is the task's streaming
    build when its error list counts too: when the lint is among the errors it has reported so far, this
    waits for that build to end, so that its later projections are rewritten too and the rewrite's own
    build can take the build lock. Otherwise the agent starts at the stream's first error as before, and
    projections the stream reports later are rewritten after a failed verification. Returns the findings with those files' errors replaced by
    what a scoped build of them still reports (the error list is updated the same way) and the record
    for the outcome; None when there is nothing to rewrite or the rewrite does not apply, with the
    worktree and findings as they were."""
    directory = repair.listings_path(worktree).parent
    errors = repair_parallel.read_errors(directory) if stream is not None else None
    if not repair_numproj.lint_files(findings, errors):
        return None
    if stream is not None:
        while not stream.finished.wait(0.5):
            if cancel.is_set():
                return None
        errors = repair_parallel.read_errors(directory)
    files = repair_numproj.lint_files(findings, errors)
    tip = git(worktree, "rev-parse", "HEAD").strip()
    never_edited = {m.number for m in task.members if m.no_repair or m.withdrawn}
    frozen = frozenset(c.sha for c in read_chain(cfg.clone, task.validation_base, tip) if c.pr in never_edited)
    started = time.monotonic()
    rewrite = repair_numproj.rewrite_chain(
        worktree,
        task.validation_base,
        files,
        env_lake,
        logdir,
        frozen=frozen,
        timeout=cfg.build_timeout,
        lake=tuple(cfg.lake),
    )
    if rewrite is None:
        log(f"task {task.task_id}: numeric projections in {len(files)} file(s) left to the agent")
        return None
    repair_numproj.replace_stream_errors(directory, rewrite)
    record = {
        "from": tip,
        "tip": rewrite.tip,
        "paths": rewrite.paths,
        "edited": rewrite.edited,
        "errors": len(rewrite.errors),
        "seconds": round(time.monotonic() - started, 1),
        "tool": rewrite.tool,
    }
    log(
        f"task {task.task_id}: rewrote numeric projections in {len(rewrite.paths)} file(s) across "
        f"{len(rewrite.edited)} commit(s); {len(rewrite.errors)} error(s) left in them"
    )
    return repair_numproj.replace_findings(findings, rewrite), record


def findings_left(findings: dict, worktree: Path, streamed: bool) -> bool:
    """Whether anything is left for an agent: findings, or the streaming build's errors."""
    if (
        findings["compiler_errors"]
        or findings["lint"]["findings"]
        or findings["duplicate_declarations"]
        or findings["import_collisions"]
        or findings["failed_modules"]
        or findings.get("other")
    ):
        return True
    return streamed and bool(repair_parallel.read_errors(repair.listings_path(worktree).parent))


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
) -> tuple[dict, dict | None]:
    """The parallel path of a repair (``repair_parallel``): split the task's findings into independent
    groups and, with two or more and an eligible provider, run one agent per group in its own worktree
    and child process, fold their fixups into the chain and run the verification build. It never waits
    for a streaming build: the groups come from the findings the task already has, and the agents start
    at once. Returns the record for the outcome and the findings the sequential path still has to
    repair (None when nothing changes for it)."""
    require_task_execution(cfg=cfg, task=task)
    record: dict = {"source": "diagnostics"}
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


PREPARE_REMAINING_LIMIT = 100


def prepare_remaining_errors(parallel: dict | None, findings: dict, worktree: Path) -> list[dict]:
    """The errors a ``prepare`` task leaves for its batch's later ``pre_rebase`` task (the submission's
    ``remaining_errors`` note): none when the parallel path's verification build passed; otherwise
    the compiler errors handed to the last agent, the streaming build's ``errors.jsonl`` included
    unless the parallel path already narrowed them, since no worker build showed those fixed."""
    status = (parallel or {}).get("status")
    if status == "ok":
        return []
    if status != "partial":
        errors = repair_parallel.read_errors(repair.listings_path(worktree).parent)
        if errors:
            findings = repair_parallel.merge_errors(findings, errors)
    return [
        {
            "path": e.get("path"),
            "line": e.get("line", 0),
            "column": e.get("column", 0),
            "message": str(e.get("message") or "")[:1000],
        }
        for e in findings.get("compiler_errors") or []
    ][:PREPARE_REMAINING_LIMIT]


def _parallel_summary(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("remaining", "report", "attempts")}


def delivery_identity(cfg: RunnerConfig, task: Task, tip: str) -> dict:
    """Only unchanged source and checking policy may reuse completed verification."""
    return {
        "tip": tip,
        "base": task.validation_base,
        "audits": cfg.worker_audits,
        "lake": list(cfg.lake),
        "checker": hashlib.sha256(
            Path(repair_audit.__file__).read_bytes() + Path(repair_sandbox.__file__).read_bytes()
        ).hexdigest(),
    }


def check_submission_access(cfg: RunnerConfig, task: Task, logdir: Path) -> dict:
    """Exercise receive-pack authentication without changing any remote ref."""
    failure = refresh_github_token()
    if failure:
        log(f"task {task.task_id}: {failure}")
        return {"rc": 75, "error": failure}
    remote = cfg.push_remote or f"https://github.com/{task.repository}"
    return repair.run_logged(
        [
            "git",
            "-c",
            "push.followTags=false",
            *(argument for key, value in _NO_GIT_HOOKS for argument in ("-c", f"{key}={value}")),
            "push",
            "--no-verify",
            "--dry-run",
            "--porcelain",
            "--force",
            remote,
            task.tip + ":" + task.submission_ref,
        ],
        cfg.clone,
        logdir / "submission-access.log",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=30,
    )


def push_result(cfg: RunnerConfig, task: Task, worktree: Path, tip: str, logdir: Path, expect: str | None) -> dict:
    """Push the proposed tip to the task's ``submission_ref`` through ``git-safe-push``.

    The ref is ``refs/tauceti-queue/submissions/<task_id>/<claim_id>``, private to this claim; the
    controller fetches it when the result is submitted. ``expect`` is the oid this runner last pushed
    there (ref CAS); the first push is create-only and, when the ref already exists (a previous
    incarnation of this runner under the same claim), is retried once against the observed oid.
    """
    require_task_execution(cfg=cfg, task=task)
    remote = cfg.push_remote or f"https://github.com/{task.repository}"
    failure = refresh_github_token()
    if failure:
        log(f"task {task.task_id}: {failure}")
        return {"rc": 75, "ok": False, "error": failure, "ref": task.submission_ref, "remote": remote}
    env = {
        k: v
        for k, v in os.environ.items()
        if k == "GIT_CONFIG_GLOBAL" or not k.startswith(("TAUCETI_PUSH_", "TAUCETI_CLAIM_", "GIT_CONFIG_"))
    }
    env.update(
        app_git_environment(remote),
        TAUCETI_PUSH_REF=task.submission_ref,
        TAUCETI_PUSH_EXPECT=expect or "",
        TAUCETI_PUSH_REMOTE=remote,
        TAUCETI_PUSH_OID=tip,
        TAUCETI_PUSH_SCOPE=f"queue-task-{task.task_id}",
    )
    record = repair.run_logged([str(GIT_SAFE_PUSH)], worktree, logdir / "push.log", env=env, timeout=600)
    if record["rc"] == 78 and not expect:
        observed = git(worktree, "ls-remote", remote, task.submission_ref, check=False, env=env).split()
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
    from tauceti_queue.repair_cache import before_task, cache_lease, task_cache
    from tauceti_queue.repair_cleanup import worktree_lease

    require_task_execution(cfg=cfg, task=task)
    if background_task(task):
        os.environ[repair_sandbox.BACKGROUND_VARIABLE] = "1"
    else:
        os.environ.pop(repair_sandbox.BACKGROUND_VARIABLE, None)
    cache_root = task_cache(cfg.root, task.task_id)
    cache_cleanup = before_task(cache_root)
    if cache_cleanup["state"] != "busy":
        log(f"repair cache retention: {json.dumps(cache_cleanup, sort_keys=True)}")
    with cache_lease(cache_root), worktree_lease(cfg.root, task.task_id):
        return _run_task(cfg, task, client, resume_state=resume_state, stop=stop)


def no_repair_problem(clone: Path, task: Task, source_pr: str, prefix: str, tip: str) -> str | None:
    """The problem with a resolution ``tip`` of ``source_pr``'s commit on ``prefix`` when the member is
    never edited (``no_repair`` or withdrawn) and the resolution changes the prefix, else None."""
    member = next((m for m in task.members if m.source_pr == source_pr), None)
    if (
        member is not None
        and (member.no_repair or member.withdrawn)
        and chain_tool.tree_of(clone, tip) != chain_tool.tree_of(clone, prefix)
    ):
        return (
            f"no-repair-edited: {source_pr} is never edited: reduce it to an empty commit whose "
            "explanation names the interacting PR, file and finding"
        )
    return None


def resolve_conflicts(
    cfg: RunnerConfig,
    task: Task,
    worktree: Path,
    logdir: Path,
    cancel: threading.Event,
    state: TaskState,
    env_lake: dict,
    *,
    artifacts: dict | None,
    agent_records: list[dict],
) -> dict:
    """Replay a rebase task's chain onto ``onto`` and resolve its conflicts one commit at a time.

    The worker replays ``base..tip`` onto ``onto`` with ``chain.replay``, as the queue does, and stops
    at the first conflicting commit. The agent gets that commit applied onto the clean prefix, with
    git's conflict markers, and nothing after it: no rebase in progress, no step script, no later
    commit. The worker commits the working tree as the resolution (``repair.commit_resolution``), so
    nothing the agent does to HEAD, refs or history counts; checks it; replays the rest of the chain
    onto it; and repeats at the next conflict, on the same claim and worktree. Nothing is submitted
    until the replay reaches the end, so the queue keeps no state between conflicts.

    With ``cfg.lookahead_helpers`` above 0, later conflicting members are resolved meanwhile on ``onto``
    itself by ``repair_lookahead``; at its conflict a member's lookahead resolution is merged onto the
    real prefix and checked like any other, and the agent resolves the member here only when there is
    none, it does not merge cleanly or it is not acceptable.

    Returns ``{"status": "ok", "tip", "agent", "resolved"}`` with the worktree checked out at the whole
    chain, ``{"status": "cancelled"}``, or a release status with its ``reason``: an agent status in
    ``RELEASED_AGENT_STATUSES``, or ``agent_failed`` when a conflict stays unresolved after
    ``cfg.agent_attempts`` attempts."""
    chain_ids: list[str] = []
    resolved: list[dict] = []
    agent: dict = {"status": "ok"}
    lookahead = None

    def replay(base: str, onto: str) -> chain_tool.ChainResult:
        chain_id = f"worker-{task.task_id}-{secrets.token_hex(4)}"
        chain_ids.append(chain_id)
        return chain_tool.replay(cfg.clone, base, task.tip, onto, chain_id=chain_id)

    def accepter(source_pr: str, prefix: str, commit: str):
        def accept(tip: str, report: dict) -> chain_tool.ChainResult | None:
            """The rest of the chain replayed onto the resolved ``tip`` when the resolution is
            acceptable, else None, with the problems added to ``report``."""
            if not report["ok"] or report["unexplained"]:
                return None
            problem = no_repair_problem(cfg.clone, task, source_pr, prefix, tip)
            if problem:
                report["problems"].append(problem)
            rest = replay(commit, tip)
            report["problems"] += absorbed_members(cfg.clone, prefix, commit, tip, task.tip, rest)
            report["ok"] = not report["problems"]
            return rest if report["ok"] else None

        return accept

    try:
        result = replay(task.base, task.onto)
        while result.status != chain_tool.CLEAN:
            conflict = result.conflict
            if conflict is None or conflict.replayed is None:
                raise RuntimeError(f"the replay stopped without a conflicting commit: {result.status}")
            prefix, commit, source_pr = result.tip, conflict.replayed, conflict.source_pr
            if (
                lookahead is None
                and cfg.lookahead_helpers > 0
                and not repair_lookahead.changes_dependencies(cfg.clone, task)
            ):
                lookahead = repair_lookahead.Lookahead(
                    cfg, task, worktree, logdir, cancel, env_lake, artifacts, after=commit
                )
            accept = accepter(source_pr, prefix, commit)
            paths = list(conflict.conflict_paths)
            reuse = None
            stored = stored_resolution(cfg.clone, task.batch_id, commit, prefix)
            if stored is not None:
                # An earlier run of a task of this batch (on any worker sharing the clone) accepted a
                # resolution of this commit on this prefix; it goes through the same checks as a fresh one.
                reuse = ("reused", stored, None)
            else:
                if lookahead is not None:
                    lookahead.wait(commit)
                ahead = stored_resolution(cfg.clone, task.batch_id, commit, task.onto)
                if ahead is not None:
                    # Resolved on `onto` alone by the lookahead: its change merged onto this prefix.
                    reuse = ("lookahead", ahead, task.onto)
                else:
                    # Resolved by an earlier task of the batch on another prefix with the same inputs,
                    # such as a pre-rebase onto an earlier tip of the head.
                    earlier = applicable_resolution(cfg.clone, task.batch_id, commit, prefix)
                    if earlier is not None:
                        reuse = ("reused", earlier, git(cfg.clone, "rev-parse", f"{earlier}^").strip())
            if reuse is not None:
                how, sha, made_on = reuse
                recommitted = repair.recommit_resolution(
                    cfg.clone, sha, prefix, commit, paths, task.large_refactor_heads, made_on=made_on
                )
                if recommitted is not None:
                    tip, report = recommitted
                    rest = accept(tip, report)
                    state.write("conflict_validated", conflict=source_pr, attempt=0, tip=tip, report=report)
                    if rest is not None:
                        log(
                            f"task {task.task_id}: {'reusing the stored' if how == 'reused' else 'using the lookahead'} resolution of {source_pr}'s commit {commit[:12]}"
                        )
                        if made_on is not None and not cancel.is_set():
                            store_resolution(cfg.clone, task.batch_id, commit, prefix, tip)
                        resolved.append({"source_pr": source_pr, "commit": commit, "resolved": tip, how: True})
                        result = rest
                        continue
                if how == "reused":
                    forget_resolutions(cfg.clone, task.batch_id, sha)
                else:
                    log(
                        f"task {task.task_id}: the lookahead resolution of {source_pr} does not compose; resolving it here"
                    )
            entry = {
                "kind": "conflict",
                "source_pr": source_pr,
                "commit": commit,
                "paths": paths,
                "onto": task.onto,
                "message": f"replaying the chain onto {task.onto[:12]} conflicts at {source_pr}",
                "pending": [r.source_pr for r in result.commits if r.status == chain_tool.PENDING],
                "clean_prefix_tip": prefix,
                "landed_on_base": result.landed_on_base,
            }
            outcome = resolve_one(
                cfg,
                task,
                worktree,
                logdir,
                cancel,
                state,
                env_lake,
                prefix=prefix,
                commit=commit,
                entry=entry,
                artifacts=artifacts,
                agent_records=agent_records,
                accept=accept,
                label=f"conflict-{len(resolved) + 1}",
            )
            if outcome["status"] != "ok":
                return {**outcome, "resolved": resolved}
            agent = outcome.get("agent") or agent
            if not cancel.is_set():  # a worker that lost its claim records nothing for the next claimant
                store_resolution(cfg.clone, task.batch_id, commit, prefix, outcome["tip"])
            resolved.append(
                {"source_pr": source_pr, "commit": commit, "resolved": outcome["tip"]}
                | ({"numproj": True} if outcome.get("numproj") else {})
            )
            result = outcome["accepted"]
        git(worktree, "checkout", "--quiet", "--force", "--detach", result.tip)
        return {"status": "ok", "tip": result.tip, "agent": agent, "resolved": resolved}
    finally:
        if lookahead is not None:
            lookahead.close()
            agent_records.extend(lookahead.agent_records)
        for chain_id in chain_ids:
            with contextlib.suppress(Exception):
                chain_tool.delete_chain_ref(cfg.clone, chain_id)


def resolve_one(
    cfg: RunnerConfig,
    task: Task,
    worktree: Path,
    logdir: Path,
    cancel: threading.Event,
    state,
    env_lake: dict,
    *,
    prefix: str,
    commit: str,
    entry: dict,
    artifacts: dict | None,
    agent_records: list[dict],
    accept,
    label: str,
    providers: list[str] | None = None,
) -> dict:
    """Resolve the one conflicting ``commit`` onto ``prefix`` in ``worktree``: the numproj pre-pass, then
    the agent for up to ``cfg.agent_attempts`` attempts. ``accept(tip, report)`` is not None for an
    acceptable resolution (and adds its problems to ``report`` otherwise). ``providers`` is the
    provider chain the worker chose (``repair_lookahead``).

    Returns ``{"status": "ok", "tip", "accepted", "agent"}`` (``numproj`` when the pre-pass resolved
    it), or ``{"status", "reason"}``: an agent status that ends the task, or ``agent_failed``."""
    source_pr = entry["source_pr"]
    # The clean prefix and the one conflicting commit: the commits after it are the worker's to replay.
    chain_before = [*read_chain(cfg.clone, task.onto, prefix), *read_chain(cfg.clone, f"{commit}^", commit)]
    findings = conflict_findings(task, entry, {c.pr for c in chain_before if c.pr is not None})
    ownership = repair_context.compute(
        cfg.clone,
        chain_before,
        task.validation_base,
        worktree,
        declaration_names={m.number: m.declaration_names for m in task.members},
        edited_prs={m.number for m in task.members if m.provenance == "edited"},
    )
    paths = repair.apply_conflict(worktree, prefix, commit)
    if cfg.mathlib_cache and cfg.seed_packages is None and dependencies_differ(worktree, task.onto):
        # A member before or at this conflict changes the dependencies prepared at `onto`; the
        # agent's offline builds need this checkout's. A failed fetch leaves Lake to report it.
        from tauceti_queue.repair_cache import downloads

        with downloads(env_lake) as fetch_env:
            repair.run_logged(
                [*cfg.lake, "exe", "cache", "get"],
                worktree,
                logdir / f"cache-get-mathlib-{commit[:12]}.log",
                fetch_env,
                1800,
            )
    # Projection-only conflicts after `main` renamed numeric projections resolve without the agent
    # (`repair_numproj`); any other outcome leaves the agent the conflict as before, or what is left.
    prepass = (
        repair_numproj.prepass(worktree, prefix, commit, paths, env_lake, logdir, lake=tuple(cfg.lake))
        if (artifacts or {}).get("available")
        else None
    )
    if prepass is not None:
        log(
            f"task {task.task_id}: numproj pre-pass on {source_pr}'s commit {commit[:12]}: "
            f"merged {prepass.merged}, remaining {prepass.remaining}"
        )
    if prepass is not None and prepass.clean:
        repair.listings_path(worktree).parent.mkdir(exist_ok=True)  # excluded from the commit
        (worktree / repair.LISTINGS_DIR / repair.EXPLANATION_FILE).write_text(prepass.note + "\n")
        tip, report = repair.commit_resolution(cfg.clone, worktree, prefix, commit, paths, task.large_refactor_heads)
        accepted = accept(tip, report)
        state.write("conflict_validated", conflict=source_pr, attempt=0, tip=tip, report=report, numproj=True)
        if accepted is not None:
            log(f"task {task.task_id}: resolved {source_pr}'s commit {commit[:12]} by the numproj pre-pass")
            return {"status": "ok", "tip": tip, "accepted": accepted, "numproj": True}
        # Not acceptable after all: the agent gets the conflict exactly as without the pre-pass.
        paths = repair.apply_conflict(worktree, prefix, commit)
        prepass = None
    log(f"task {task.task_id}: resolving {source_pr}'s commit {commit[:12]} onto {prefix[:12]}")
    feedback: str | None = None
    tip, report = prefix, {}
    for attempt in range(1, cfg.agent_attempts + 1):
        if attempt > 1:
            # The agent continues from its own files, on the clean prefix as before.
            git(worktree, "checkout", "--quiet", "--force", "--detach", prefix)
            git(worktree, "read-tree", "-u", "--reset", tip)
        context = prompt_context(
            task,
            feedback,
            findings,
            artifacts=artifacts,
            listings=repair.listings_path(worktree),
            shell_limit=int(cfg.shell_limit),
            worker_verifies=cfg.worker_audits,
        )
        context.repair_context = ownership
        context.prepass = prepass.note if prepass is not None else None
        prompt = repair.compose_prompt(context, findings, chain_before, cfg.clone)
        agent = run_repair_agent(
            cfg,
            task,
            worktree,
            prompt,
            logdir,
            cancel,
            state,
            env_lake,
            label=f"{label}-attempt-{attempt}",
            preselected=providers,
        )
        agent_records.append(agent)
        if agent["status"] == "cancelled" or agent["status"] in RELEASED_AGENT_STATUSES:
            return {"status": agent["status"], "reason": agent.get("reason")}
        tip, report = repair.commit_resolution(cfg.clone, worktree, prefix, commit, paths, task.large_refactor_heads)
        if report["ok"] and report["unexplained"] and not cancel.is_set():
            follow = run_repair_agent(
                cfg,
                task,
                worktree,
                repair.resolution_explanation_prompt(source_pr),
                logdir,
                cancel,
                state,
                env_lake,
                resume=agent.get("session"),
                resume_provider=agent.get("provider"),
                label=f"{label}-attempt-{attempt}-explanation",
                # The resolving session's provider first, so its session is the one continued.
                preselected=providers and [p for p in dict.fromkeys([agent.get("provider"), *providers]) if p],
            )
            agent_records.append(follow)
            if follow["status"] == "cancelled" or follow["status"] in RELEASED_AGENT_STATUSES:
                return {"status": follow["status"], "reason": follow.get("reason")}
            tip, report = repair.commit_resolution(
                cfg.clone, worktree, prefix, commit, paths, task.large_refactor_heads
            )
        accepted = accept(tip, report)
        state.write("conflict_validated", conflict=source_pr, attempt=attempt, tip=tip, report=report)
        if accepted is not None:
            return {"status": "ok", "tip": tip, "accepted": accepted, "agent": agent}
        feedback = "Local validation of the previous attempt's resolution:\n" + format_report(report)
        log(f"task {task.task_id}: the resolution of {source_pr} is not acceptable (attempt {attempt})")
    return {"status": "agent_failed", "reason": f"{source_pr}: the conflict stays unresolved\n{format_report(report)}"}


def resolution_ref(batch_id: str, commit: str, prefix_tree: str) -> str:
    return f"{RESOLUTION_PREFIX}/{batch_id}/{commit}-{prefix_tree}"


def stored_resolution(clone: Path, batch_id: str, commit: str, prefix: str) -> str | None:
    """The resolved commit an earlier run of a task of batch ``batch_id`` accepted for ``commit`` on a
    clean prefix with ``prefix``'s tree (``store_resolution``), or None. Keyed by the tree because the
    replay dates its commits when it runs: a rerun reaches the same prefix tree under another SHA."""
    ref = resolution_ref(batch_id, commit, chain_tool.tree_of(clone, prefix))
    sha = git(clone, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
    return sha or None


def changed_paths(clone: Path, old: str, new: str) -> dict[str, str]:
    """``git diff --raw`` from ``old`` to ``new``: each changed path's line, without rename detection."""
    listing = git(clone, "diff", "--raw", "--no-renames", "-z", old, new, check=False).split("\0")
    return dict(zip(listing[1::2], listing[0::2], strict=False))


def applicable_resolution(clone: Path, batch_id: str, commit: str, prefix: str) -> str | None:
    """A stored resolution of ``commit`` by any task of batch ``batch_id``, on any prefix, of the conflict
    ``commit`` has on ``prefix``, or None. The key's commit fixes the conflicting change and its parent.
    The prefix it was made on is the same as ``prefix`` in the conflicting paths and in every path the
    resolution changes, and cherry-picking ``commit`` onto either conflicts in the same paths and
    otherwise differs only as the two prefixes do (no change of ``commit`` lands elsewhere, as after a
    directory rename), so the conflict it resolved is this one."""
    stored = git(
        clone, "for-each-ref", "--format=%(objectname)", f"{RESOLUTION_PREFIX}/{batch_id}/{commit}-*", check=False
    ).split()
    if not stored:
        return None
    here = chain_tool.merge_trees(clone, prefix, commit, merge_base=f"{commit}^")
    conflicting = set(here.conflict_paths)
    for sha in stored:
        made_on = git(clone, "rev-parse", "--verify", "--quiet", f"{sha}^", check=False).strip()
        if not made_on:
            continue
        there = chain_tool.merge_trees(clone, made_on, commit, merge_base=f"{commit}^")
        differ = changed_paths(clone, made_on, prefix)
        if (
            set(there.conflict_paths) == conflicting
            and not differ.keys() & (conflicting | changed_paths(clone, made_on, sha).keys())
            and differ
            == {p: line for p, line in changed_paths(clone, there.tree, here.tree).items() if p not in conflicting}
        ):
            return sha
    return None


def store_resolution(clone: Path, batch_id: str, commit: str, prefix: str, resolved: str) -> None:
    """Record an accepted resolution in the shared clone, so a rerun of the task after a stop, or a later
    task of the batch, resumes after it (``stored_resolution``, ``applicable_resolution``); the ref also
    keeps its objects. Never fails the task."""
    try:
        git(clone, "update-ref", resolution_ref(batch_id, commit, chain_tool.tree_of(clone, prefix)), resolved)
    except Exception as error:  # noqa: BLE001 - losing the record only costs the resume
        log(f"batch {batch_id}: could not record the resolution of {commit[:12]}: {error}")


def forget_resolutions(clone: Path, batch_id: str, only: str | None = None) -> None:
    """Delete batch ``batch_id``'s stored resolutions (``only``: the entries naming that commit). Never
    fails."""
    try:
        listing = git(
            clone, "for-each-ref", "--format=%(refname) %(objectname)", f"{RESOLUTION_PREFIX}/{batch_id}/", check=False
        )
        for line in listing.splitlines():
            ref, _, sha = line.partition(" ")
            if only is None or sha == only:
                git(clone, "update-ref", "-d", ref, sha, check=False)
    except Exception as error:  # noqa: BLE001
        log(f"batch {batch_id}: could not delete its stored resolutions: {error}")


def conflict_findings(task: Task, entry: dict, prs: set) -> dict:
    """The findings one conflict's prompt shows: the conflict, and a pre-rebase's predicted ripples from
    the head that reference the members in ``prs`` (the clean prefix and the conflicting commit). The
    batch-wide admission predictions and preparation errors, which name later members, are left out:
    the agent resolves this one commit and never sees the commits after it."""
    findings = repair.findings_from_diagnostics([entry])
    findings["admission_predictions"] = None  # not taken from the task by `prompt_context` either
    ripples = task_findings(task).get("cross_batch")
    if ripples:
        removed = []
        for item in ripples.get("removed_used") or []:
            references = [r for r in item.get("references") or [] if r.get("pr") in prs]
            if references:
                removed.append({**item, "references": references})
        added = []
        for item in ripples.get("added_by_both") or []:
            ours = [n for n in item.get("next") or [] if n.get("pr") in prs]
            if ours:
                added.append({**item, "next": ours})
        if removed or added:
            findings["cross_batch"] = {**ripples, "removed_used": removed, "added_by_both": added}
    return findings


def absorbed_members(
    clone: Path, prefix: str, commit: str, resolved: str, tip: str, rest: chain_tool.ChainResult
) -> list[str]:
    """Later members a resolution of ``commit`` took into itself, as problems.

    A later member of the input chain ``commit..tip`` (replayed already in ``rest`` or still pending
    behind a later conflict) is absorbed when it did not land on the base, its change applies onto
    the clean ``prefix`` and changes it, the resolved commit already holds it (applying it there
    changes nothing), and the conflicting commit did not carry it in the input chain. Taking a later
    member's content is replaying it by hand, and would leave that member an empty commit. A member
    that conflicts with the prefix is not judged; content moved in part is not detected mechanically.

    Only a later member that changes a path the resolution changes on the prefix is judged: where the
    prefix and the resolved commit agree on every path a member changes, applying it changes both
    alike, so one cannot hold it while the other does not. (Git's rename detection could carry a
    member's edit to a path the resolution added; like content moved in part, that is not judged.)
    Merging every later member instead took about 20 s per conflict on a 1,014-member batch, about
    half an hour over its 212 conflicts."""
    prefix_tree = chain_tool.tree_of(clone, prefix)
    resolved_tree = chain_tool.tree_of(clone, resolved)
    if resolved_tree == prefix_tree:
        return []  # the resolution leaves the prefix as it is, so applying a member changes both alike
    original_tree = chain_tool.tree_of(clone, commit)
    touched = set(chain_tool.git(clone, "diff", "--name-only", "--no-renames", "-z", prefix, resolved).split("\0"))
    touched.discard("")
    landed = {r.source_pr for r in rest.commits if r.landed_on_base}
    later_commits = chain_tool.chain_commits(clone, commit, tip)
    paths = chain_tool.commit_paths(clone, [c.sha for c in later_commits])
    problems = []
    for later in later_commits:
        if later.source_pr in landed or paths[later.sha].isdisjoint(touched):
            continue  # landed, empty, or applies alike onto the prefix and the resolution
        parent = later.parents[0]
        onto_prefix = chain_tool.merge_trees(clone, prefix, later.sha, merge_base=parent)
        if not onto_prefix.clean or onto_prefix.tree == prefix_tree:
            continue
        onto_resolved = chain_tool.merge_trees(clone, resolved, later.sha, merge_base=parent)
        if not onto_resolved.clean or onto_resolved.tree != resolved_tree:
            continue
        onto_original = chain_tool.merge_trees(clone, commit, later.sha, merge_base=parent)
        if onto_original.clean and onto_original.tree == original_tree:
            continue
        problems.append(
            f"absorbed-later-member: the resolution already contains {later.source_pr}'s change, which the worker "
            "replays after it: keep only the conflicting commit's change"
        )
    return problems


def _run_task(
    cfg: RunnerConfig,
    task: Task,
    client: TaskClient,
    *,
    resume_state: TaskState | None = None,
    stop: Stop | None = None,
) -> dict:
    """Run one claimed task to its outcome. The claim is renewed throughout and the result discarded
    if it is lost. Returns the outcome record (also written to the task's state directory).

    ``stop`` is the worker's stop latch: when it is set while the agent runs, the agent is stopped,
    the claim released (``worker-stopped``) and the worktree removed; nothing of the attempt is
    continued. A stop after validation lets the push and submission finish; they are quick and end
    the task. ``resume_state`` is a recorded task whose validated result ``retry_deliveries`` is
    delivering again.
    """
    require_task_execution(cfg=cfg, task=task)
    state_dir = cfg.tasks_dir / task.task_id
    logdir = state_dir / "logs"
    state = resume_state or TaskState(state_dir, task)
    pending_delivery = state.record.get("delivery") if state.state in IN_FLIGHT_STATES else None
    resuming = resume_state is not None and bool(pending_delivery)
    cancel = threading.Event()
    if stop is not None:
        stop.attach(cancel)
    lease = ClaimLease(client, task, cancel, retry_interval=cfg.lease_retry_interval)
    lease.start()
    worktree = cfg.root / "worktrees" / f"task-{task.task_id}"
    outcome: dict = dict(state.record.get("outcome") or {}) if pending_delivery else {}
    outcome.update(task_id=task.task_id, status="running", worker=cfg.worker)
    state.progress_outcome = outcome
    continuation = task.extra.get("repair_checkpoint") if cfg.worker_audits else None
    start_tip = continuation["tip"] if continuation else task.tip
    # A rebase task's agent step is the worker's own replay, the agent resolving one conflict at a time
    # (`resolve_conflicts`); a retained repair checkpoint is already on the target and is repaired as is.
    rebasing = task.kind in repair.REBASE_KINDS and not continuation
    # A rebased chain that fails verification is repaired like a repair task's (``rebasing`` turns
    # off); ``rebased`` stays, and ``rebased_tip`` is that chain, submitted for CI if the repair fails.
    rebased = rebasing
    rebased_tip: str | None = None
    rebased_report: dict = {}
    # A ``prepare`` task always streams: the streaming build is how it learns whether there is anything
    # to repair at all (the batch has not been tried, so the task carries no diagnostics).
    known_checks = bool(continuation) or known_check_failures(task)
    streaming = (
        task.kind == "prepare" or (cfg.stream_repair and task.kind == "repair" and not known_checks)
    ) and not resuming
    if known_checks:
        outcome["pre_repair_discovery"] = "skipped: task already supplies located audit/lint findings"
    stream: repair_stream.StreamingBuild | None = None
    started = time.monotonic()

    def discard(detail: str) -> dict:
        outcome.update(status="claim_lost", detail=detail)
        state.write("claim_lost", detail=detail, outcome=outcome)
        return outcome

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    background: dict = {}  # the launch record of a publication that owns the worktree once started

    def publish_after(revision: str) -> None:
        """``TAUCETI_REPAIR_PUBLISH=after``: publish ``revision``'s outputs in the background now that the
        result is in; the publisher removes the worktree when done. Never fails the task. Under ``off``
        the record says so and nothing starts."""
        if cfg.publish.when == "before":
            return
        early = repair_publish.early_record(logdir, revision, cfg.publish)
        if early:
            outcome["publish"] = {**early, "early": True}
            return  # frozen outputs do not need the live worktree kept alive
        record = repair_publish.start_background(
            worktree,
            revision,
            settings=cfg.publish,
            env=env_lake,
            logdir=logdir,
            task_id=task.task_id,
            repository=task.repository,
            lake=cfg.lake,
            clone=cfg.clone,
            remove_worktree=not cfg.keep_worktree,
            batch_id=task.batch_id,
            park=parkable(cfg, task, outcome.get("artifacts") or state.record.get("artifacts")),
        )
        outcome["publish"] = record
        if record.get("launched"):
            background.update(record)

    def cancelled(detail: str) -> dict:
        """The agent was cancelled: by the claim being lost (discard), or by the worker stopping. A
        stop abandons the attempt and releases the claim as ``worker-stopped``, so the service
        re-exposes the task at once instead of at the claim's expiry."""
        if not stopping() or lease.lost.is_set():
            return discard(lease.lost_reason or detail)
        phase = state.state
        log(f"task {task.task_id}: stopped while {phase}; abandoning the attempt and releasing the claim")
        return release_task(
            cfg, client, task, state, "worker-stopped", f"worker stopped while {phase}: {detail}", outcome, lease
        )

    def defer_delivery(detail: str) -> dict:
        lease.submitting.clear()
        outcome.update(status="delivery_pending", detail=detail)
        state.write("delivery_pending", detail=detail, outcome=outcome, next_retry_at=time.time() + cfg.poll_seconds)
        log(f"task {task.task_id}: delivery retained for automatic retry: {detail}")
        return outcome

    try:
        state.write("claimed", worker=cfg.worker, service=cfg.service, resumed=resuming)
        if task.repository != cfg.repository:
            verify_admin(task.repository)
        from tauceti_queue.repair_cache import task_cache

        env_lake = repair.lake_env(cfg.root, cache_root=task_cache(cfg.root, task.task_id))
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
            from tauceti_queue import repair_cleanup

            parked_at = repair_cleanup.adopt(cfg.clone, cfg.root, task.batch_id, worktree)
            outcome["sources"]["worktree_reused"] = checkout_worktree(cfg.clone, start_tip, worktree)
            # The batch's tree from an earlier task on this worker: its `.lake` is the warm state, so
            # nothing is fetched and Lake compiles only what changed since.
            warm = warm_tree(worktree, task, parked_at, start_tip)
            outcome["sources"]["batch_tree"] = "warm" if warm else "adopted" if parked_at else "new"
            if cfg.seed_packages:
                repair.link_shared_packages(worktree, cfg.seed_packages)
            state.write("sources_ready", worktree=str(worktree), sources=outcome["sources"])
            revisions = artifact_revisions(task)
            with artifact_checkout(worktree, task, start_tip):
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
                    warm=repair_publish.warm(cfg.publish, task),
                    build=not streaming and not known_checks and task.kind not in repair.REBASE_KINDS,
                    fetch=not warm,
                )
            if artifacts.get("warm_attempts"):
                warmed = artifacts.get("warmed_from") or []
                missing = [a["revision"][:12] for a in artifacts["warm_attempts"] if not a["mapping_found"]]
                log(
                    f"task {task.task_id}: repair outputs warmed the tree from "
                    + (", ".join(r[:12] for r in warmed) if warmed else "no revision")
                    + (f"; no published mapping for {', '.join(missing)}" if missing else "")
                )
            if artifacts["cache_revision"]:
                skipped = "; ".join(artifacts.get("cache_skipped") or ())
                log(
                    f"task {task.task_id}: warm artifacts from {describe_cache_revision(task, artifacts)}"
                    + (f"; skipped: {skipped}" if skipped else "")
                )
            if not artifacts["available"] and (
                task.kind in repair.REBASE_KINDS
                # A build-failure repair only: a preparation discovers its failures with the warm build
                # (a repair works from the task's diagnostics, without the stream), and audit/lint
                # findings are verified by the worker, which a cold tree cannot do.
                or (task.kind == "repair" and not known_checks and toolchain_changed(worktree, revisions[0], start_tip))
            ):
                artifacts["cold"] = True
            outcome["artifacts"] = {k: v for k, v in artifacts.items() if k != "incremental_build"} | {
                "incremental_build": {
                    k: v for k, v in (artifacts.get("incremental_build") or {}).items() if "load" not in k
                }
            }
            state.write("artifacts_ready" if artifacts["available"] else "artifacts_unavailable", artifacts=artifacts)
            if known_checks:
                log(f"task {task.task_id}: using the task's audit/lint findings; verification follows the repair")
            if stopping() and not lease.lost.is_set():
                return cancelled("the worker stopped while the artifacts were prepared")
            if not artifacts["available"]:
                if not artifacts.get("cold"):
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
                # service's CI. A chain that changes `lean-toolchain` can never be warmed from main
                # (`toolchain_changed`). Either way the agent is run rather than the task released.
                log(
                    f"task {task.task_id}: no warm artifacts ({artifacts['unavailable_reason']}); "
                    f"running the {'rebase' if task.kind in repair.REBASE_KINDS else 'repair'} without them"
                )
        elif not worktree.is_dir():
            return release_task(
                cfg, client, task, state, "agent_failed", "delivery retry: the worktree is gone", outcome, lease
            )
        if not resuming:
            access = check_submission_access(cfg, task, logdir)
            if access["rc"]:
                return release_task(
                    cfg,
                    client,
                    task,
                    state,
                    "submission_unavailable",
                    "submission authentication/preflight failed; see submission-access.log",
                    outcome,
                    lease,
                )
        chain_before = read_chain(cfg.clone, task.base, task.tip)
        prompt_chain = read_chain(cfg.clone, task.validation_base, start_tip) if continuation else chain_before

        def compute_ownership(chain: list) -> repair_context.RepairContext:
            return repair_context.compute(
                cfg.clone,
                chain,
                task.validation_base,
                worktree,
                declaration_names={m.number: m.declaration_names for m in task.members},
                edited_prs={m.number for m in task.members if m.provenance == "edited"},
            )

        # A rebase task computes the context of each conflict's own commits (`resolve_conflicts`).
        ownership = None if rebasing else compute_ownership(chain_before)
        if ownership is not None:
            outcome["repair_context"] = ownership.summary()
        tools_root = cfg.root / repair_audit.TOOLS_DIR
        repair_publish.write_early_settings(
            worktree,
            settings=cfg.publish,
            logdir=logdir,
            task_id=task.task_id,
            repository=task.repository,
            lake=tuple(cfg.lake),
        )
        if cfg.worker_audits:
            repair_audit.write_settings(
                worktree,
                base=task.validation_base,
                tools_root=tools_root,
                lake=tuple(cfg.lake),
                logdir=logdir,
            )
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
                audit=functools.partial(
                    repair_audit.check,
                    worktree,
                    env_lake,
                    base=task.validation_base,
                    tools_root=tools_root,
                    logdir=logdir,
                    lake=tuple(cfg.lake),
                    timeout=cfg.build_timeout,
                )
                if cfg.worker_audits
                else None,
            )
            if not stream.wait_for_agent(cancel):
                return cancelled("cancelled while the build streamed")
            outcome["stream_build"] = stream.summary()
            if (
                task.kind == "prepare"
                and stream.finished.is_set()
                and stream.rc == 0
                and not stream.errors
                and not stream.findings
            ):
                # The batch builds on its base as assembled: give the task back as prepared, without an
                # agent, and publish the outputs for the head's later rounds.
                if cfg.publish.when == "before":
                    outcome["publish"] = repair_publish.publish_outputs(
                        worktree,
                        task.tip,
                        settings=cfg.publish,
                        env=env_lake,
                        logdir=logdir,
                        repository=task.repository,
                        lake=cfg.lake,
                    )
                where = "the head's tip" if pre_rebased(task) is not None else "its base"
                log(f"task {task.task_id}: the open batch builds on {where}; nothing to prepare")
                released = release_task(
                    cfg,
                    client,
                    task,
                    state,
                    "prepared_clean",
                    f"the batch built on {where} without errors",
                    outcome,
                    lease,
                )
                publish_after(task.tip)
                return released
        findings = (
            repair.findings_from_diagnostics(continuation["diagnostics"]) if continuation else task_findings(task)
        )
        feedback: str | None = (
            f"The checkout starts at retained repair `{start_tip[:12]}` on the required base. "
            "That chain compiled. Keep its repairs and fix the audit or lint findings listed here."
            if continuation
            else None
        )
        pushed: str | None = state.record.get("pushed_tip") if pending_delivery else None
        attempt = 0
        validation_retries = 0
        verify_rounds = 0
        explanation_resubmits = 0
        resubmit = False
        agent_records: list[dict] = list(outcome.get("agent", []))
        agent = {"status": "ok"}
        skip_agent = bool(pending_delivery)
        # Numeric projections are rewritten mechanically before an agent sees them: the first findings
        # (the stream's too) and those of each failed verification (`rewrite_projections`).
        projections_due = (
            task.kind in ("repair", "prepare")
            and not resuming
            and not continuation
            and not (outcome.get("artifacts") or {}).get("cold")
        )
        while True:
            attempt += 1
            if cancel.is_set() or stopping():
                return cancelled("cancelled before the agent started")
            if projections_due:
                projections_due = False
                rewritten = rewrite_projections(
                    cfg, task, worktree, findings, stream if attempt == 1 else None, logdir, cancel, env_lake
                )
                if cancel.is_set():
                    return cancelled("cancelled while numeric projections were rewritten")
                if rewritten is not None:
                    findings, record = rewritten
                    outcome.setdefault("numproj", []).append(record)
                    chain_before = prompt_chain = read_chain(cfg.clone, task.validation_base, record["tip"])
                    ownership = compute_ownership(chain_before)
                    outcome["repair_context"] = ownership.summary()
                    if feedback:  # a verification's: the findings it lists are superseded
                        feedback = (
                            "The worker's verification of the chain failed. The worker then rewrote the numeric "
                            "projections in "
                            + ", ".join(f"`{p}`" for p in record["paths"])
                            + " with `tauceti-tools numproj`, in the members that wrote them, and rebuilt those files. "
                            "What is left is listed as diagnostics and in "
                            f"`{repair.LISTINGS_DIR}/{repair_audit.ERRORS_FILE}`."
                        )
                    if not findings_left(findings, worktree, stream is not None and attempt == 1):
                        log(f"task {task.task_id}: nothing left for the agent after the numeric projection rewrite")
                        skip_agent = True
            if (
                attempt == 1
                and not resuming
                and not continuation
                and task.kind in ("repair", "prepare")
                and cfg.parallel_agents > 1
                and not (outcome.get("artifacts") or {}).get("cold")
            ):
                # Independent failing groups are repaired by concurrent agents first; what they leave
                # (nothing, a group that failed, a build that still fails) goes to the usual agent below.
                parallel, remaining = run_parallel_groups(
                    cfg, task, worktree, chain_before, findings, ownership, logdir, cancel, state, env_lake
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
            observed = set(outcome.get("observed_duplicate_names", []))
            observed.update(d["declaration"] for d in findings.get("duplicate_declarations", []))
            outcome["observed_duplicate_names"] = sorted(observed)
            if resubmit:
                resubmit = False  # the explanation turn already ran; check and submit its result
            elif skip_agent:
                skip_agent = False  # every group folded and the verification build passed; check and submit
            elif rebasing:
                pending_delivery = None
                state.record.pop("delivery", None)
                outcome.pop("repair_checkpoint", None)
                resolution = resolve_conflicts(
                    cfg,
                    task,
                    worktree,
                    logdir,
                    cancel,
                    state,
                    env_lake,
                    artifacts=outcome.get("artifacts") or state.record.get("artifacts"),
                    agent_records=agent_records,
                )
                outcome["agent"] = agent_records
                outcome["conflicts"] = resolution["resolved"]
                if resolution["status"] == "cancelled":
                    return cancelled("the agent was cancelled")
                if resolution["status"] != "ok":
                    return release_task(
                        cfg, client, task, state, resolution["status"], resolution.get("reason"), outcome, lease
                    )
                agent = resolution["agent"]
            else:
                pending_delivery = None
                state.record.pop("delivery", None)
                outcome.pop("repair_checkpoint", None)
                context = prompt_context(
                    task,
                    feedback,
                    findings,
                    artifacts=outcome.get("artifacts") or state.record.get("artifacts"),
                    listings=repair.listings_path(worktree),
                    shell_limit=int(cfg.shell_limit),
                    worker_verifies=cfg.worker_audits,
                )
                context.repair_context = ownership
                if continuation or rebased:
                    context.kind = "repair"
                    context.check_findings_known = True
                if context.worker_verifies:
                    from tauceti_queue import repair_targeted

                    repair_targeted.write(worktree / repair.LISTINGS_DIR, candidate=start_tip, findings=findings)
                prompt = repair.compose_prompt(context, findings, prompt_chain, cfg.clone)
                if stream is not None:
                    prompt = stream.annotate(prompt, worker_verifies=context.worker_verifies)
                agent = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    prompt,
                    logdir,
                    cancel,
                    state,
                    env_lake,
                    label=f"attempt-{attempt}",
                )
                agent_records.append(agent)
                outcome["agent"] = agent_records
                if agent["status"] == "cancelled":
                    return cancelled("the agent was cancelled")
                if agent["status"] in RELEASED_AGENT_STATUSES:
                    return release_task(cfg, client, task, state, agent["status"], agent.get("reason"), outcome, lease)
            tip = git(worktree, "rev-parse", "HEAD").strip()
            git(cfg.clone, "update-ref", f"{RESULT_REF_PREFIX}/{task.task_id}/attempt-{attempt}", tip)
            report = repair.chain_report(
                cfg.clone,
                task.validation_base,
                task.tip,
                tip,
                task.member_keys,
                prior_base=task.base,
                large_refactors=task.large_refactor_heads,
            )
            if report["ok"] and report["unexplained"] and not cancel.is_set() and not rebasing:
                follow = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    repair.explanation_prompt(
                        report["unexplained"],
                        repair.read_chain(cfg.clone, task.validation_base, tip),
                        base=task.validation_base,
                    ),
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
                    cfg.clone,
                    task.validation_base,
                    task.tip,
                    tip,
                    task.member_keys,
                    prior_base=task.base,
                    large_refactors=task.large_refactor_heads,
                )
            report["dirty"] = bool(git(worktree, "status", "--porcelain", "--untracked-files=no").strip())
            report["unchanged"] = tip == task.tip
            report["agent_failed"] = agent["status"] == "failed"
            state.write("validated", attempt=attempt, tip=tip, report=report)
            proactive = bool((task_findings(task).get("admission_predictions") or {}).get("proactive"))
            acceptable = (
                report["ok"]
                and not report["dirty"]
                and (not report["unchanged"] or proactive or scope_only_repair(task))
            )
            if not acceptable:
                detail = format_report(report)
                if agent.get("timed_out"):
                    detail = f"agent exceeded the worker's {cfg.agent_timeout:.0f}s cap\n{detail}"
                elif agent["status"] == "failed":
                    detail = f"agent exited {agent.get('rc')}\n{detail}"
                if attempt < cfg.agent_attempts and not rebasing:
                    feedback = (
                        "Local validation of the previous attempt's chain:\n"
                        + detail
                        + f"\n\nThe chain the task started from is retained at `{RESULT_REF_PREFIX}/{task.task_id}/input` "
                        f"(tip `{task.tip[:12]}`, on base `{task.validation_base[:12]}`)."
                    )
                    log(f"attempt {attempt} not acceptable; retrying once with the report")
                    continue
                if rebased_tip is not None:
                    log(f"task {task.task_id}: the repair of the rebased chain failed; submitting that chain for CI")
                    git(worktree, "checkout", "--quiet", "--force", "--detach", rebased_tip)
                    tip, report = rebased_tip, rebased_report
                else:
                    if rebased and not cancel.is_set():
                        # A rerun would rebuild this chain from the stored resolutions without the agent. A
                        # worker that lost its claim leaves them to the claimant.
                        forget_resolutions(cfg.clone, task.batch_id)
                    return release_task(cfg, client, task, state, "agent_failed", detail, outcome, lease)
            parallel = outcome.get("parallel") or {}
            verified = (
                (parallel.get("verified_tip") == tip and "checks" in parallel)
                or pending_delivery == delivery_identity(cfg, task, tip)
                or tip == rebased_tip
            )
            artifacts_cold = bool((outcome.get("artifacts") or state.record.get("artifacts") or {}).get("cold"))
            if cfg.worker_audits and (not artifacts_cold or continuation) and not verified:
                outcome.pop("repair_checkpoint", None)
                # Compile the final chain, capture its native output map and run every check.
                state.write("verifying", attempt=attempt, tip=tip)
                verification = repair_audit.verify(
                    worktree,
                    env_lake,
                    base=task.validation_base,
                    tools_root=tools_root,
                    logdir=logdir,
                    lake=tuple(cfg.lake),
                    timeout=cfg.build_timeout,
                    audits=True,
                )
                outcome.setdefault("verification", []).append(
                    {k: v for k, v in verification.items() if k != "diagnostics"}
                    | {"tip": tip, "findings": len(verification["diagnostics"])}
                )
                observed = set(outcome.get("observed_duplicate_names", []))
                observed.update(
                    d["declaration"]
                    for d in repair.findings_from_diagnostics(verification["diagnostics"]).get(
                        "duplicate_declarations", []
                    )
                )
                outcome["observed_duplicate_names"] = sorted(observed)
                checks = verification.get("checks") or {}
                if checks.get("status") == "unavailable":
                    log(f"task {task.task_id}: audits unavailable ({checks.get('reason')}); CI runs them")
                    if continuation:
                        return release_task(
                            cfg, client, task, state, "artifacts_unavailable", checks.get("reason"), outcome, lease
                        )
                if not verification["ok"]:
                    detail = "The worker's verification of the chain failed:\n" + repair_audit.format_findings(
                        verification["diagnostics"]
                    )
                    state.write("verification_failed", attempt=attempt, tip=tip, detail=detail)
                    if verify_rounds < cfg.verify_retries:
                        if rebasing:
                            # The agent resolved conflicts and never saw the whole chain: repair the chain
                            # on its target as a repair task would.
                            log(f"task {task.task_id}: the rebased chain fails verification; repairing it")
                            rebasing, rebased_tip, rebased_report = False, tip, report
                            chain_before = prompt_chain = read_chain(cfg.clone, task.validation_base, tip)
                            ownership = compute_ownership(chain_before)
                        verify_rounds += 1
                        findings = repair.findings_from_diagnostics(verification["diagnostics"])
                        projections_due = not continuation
                        feedback = (
                            detail
                            + "\n\nThe findings are listed above as diagnostics and appended to "
                            + f"`{repair.LISTINGS_DIR}/{repair_audit.ERRORS_FILE}`."
                        )
                        log(f"attempt {attempt}: verification failed; running the agent on its findings")
                        continue
                    elif rebased:
                        # The retries are spent: the head's CI and repair tasks take the build from here.
                        log(f"task {task.task_id}: the repaired chain still fails verification; submitting it for CI")
                    elif not (
                        task.extra.get("can_checkpoint_repair")
                        and verification.get("build", {}).get("rc") == 0
                        and checks.get("status") == "failed"
                        and verification["diagnostics"]
                        and all(d.get("kind") in ("audit", "lint") for d in verification["diagnostics"])
                    ):
                        return release_task(cfg, client, task, state, "agent_failed", detail, outcome, lease)
                    else:
                        outcome["repair_checkpoint"] = {
                            "tip": tip,
                            "diagnostics": [
                                {**finding, "message": finding["message"][:2000]}
                                for finding in verification["diagnostics"][:20]
                            ],
                            "tools": checks.get("tools_commit") or checks.get("tools"),
                            # The code that produced it, kept if a later process delivers it.
                            "worker_revision": STARTUP_REVISION,
                        }
            if lease.lost.is_set():
                return discard(lease.lost_reason or "claim lost before the push")
            if cfg.publish.when == "before" and (outcome.get("publish") or {}).get("revision") != tip:
                outcome["publish"] = repair_publish.publish_outputs(
                    worktree,
                    tip,
                    settings=cfg.publish,
                    env=env_lake,
                    logdir=logdir,
                    repository=task.repository,
                    lake=cfg.lake,
                )
            state.write(
                "delivery_pending", delivery=delivery_identity(cfg, task, tip), outcome=outcome, next_retry_at=0
            )
            push = push_result(cfg, task, worktree, tip, logdir, pushed)
            outcome["push"] = {k: v for k, v in push.items() if "load" not in k}
            if not push["ok"]:
                return defer_delivery(f"git-safe-push exited {push['rc']}")
            pushed = tip
            state.write("pushed", tip=tip, pushed_tip=tip, pushed_ref=push["ref"], outcome=outcome)
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
            verification_note = admission_verification(
                task, tip, outcome, stream.findings if stream is not None else []
            )
            if verification_note is not None:
                notes["admission_verification"] = verification_note
            if task.kind == "pre_rebase":
                for result in reversed(outcome.get("verification", [])):
                    checks = result.get("checks") or {}
                    if result.get("tip") == tip and result.get("ok") is True and checks.get("status") == "passed":
                        scope = (checks.get("lint") or {}).get("scope")
                        if scope == "full":
                            notes["pre_rebase_verification"] = {
                                "tip": tip,
                                "base": task.validation_base,
                                "tools": checks.get("tools"),
                                "scope": scope,
                            }
                        break
            if task.kind == "prepare":
                notes["remaining_errors"] = prepare_remaining_errors(outcome.get("parallel"), findings, worktree)
            checkpoint = outcome.get("repair_checkpoint") or {}
            saving_checkpoint = checkpoint.get("tip") == tip
            submit_options = {"checkpoint": True} if saving_checkpoint else {}
            if saving_checkpoint:
                notes["repair_checkpoint"] = checkpoint["diagnostics"]
                # What the queue's stall detection reads (docs/queue/escalation.md, "Stalled repair"): the
                # code that produced the checkpoint and the agent's own conclusion about it.
                notes["worker_revision"] = checkpoint.get("worker_revision") or STARTUP_REVISION
                if checkpoint.get("tools"):
                    notes["tools_revision"] = checkpoint["tools"]
                conclusion = last_agent_conclusion(agent_records)
                if conclusion:
                    notes["agent_conclusion"] = conclusion
            lease.submitting.set()
            try:
                response = with_retries(
                    functools.partial(client.submit, task, tip, push["ref"], notes, **submit_options),
                    attempts=cfg.service_attempts,
                    delays=cfg.service_delays,
                    what="result",
                )
            except EndpointMissing as error:
                return release_task(cfg, client, task, state, "agent_failed", str(error), outcome, lease)
            except ClaimRefused as error:
                return discard(f"result refused: {error}")
            except ServiceError as error:
                return defer_delivery(f"result response unavailable: {error}")
            state.write("submitted", attempt=attempt, response=response)
            outcome["response"] = response
            reason = str(response.get("reason") or "")
            if rebased and reason not in CLAIM_CODES:
                # The service has the chain: the task is answered. A refused claim's successor may be
                # using the batch's resolutions.
                forget_resolutions(cfg.clone, task.batch_id)
            if reason == REPAIR_CHECKPOINT:
                lease.stop()
                outcome.update(status="released", reason=REPAIR_CHECKPOINT, tip=tip, pushed_ref=push["ref"])
                state.write("released", outcome=outcome)
                publish_after(tip)
                return outcome
            if response.get("accepted"):
                lease.stop()
                outcome.update(status="adopted", tip=tip, pushed_ref=push["ref"], version=response.get("version"))
                state.write("adopted", outcome=outcome)
                publish_after(tip)
                return outcome
            if reason in CLAIM_CODES:
                return discard(f"result rejected: {reason}")
            lease.submitting.clear()
            if reason == "explanation-required" and explanation_resubmits < cfg.validation_retries and not rebasing:
                # The controller asks the same claim to add the explanation and resubmit.
                explanation_resubmits += 1
                named = rejected_members(response) or report.get("edited") or task.member_keys
                follow = run_repair_agent(
                    cfg,
                    task,
                    worktree,
                    repair.explanation_prompt(
                        named, repair.read_chain(cfg.clone, task.validation_base, tip), base=task.validation_base
                    ),
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
            if (
                reason in ("invalid-chain", "no-repair-edited")
                and validation_retries < cfg.validation_retries
                and not rebasing
            ):
                validation_retries += 1
                feedback = "The queue's validation of the previous attempt's chain:\n" + format_service_validation(
                    response
                )
                log(f"chain not adopted ({reason}); retrying with the validation report")
                continue
            outcome.update(status="not_adopted", tip=tip, pushed_ref=push["ref"], reason=reason)
            state.write("not_adopted", outcome=outcome)
            publish_after(tip)
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
        # A task awaiting delivery keeps its worktree for the retry. A background publication owns it
        # from its start and parks or removes it when done.
        if not cfg.keep_worktree and outcome.get("status") not in ("error", "delivery_pending") and not background:
            with contextlib.suppress(Exception):
                outcome["worktree"] = retire_worktree(
                    cfg, task, outcome.get("artifacts") or state.record.get("artifacts")
                )
        with contextlib.suppress(Exception):
            repair.write_record(state_dir / "outcome.json", outcome)
        state.write_progress(outcome)
    return outcome


# ----------------------------------------------------------------------------------------------
# Loop


ADMIN_CHECK_FILE = "admin-check.json"


def admin_check_path(cfg: RunnerConfig) -> Path:
    return cfg.root / ADMIN_CHECK_FILE


def read_admin_check(cfg: RunnerConfig) -> dict:
    """The cached positive verification for ``cfg.repository`` (``login``, ``repository``,
    ``verified_at``), or ``{}`` when there is none."""
    from tauceti_worker.runtime_status import read_json

    if cfg.binding is not None:
        return {}
    record = read_json(admin_check_path(cfg))
    if record.get("repository") != cfg.repository or not isinstance(record.get("verified_at"), (int, float)):
        return {}
    return record


def describe_age(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{seconds / 60:.0f}m"


class AdminCheck:
    """Caches the admin verification for ``admin_recheck_seconds`` to spare the shared API quota.

    The positive answer is kept in ``<root>/admin-check.json`` so a restart within the window does not
    check again. A check that cannot be made (``NoProgress``: rate limit, 5xx, network) is not a refusal:
    the worker keeps the cached answer, or with none claims anyway and leaves the decision to the
    service, which enforces admin on every claim; it retries after ``admin_retry_seconds``. Only an
    authoritative negative (``Die``) stops the claim, and it drops the cached answer.
    """

    def __init__(self, cfg: RunnerConfig):
        self.cfg = cfg
        cached = read_admin_check(cfg)
        self.verified_at: float | None = float(cached["verified_at"]) if cached else None
        self.login: str | None = cached.get("login") if cached else None
        self.retry_at = 0.0

    def fresh(self, now: float) -> bool:
        return self.verified_at is not None and now - self.verified_at <= self.cfg.admin_recheck_seconds

    def ensure(self) -> None:
        from tauceti_worker.config import Die, NoProgress
        from tauceti_worker.runtime_status import atomic_json

        require_task_execution(cfg=self.cfg)
        now = time.time()
        if self.fresh(now) or now < self.retry_at:
            return
        try:
            verify_admin(self.cfg.repository)
        except Die:
            self.verified_at = self.login = None
            admin_check_path(self.cfg).unlink(missing_ok=True)
            raise
        except (NoProgress, OSError) as error:
            self.retry_at = now + self.cfg.admin_retry_seconds
            if self.verified_at is not None:
                kept = f"keeping the verification from {describe_age(now - self.verified_at)} ago"
            else:
                kept = "no cached verification; claiming anyway, the service enforces admin on every claim"
            log(
                f"authorization: local admin check skipped ({type(error).__name__}: {error}); {kept}; "
                f"next check in {describe_age(self.cfg.admin_retry_seconds)}"
            )
            return
        self.login = admin_login()
        self.verified_at = time.time()
        record = {"login": self.login, "repository": self.cfg.repository, "verified_at": self.verified_at}
        try:
            atomic_json(admin_check_path(self.cfg), record)
        except OSError as error:
            log(f"authorization: could not record the admin verification: {error}")


def admin_login() -> str | None:
    """The account's login, for the record; None when GitHub does not answer."""
    from tauceti_worker.config import NoProgress
    from tauceti_worker.security_audit import api

    app = worker_app()
    if app is not None:
        return app.login
    try:
        return str(api("user").get("login") or "") or None
    except (NoProgress, OSError, AttributeError):
        return None


def make_client(cfg: RunnerConfig) -> TaskClient:
    if cfg.binding is not None:
        if not cfg.token:
            raise TaskBindingMismatch("an explicit native queue bearer is required; GitHub fallback is unavailable")
        return TaskClient(cfg.service, cfg.token, cfg.worker, binding=cfg.binding)
    app = None if cfg.token else worker_app()
    if app is not None:
        return TaskClient(cfg.service, app.bearer, cfg.worker)
    token = cfg.token or worker_token()
    use_worker_token(token)
    return TaskClient(cfg.service, token, cfg.worker)


def claim_and_run(
    cfg: RunnerConfig, client: TaskClient, admin: AdminCheck | None = None, *, stop: Stop | None = None
) -> dict | None:
    """Verify the account, claim one task and run it. None when nothing was claimable."""
    require_task_execution(cfg=cfg)
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


def retry_deliveries(cfg: RunnerConfig, client: TaskClient, *, stop: Stop | None = None) -> list[dict]:
    """Deliver validated results a previous run could not, and release every other unfinished claim.

    Tasks are all-or-nothing: one a previous runner left mid-attempt (stopped, killed or crashed) is
    abandoned, its claim released (``worker-stopped``) and its worktree removed. Only a result that
    was validated and is being pushed or submitted is delivered again, while the claim is still live;
    if the service refuses the claim the task is recorded as lost.
    """
    outcomes = []
    for directory, record in in_flight_checkpoints(cfg):
        if stop is not None and stop.is_set():
            break
        try:
            task = Task.from_json(record["task"])
            require_task_execution(cfg=cfg, task=task)
        except NativeTaskExecutionUnavailable as error:
            log(f"{directory.name}: native checkpoint cannot run ({error})")
            continue
        except (ValueError, KeyError) as error:
            log(f"{directory.name}: unreadable checkpoint ({error})")
            continue
        state = TaskState(directory, task)
        try:
            task.expires_at = client.renew(task)
        except ClaimRefused as error:
            # Submission may have committed before its response was lost. Ask for
            # the exact delivery's receipt before treating a released claim as loss.
            delivery = record.get("delivery") or {}
            if record.get("pushed_tip") and delivery.get("tip") == record["pushed_tip"]:
                checkpoint = (record.get("outcome") or {}).get("repair_checkpoint") or {}
                submit_options = {"checkpoint": True} if checkpoint.get("tip") == record["pushed_tip"] else {}
                try:
                    response = client.submit(task, record["pushed_tip"], task.submission_ref, {}, **submit_options)
                except (ClaimRefused, ServiceError):
                    continue
                except EndpointMissing:
                    response = {}
                saved = response.get("reason") == REPAIR_CHECKPOINT
                if response.get("accepted") or saved:
                    status = "released" if saved else "adopted"
                    outcome = dict(
                        record.get("outcome") or {},
                        status=status,
                        tip=record["pushed_tip"],
                        response=response,
                        version=response.get("version"),
                    )
                    if saved:
                        outcome["reason"] = REPAIR_CHECKPOINT
                    # A lost reply must not skip publication's submission-time
                    # fallback. Freeze any surviving outputs before terminal cleanup.
                    from tauceti_queue.repair_cache import cache_lease, task_cache
                    from tauceti_queue.repair_cleanup import worktree_lease

                    worktree = cfg.root / "worktrees" / f"task-{task.task_id}"
                    with cache_lease(task_cache(cfg.root, task.task_id)), worktree_lease(cfg.root, task.task_id):
                        if worktree.is_dir():
                            env = repair.lake_env(cfg.root, cache_root=task_cache(cfg.root, task.task_id))
                            publication = repair_publish.start_early(worktree, env)
                            if publication is not None:
                                outcome["publish"] = publication
                    state.write(status, outcome=outcome)
                    outcomes.append(outcome)
                    if not cfg.keep_worktree:
                        with contextlib.suppress(Exception):
                            retire_worktree(cfg, task, record.get("artifacts"))
                    continue
            state.write("claim_lost", detail=f"delivery retry: {error}")
            if not cfg.keep_worktree:
                with contextlib.suppress(Exception):
                    retire_worktree(cfg, task, record.get("artifacts"))
            log(f"task {task.task_id}: claim no longer ours; not delivered")
            continue
        except ServiceError as error:
            log(f"task {task.task_id}: renew failed ({error}); will retry on the next start")
            continue
        if not record.get("delivery"):
            # Stopped, killed or crashed before its result was validated: abandon the attempt and
            # give the claim back now rather than hold it to expiry.
            log(
                f"task {task.task_id}: claim still ours but stopped while {record.get('state')}; "
                "abandoning it and releasing the claim (worker-stopped)"
            )
            outcomes.append(
                release_task(
                    cfg, client, task, state, "worker-stopped", f"worker restarted while {record.get('state')}"
                )
            )
            if not cfg.keep_worktree:
                with contextlib.suppress(Exception):
                    retire_worktree(cfg, task, record.get("artifacts"))
            continue
        if record.get("next_retry_at", 0) > time.time():
            continue
        log(f"retrying delivery of task {task.task_id}")
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
            require_task_execution(cfg=cfg, task=task)
        except NativeTaskExecutionUnavailable:
            continue
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
        publications = repair_publish.background_status()
        if publications is not None:
            summary["publish"] = publications
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
            require_task_execution(cfg=cfg, task=task)
        except NativeTaskExecutionUnavailable as error:
            log(f"{directory.name}: native release checkpoint needs its native runner ({error})")
            continue
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

    require_task_execution(cfg=cfg)
    admin = admin or AdminCheck(cfg)
    summary: dict = {
        "schema": HEARTBEAT_SCHEMA,
        "pid": os.getpid(),
        "worker": cfg.worker,
        "service": cfg.service,
        "root": str(cfg.root),
        # The code this process was started from (its release's baked revision), for the installer's
        # check and the operator; read once, so it never follows later changes on disk.
        "revision": STARTUP_REVISION,
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
        if outcome.get("reason") not in ("artifacts_unavailable", "submission_unavailable"):
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
            for outcome in retry_deliveries(cfg, client, stop=stop):
                record(outcome)
        except Exception as error:
            failed("delivery retry", error, trace=True)
    while not stop.is_set():
        outcome = None
        try:
            deliveries = [r for _, r in in_flight_checkpoints(cfg) if r.get("delivery")]
            if deliveries:
                for completed in retry_deliveries(cfg, client, stop=stop):
                    record(completed)
                write_heartbeat(cfg, summary, "delivery")
                stop.wait(cfg.poll_seconds)
                continue
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
    if cfg.binding is not None:
        return [
            {
                "check": "native task adapters",
                "ok": False,
                "detail": "native task transport/runner adapters are unavailable",
            }
        ]
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
    token_ok = False
    app = None
    try:
        app = None if cfg.token else worker_app()
        if app is not None:
            failure = refresh_github_token()
            if failure:
                raise RuntimeError(failure)
        else:
            use_worker_token(cfg.token or worker_token())
        token_ok = True
        check("github token", True, f"from {token_source()}")
    except Exception as error:
        check("github token", False, str(error))
    try:
        from tauceti_worker.security_audit import api

        if not token_ok:
            raise RuntimeError("not checked without the worker's own token")

        if app is not None:
            verify_admin(cfg.repository)
            check("repository access", True, f"{app.login} with contents write on {cfg.repository}")
        else:
            login = str(api("user").get("login") or "")
            admin = api(f"repos/{cfg.repository}").get("permissions", {}).get("admin") is True
            check("repository admin", admin, f"{login or 'unknown login'} on {cfg.repository}")
    except Exception as error:
        check("repository access" if app is not None else "repository admin", False, str(error))
    cached = read_admin_check(cfg)
    if cached:
        age = time.time() - cached["verified_at"]
        state = "current" if age <= cfg.admin_recheck_seconds else "expired, the worker checks again"
        check(
            "admin verification cache",
            True,
            f"{cached.get('login') or 'unknown login'} on {cfg.repository} verified {describe_age(age)} ago "
            f"({state}; window {describe_age(cfg.admin_recheck_seconds)})",
        )
    else:
        check("admin verification cache", True, f"none in {admin_check_path(cfg)}; the worker checks at start")
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
    parser.add_argument(
        "--no-resume", action="store_true", help="do not retry pending deliveries or release abandoned claims on start"
    )
    parser.add_argument(
        "--no-stream-repair",
        action="store_true",
        help="let the agent run the first whole-library build itself instead of the worker streaming it to the agent"
        " as it runs ($TAUCETI_QUEUE_STREAM_REPAIR=0)",
    )
    parser.add_argument(
        "--no-worker-audits",
        action="store_true",
        help="skip the worker's verification build and CI's audits and changed-module lint after its green builds"
        " ($TAUCETI_QUEUE_WORKER_AUDITS=0)",
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
        worker_audits=repair_audit.enabled() and not args.no_worker_audits,
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
        outcomes += retry_deliveries(cfg, client)
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

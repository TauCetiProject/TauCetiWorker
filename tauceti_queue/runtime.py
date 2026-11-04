"""What the running process knows about itself, and how the status endpoint reports it.

The plan ("State, existing code, and implementation") requires the status endpoint to carry a
schema version, the service's running revision (git SHA) and its start time, so a deployment is
verified from the endpoint rather than inferred from the next natural event. This module owns that
part of the status document:

- `running_revision()`: the git SHA of the installed checkout, from a baked `_revision.py` when a
  wheel or `services/queue/install.sh` wrote one, otherwise `git -C <package dir> rev-parse HEAD`;
- `ServiceRuntime`: revision, start time, host, pid and the configuration facts status reports;
- `status_document()`: the `tauceti-queue.status/v1` envelope, merging the controller's own status
  (head, open batch, waiting inputs, claims, CI placement, stall, hold) under the service section;
- `summarize()`: the one-screen rendering `tauceti-queue status` prints;
- `fallback_server()`: a stdlib HTTP server answering `/status`, `/healthz` and `/settings` when the
  controller and the GitHub adapters are not installed yet, so the host, unit, tunnel and install
  script can be verified ahead of them. It never serves webhooks or admin writes.

Status document layout (all times are UTC ISO 8601; absent sections are `null`):

    schema          "tauceti-queue.status/v1"
    generated_at
    service         revision, revision_source, dirty, started_at, host, pid, repository,
                    canonical_branch, publication_enabled, mode ("shadow" or "production"),
                    controller ("running" or "unavailable"), reason (when unavailable)
    settings        effective insertion_age_window, repair_concurrency, stall_interval_seconds
    conditions      list of the first-line conditions from "Production monitoring", each
                    {"condition": ..., "since": ..., "detail": ...}
    head            batch, version, base, tip, members, claim, ci {run_id, runner, host,
                    started_at, phases[{name, started_at, updated_at, progress}]}, waiting_for
    open_batch      batch, size, oldest_member_age_seconds
    approved_waiting  count of approved inputs not yet inserted
    pending_task    the claimable repair or conflict task, if any
    stall           {since, phase, silent_seconds} or null
    hold            {since, last_error} or null
    feeds           {observation, candidates}: the compatibility feed paths
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import STATUS_SCHEMA, Config

PACKAGE_DIR = Path(__file__).resolve().parent
OBSERVATION_FEED = "/repositories/1/active-batches"
CANDIDATES_FEED = "/repositories/1/candidates"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- revision --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    sha: str  # 40 hex characters, or "unknown"
    dirty: bool
    source: str  # "baked", "git" or "unknown"

    @property
    def short(self) -> str:
        return self.sha[:12] if self.sha != "unknown" else "unknown"


def running_revision(package_dir: Path | None = None, timeout: float = 10) -> Revision:
    """The git SHA this process runs from.

    A baked `_revision.py` (module attribute `SHA`, optional `DIRTY`) wins, because an installed
    wheel has no `.git`; `services/queue/install.sh` writes one for the checkout it installs. In a
    plain checkout the SHA comes from git, with `dirty` set when tracked files are modified.
    """
    package_dir = package_dir or PACKAGE_DIR
    baked = package_dir / "_revision.py"
    if baked.is_file():
        namespace: dict[str, Any] = {}
        try:
            exec(compile(baked.read_text(), str(baked), "exec"), namespace)
        except Exception:
            namespace = {}
        sha = str(namespace.get("SHA", "")).strip().lower()
        if _SHA_RE.match(sha):
            return Revision(sha, bool(namespace.get("DIRTY", False)), "baked")
    try:
        sha = subprocess.run(
            ["git", "-C", str(package_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(package_dir), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return Revision("unknown", False, "unknown")
    if not _SHA_RE.match(sha):
        return Revision("unknown", False, "unknown")
    return Revision(sha, bool(status.strip()), "git")


# --- service runtime -------------------------------------------------------------------------------


@dataclass
class ServiceRuntime:
    revision: Revision
    started_at: str
    host: str
    pid: int
    repository: str
    canonical_branch: str
    publication_enabled: bool

    @classmethod
    def start(cls, config: Config, revision: Revision | None = None) -> ServiceRuntime:
        return cls(
            revision=revision or running_revision(),
            started_at=now_iso(),
            host=config.host,
            pid=os.getpid(),
            repository=config.repository,
            canonical_branch=config.canonical_branch,
            publication_enabled=config.publication_enabled,
        )

    def section(self, controller: str = "running", reason: str | None = None) -> dict[str, Any]:
        return {
            "revision": self.revision.sha,
            "revision_source": self.revision.source,
            "dirty": self.revision.dirty,
            "started_at": self.started_at,
            "host": self.host,
            "pid": self.pid,
            "repository": self.repository,
            "canonical_branch": self.canonical_branch or None,
            "publication_enabled": self.publication_enabled,
            "mode": "production" if self.publication_enabled else "shadow",
            "controller": controller,
            "reason": reason,
        }


def status_document(
    runtime: ServiceRuntime,
    settings: Mapping[str, int],
    controller_status: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The full status document: the controller's sections under the service envelope.

    The controller's status (stream E) supplies `head`, `open_batch`, `approved_waiting`,
    `pending_task`, `stall`, `hold`, `conditions` and may override `settings` with the persisted
    effective values. Keys the controller does not report are present and null.
    """
    document: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "generated_at": now_iso(),
        "service": runtime.section("running" if controller_status is not None else "unavailable", reason),
        "settings": dict(settings),
        "conditions": [],
        "head": None,
        "open_batch": None,
        "approved_waiting": None,
        "pending_task": None,
        "stall": None,
        "hold": None,
        "feeds": {"observation": OBSERVATION_FEED, "candidates": CANDIDATES_FEED},
    }
    if controller_status:
        for key, value in controller_status.items():
            if key == "service":
                continue  # the process, not the controller, knows its revision
            if key == "settings" and isinstance(value, Mapping):
                document["settings"].update(value)
            else:
                document[key] = value
    return document


# --- summary ---------------------------------------------------------------------------------------


def _instant(value: Any) -> datetime | None:
    """A status timestamp as a datetime: the controller reports epoch seconds, the envelope ISO 8601."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _when(value: Any) -> str:
    then = _instant(value)
    return then.strftime("%Y-%m-%dT%H:%M:%SZ") if then else "?"


def _age(since: Any, now: datetime | None = None) -> str:
    then = _instant(since)
    if then is None:
        return "?"
    now = now or datetime.now(UTC)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _sha(value: Any) -> str:
    return str(value)[:12] if value else "-"


def _count(value: Any) -> Any:
    return len(value) if isinstance(value, list) else value


def summarize(document: Mapping[str, Any], now: datetime | None = None) -> str:
    """One screen: head, phase progress, stall, hold, conditions, settings, revision."""
    service = document.get("service") or {}
    settings = document.get("settings") or {}
    lines: list[str] = []
    revision = str(service.get("revision") or "unknown")[:12]
    dirty = " (dirty)" if service.get("dirty") else ""
    lines.append(
        f"tauceti-queue {service.get('mode', '?')} on {service.get('host', '?')}  "
        f"revision {revision}{dirty}  up {_age(service.get('started_at'), now)}  "
        f"pid {service.get('pid', '?')}"
    )
    lines.append(
        f"repository {service.get('repository', '?')} branch {service.get('canonical_branch') or '(undiscovered)'}  "
        f"publication_enabled={str(bool(service.get('publication_enabled'))).lower()}  "
        f"controller {service.get('controller', '?')}" + (f": {service['reason']}" if service.get("reason") else "")
    )
    conditions = document.get("conditions") or []
    if conditions:
        lines.append("CONDITIONS:")
        for item in conditions:
            if isinstance(item, Mapping):
                lines.append(f"  ! {item.get('condition')} since {_when(item.get('since'))}: {item.get('detail', '')}")
            else:
                lines.append(f"  ! {item}")
    hold = document.get("hold")
    if hold:
        lines.append(
            f"HOLD: inference unavailable since {_when(hold.get('since'))} ({_age(hold.get('since'), now)}): {hold.get('last_error', '')}"
        )
    head = document.get("head")
    stall = document.get("stall") or (head or {}).get("stall")
    if stall:
        silence = stall.get("silence_seconds", stall.get("silent_seconds"))
        silence_text = f"{silence:.0f}" if isinstance(silence, (int, float)) else "?"
        lines.append(
            f"STALL: phase {stall.get('phase')} silent {silence_text}s "
            f"(since {_when(stall.get('since'))}, interval {settings.get('stall_interval_seconds', '?')}s)"
        )
    if head:
        claim = head.get("claim") or {}
        claim_text = (
            f"claimed by {claim.get('worker', '?')} for {claim.get('operation', '?')}" if claim else "unclaimed"
        )
        size = head.get("size", _count(head.get("members")))
        stage = head.get("stage") or head.get("waiting_for")
        lines.append(
            f"head: batch {head.get('id', head.get('batch'))} v{head.get('version', '?')}  {size if size is not None else '?'} members  "
            f"base {_sha(head.get('base'))}  tip {_sha(head.get('tip'))}  {claim_text}"
            + (f"  stage {stage}" if stage else "")
        )
        ci = head.get("ci") or {}
        if ci:
            outcome = ci.get("outcome") or ci.get("status") or "?"
            lines.append(
                f"  ci run {ci.get('run_id') or '(not yet found)'} on {ci.get('runner') or '?'}@{ci.get('host') or '?'} "
                f"{outcome}{(': ' + ci['outcome_detail']) if ci.get('outcome_detail') else ''}  "
                f"started {_when(ci.get('started_at'))} ({_age(ci.get('started_at'), now)} ago)"
            )
            phases = list(ci.get("phases") or [])
            if len(phases) > 6:
                lines.append(f"    ({len(phases) - 6} earlier phases)")
                phases = phases[-6:]
            for phase in phases:
                done = phase.get("status") == "completed" or bool(phase.get("finished_at") or phase.get("completed_at"))
                mark = " " if done else "*"
                updated = phase.get("completed_at") or phase.get("updated_at") or phase.get("started_at")
                lines.append(
                    f"   {mark} {str(phase.get('name', '?')):<40.40} {str(phase.get('detail') or phase.get('progress') or phase.get('status') or ''):<12.12} "
                    f"updated {_when(updated)} ({_age(updated, now)} ago)"
                )
    else:
        lines.append("head: none")
    open_batch = document.get("open_batch")
    if open_batch:
        age = open_batch.get("oldest_member_age_seconds")
        age_text = f"{age:.0f}" if isinstance(age, (int, float)) else str(age)
        lines.append(
            f"open batch: {open_batch.get('id', open_batch.get('batch', '?'))} size {open_batch.get('size', _count(open_batch.get('members')))}"
            + (f", oldest member {age_text}s" if age is not None else "")
        )
    else:
        lines.append("open batch: none")
    waiting = document.get("waiting")
    count = waiting.get("count") if isinstance(waiting, Mapping) else document.get("approved_waiting")
    urgent = waiting.get("urgent") if isinstance(waiting, Mapping) else None
    lines.append(f"approved waiting: {count if count is not None else '?'}" + (f" ({urgent} urgent)" if urgent else ""))
    task = document.get("pending_task")
    if task:
        lines.append(
            f"pending task: {task.get('kind', '?')} {task.get('id', '')} on batch {task.get('batch', '?')} ({task.get('state', '?')})"
        )
    lines.append(
        "settings: "
        + "  ".join(
            f"{key}={settings.get(key, '?')}"
            for key in ("insertion_age_window", "repair_concurrency", "stall_interval_seconds")
        )
    )
    return "\n".join(lines)


# --- fallback server -------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "tauceti-queue/0"
    status_provider: Callable[[], dict[str, Any]]
    settings_provider: Callable[[], dict[str, int]]
    log_requests: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        if self.log_requests:
            super().log_message(format, *args)

    def _send(self, code: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/status", "/status/"):
            self._send(200, self.status_provider())
        elif path == "/healthz":
            document = self.status_provider()
            self._send(200, {"ok": True, "schema": document["schema"], "service": document["service"]})
        elif path == "/settings":
            self._send(200, {"settings": self.settings_provider(), "source": "configuration"})
        else:
            self._send(404, {"error": "not_found", "path": path})

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._send(
            501,
            {
                "error": "controller_unavailable",
                "detail": "the controller and GitHub adapters are not installed in this revision; "
                "webhooks and admin writes are not served",
            },
        )


def fallback_server(
    config: Config,
    status_provider: Callable[[], dict[str, Any]],
    settings_provider: Callable[[], dict[str, int]] | None = None,
) -> ThreadingHTTPServer:
    """A status-only server for a host without the controller; `shutdown()` stops it."""
    handler = type(
        "FallbackHandler",
        (_Handler,),
        {
            "status_provider": staticmethod(status_provider),
            "settings_provider": staticmethod(settings_provider or (lambda: config.settings.as_dict())),
        },
    )
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    server.daemon_threads = True
    return server


class StopSignal:
    """A latch the signal handlers set and the serve loop waits on."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def set(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def wait_for_status(
    url: str, timeout: float, fetch: Callable[[str], dict[str, Any]], interval: float = 1.0
) -> dict[str, Any]:
    """Fetch `url` until it answers or `timeout` seconds pass; raises the last error."""
    deadline = time.monotonic() + max(0.0, timeout)
    last: Exception | None = None
    while True:
        try:
            return fetch(url)
        except Exception as exc:
            last = exc
        if time.monotonic() >= deadline:
            raise RuntimeError(f"no status from {url} after {timeout:g}s: {last}")
        time.sleep(interval)

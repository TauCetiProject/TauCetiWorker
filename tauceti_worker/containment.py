"""Rootless PID-namespace containment for a host round's agent and tool processes.

The round supervisor, meaning the round lock, claims, activity, usage accounting and recovery
journals, stays outside. Each long-running process it launches (the coding agent, cache
preparation downloads, the review engine and the security-audit client) becomes the initial
process of a fresh PID namespace that bubblewrap creates without root. When that process exits,
the kernel kills everything still in the namespace before bubblewrap returns, including
descendants that called setsid or double-forked, which no process-group sweep can reach.
`--die-with-parent` covers a supervisor that is SIGKILLed or loses its parent.

A shared Mathlib overlay view is itself a bubblewrap PID namespace, so it is reused as the single
boundary rather than nested inside a second one.

Hosts without working rootless namespaces (non-Linux systems, missing bubblewrap, disabled user
namespaces) or an operator rollback (`TAUCETI_PROCESS_CONTAINMENT=legacy`) fall back to the
previous best-effort process-group cleanup, and say so: they never report strong containment.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass

from .config import Die, log

POLICY_ENV = "TAUCETI_PROCESS_CONTAINMENT"
NAMESPACE = "pid-namespace"
PROCESS_GROUP = "process-group"
UNCONTAINED = "uncontained"

PREREQUISITE = (
    "install bubblewrap (bwrap) and allow unprivileged user namespaces "
    "(sysctl kernel.unprivileged_userns_clone=1; on Ubuntu, an AppArmor profile for bwrap or "
    "kernel.apparmor_restrict_unprivileged_userns=0); no root daemon is needed"
)
LEGACY_NOTE = (
    "best-effort process-group cleanup only: tools that call setsid or double-fork can outlive "
    "their round, and automatic work recovery is paused"
)


def command(*extra: str) -> list[str]:
    """The bwrap prefix for one boundary. `extra` adds mounts (the shared Mathlib overlay) to the
    same namespace. The whole host filesystem, /dev and the network stay as they were."""
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        *extra,
        "--",
    ]


def probe() -> tuple[bool, str]:
    """Run a real bwrap PID namespace and confirm the child sees a different namespace."""
    if platform.system() != "Linux":
        return False, f"PID namespaces are Linux-only on this {platform.system()} host"
    if not shutil.which("bwrap"):
        return False, "bubblewrap (bwrap) is not on PATH"
    try:
        p = subprocess.run(command() + ["readlink", "/proc/self/ns/pid"], capture_output=True, text=True, timeout=10)
        outer = os.readlink("/proc/self/ns/pid")
        inner = p.stdout.strip()
    except Exception as error:  # a broken probe means no containment, never a crashed round
        return False, f"rootless bwrap PID namespace unavailable: {error}"
    if p.returncode or not isinstance(inner, str) or not inner.startswith("pid:[") or inner == outer:
        detail = (p.stderr or "").strip()[-300:] or f"exit {p.returncode}"
        return False, f"rootless bwrap PID namespace probe failed: {detail}"
    return True, "rootless bwrap PID namespace per agent/tool; setsid and double-forked descendants die with it"


@dataclass(frozen=True)
class Capability:
    strong: bool
    mode: str
    reason: str


_probed: tuple[bool, str] | None = None


def policy(env=None) -> str:
    env = os.environ if env is None else env
    value = (env.get(POLICY_ENV) or "auto").strip().lower()
    if value not in {"auto", "required", "legacy"}:
        raise Die(f"{POLICY_ENV} must be auto, required or legacy")
    return value


def capability(env=None, *, fresh=False) -> Capability:
    """What this host can contain. The probe runs once per process; `fresh` re-runs it."""
    global _probed
    selected = policy(env)
    if selected == "legacy":
        return Capability(False, PROCESS_GROUP, f"{POLICY_ENV}=legacy: {LEGACY_NOTE}")
    if fresh or _probed is None:
        _probed = probe()
    ok, reason = _probed
    if ok:
        return Capability(True, NAMESPACE, reason)
    if selected == "required":
        raise Die(f"{POLICY_ENV}=required but {reason}; {PREREQUISITE}")
    if platform.system() != "Linux":
        return Capability(False, PROCESS_GROUP, f"{reason}; {LEGACY_NOTE}")
    return Capability(False, PROCESS_GROUP, f"{reason}; {PREREQUISITE}. Until then: {LEGACY_NOTE}")


def status(env=None, *, fresh=False) -> Capability:
    """`capability` for reporting: a refusal under `required` is described, not raised."""
    try:
        return capability(env, fresh=fresh)
    except Die as error:
        return Capability(False, PROCESS_GROUP, str(error))


def doctor_row(env=None) -> tuple[bool, str]:
    cap = status(env, fresh=True)
    return cap.strong, f"{cap.mode}: {cap.reason}; {POLICY_ENV}=auto|required|legacy"


def strong(env=None) -> bool:
    """True only when every new boundary is a PID namespace. Never raises."""
    return status(env).strong


def _namespace_alive(pid: int, namespace: int) -> bool:
    # The namespace's init keeps this link until every other member is dead and reaped, so the
    # link names the boundary, not just a pid that the kernel might have reused.
    try:
        return os.readlink(f"/proc/{pid}/ns/pid") == f"pid:[{namespace}]"
    except OSError:
        return False


class Boundary:
    """One owned PID namespace (or, on a legacy host, best-effort process group) for one command.

    Use: `argv = b.argv(cmd)`, launch it with `pass_fds=b.pass_fds`, call `b.launched()` once the
    child exists, and `b.finish(rc)` after it exits. `finish` confirms the namespace is empty, and
    kills only that namespace if it is not, before the caller treats the checkout as quiescent. It
    records identity and result with the current round; it never records argv, which can carry
    prompts and credentials, and never searches the host for processes it does not own."""

    def __init__(self, label: str, *, prefix=(), contain=True):
        self.label = label
        self.prefix = list(prefix)
        self.identity: dict = {}
        self._info_r: int | None = None
        self._info_w: int | None = None
        self._started = time.time()
        self._finished = False
        if not contain:
            self.strong, self.mode, self.reason = False, UNCONTAINED, "runs in a sandbox owned elsewhere"
        elif self.prefix:
            # The shared Mathlib view is already a bwrap PID namespace: it is the boundary.
            self.strong, self.mode, self.reason = True, NAMESPACE, "shared Mathlib overlay namespace"
        else:
            cap = capability()
            self.strong, self.mode, self.reason = cap.strong, cap.mode, cap.reason

    def argv(self, argv) -> list[str]:
        if not self.strong:
            return list(argv)
        base = self.prefix or command()
        if base[0] != "bwrap":
            raise ValueError("containment prefix must be a bwrap command")
        self._info_r, self._info_w = os.pipe()
        return [base[0], "--info-fd", str(self._info_w), *base[1:], *argv]

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return () if self._info_w is None else (self._info_w,)

    def launched(self) -> None:
        if self._info_w is not None:
            with contextlib.suppress(OSError):
                os.close(self._info_w)
            self._info_w = None

    def _read_identity(self) -> dict:
        if self._info_r is None:
            return self.identity
        import json

        data = b""
        try:
            os.set_blocking(self._info_r, False)
            while chunk := os.read(self._info_r, 65536):
                data += chunk
        except (BlockingIOError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(self._info_r)
            self._info_r = None
        try:
            value = json.loads(data.decode() or "{}")
        except ValueError:
            value = {}
        if isinstance(value, dict):
            self.identity = {
                key: value[key]
                for key in ("child-pid", "pid-namespace")
                if type(value.get(key)) is int and value[key] > 0
            }
        return self.identity

    def _await_empty(self, grace: float) -> str:
        pid, namespace = self.identity.get("child-pid"), self.identity.get("pid-namespace")
        if pid is None or namespace is None:
            return "not-started"  # bwrap failed before creating the namespace
        deadline = time.monotonic() + grace
        while _namespace_alive(pid, namespace):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        else:
            return "namespace-empty"
        # Signal the boundary's own init, after rechecking that the pid still names it. Killing a
        # namespace's init makes the kernel kill every other member.
        if _namespace_alive(pid, namespace):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _namespace_alive(pid, namespace) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _namespace_alive(pid, namespace):
            log(f"WARNING: {self.label}: PID namespace {namespace} survived SIGKILL of its init {pid}")
            return "namespace-survived"
        return "namespace-killed"

    def finish(self, rc: int | None, *, pgid: int | None = None, grace: float = 5.0) -> str:
        """Confirm the boundary is empty, record the outcome with the round, and return it."""
        if self._finished:
            return "finished"
        self._finished = True
        self.launched()
        self._read_identity()
        if self.strong:
            cleanup = self._await_empty(grace)
        elif self.mode == UNCONTAINED:
            cleanup = "external"
        elif pgid is not None:
            from .round import reap_round_group

            reap_round_group(pgid)
            cleanup = "process-group-swept-best-effort"
        else:
            cleanup = "round-group-best-effort"
        record(self, rc, cleanup)
        return cleanup


def record(boundary: Boundary, rc: int | None, cleanup: str) -> None:
    from . import activity

    round_id = activity.current_round_id()
    if cleanup in {"namespace-killed", "namespace-survived"} or (rc is not None and rc < 0):
        log(f"containment: {boundary.label} exited {rc}; {boundary.mode} cleanup {cleanup}")
    if round_id is None:
        return  # recorded only with a round; ad hoc commands have none to attach it to
    activity.emit(
        "containment_result",
        key=f"{round_id or 'no-round'}:{boundary.label}:{uuid.uuid4().hex}",
        round_id=round_id,
        label=boundary.label,
        mode=boundary.mode,
        child_pid=boundary.identity.get("child-pid"),
        pid_namespace=boundary.identity.get("pid-namespace"),
        exit_code=rc,
        cleanup=cleanup,
        started_at=boundary._started,
    )


def run(argv, *, label: str, input=None, timeout: float | None = None, env=None, capture_output=False, **kwargs):
    """`subprocess.run` inside one boundary. A timeout kills bwrap, and with it the namespace."""
    boundary = Boundary(label)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    extra_fds = tuple(kwargs.pop("pass_fds", ()))
    command_ = boundary.argv(argv)
    rc = None
    try:
        with subprocess.Popen(command_, env=env, pass_fds=(*extra_fds, *boundary.pass_fds), **kwargs) as proc:
            boundary.launched()
            try:
                stdout, stderr = proc.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                proc.kill()
                error.output, error.stderr = proc.communicate()
                error.cmd = argv
                raise
            except BaseException:
                proc.kill()
                raise
            rc = proc.returncode
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)
    finally:
        boundary.finish(rc)

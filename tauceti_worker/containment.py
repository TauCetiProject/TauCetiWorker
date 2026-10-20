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
import errno
import os
import platform
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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


_HAVE_PIDFD = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


class ContainmentUnconfirmed(Die):
    """A boundary could not be confirmed empty; its checkout and leases must not be reused yet."""


def _namespace_state(pid: int, namespace: int) -> bool | None:
    """True while the namespace lives, False once it is gone, None if this cannot be determined.

    The namespace's init keeps this link until every other member is dead and reaped, so the link
    names the boundary, not just a pid that the kernel might have reused."""
    try:
        if os.readlink(f"/proc/{pid}/ns/pid") != f"pid:[{namespace}]":
            return False
        # A zombie keeps the link until its parent reaps it, and an orphaned init may wait on a slow
        # subreaper. The kernel lets init exit only after every other member is gone, so a dead
        # init means an empty namespace.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in {"Z", "X"}
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, IndexError):
        return None


def _marker_dir():
    from . import activity

    return activity.root() / "containment-unconfirmed"


def unconfirmed() -> list[dict]:
    """Boundaries whose emptiness was never confirmed and that may still be alive. A marker is
    retired only once its own namespace is observed gone; nothing else on the host is examined."""
    import json

    live = []
    for path in sorted(_marker_dir().glob("*.json")) if _marker_dir().is_dir() else ():
        try:
            entry = json.loads(path.read_text())
            gone = _namespace_state(int(entry["child_pid"]), int(entry["pid_namespace"])) is False
        except (OSError, ValueError, KeyError, TypeError):
            entry, gone = {"marker": path.name}, False
        if gone:
            path.unlink(missing_ok=True)
        else:
            live.append(entry)
    return live


class Boundary:
    """One owned PID namespace (or, on a legacy host, best-effort process group) for one command.

    Use: `argv = b.argv(cmd)`, launch it with `pass_fds=b.pass_fds`, call `b.launched()` as soon as
    the child exists, and `b.finish(rc)` after it exits; `b.abort()` tears the namespace down at once
    (before draining its pipes). `launched` takes bwrap's identity handshake and a pidfd for the
    namespace's init, and records a start receipt. `finish` confirms the namespace is empty, killing
    only its own init if it is not, before the caller treats the checkout as quiescent, and raises
    `ContainmentUnconfirmed` if emptiness cannot be established. Receipts never carry argv, which can
    hold prompts and credentials, and nothing here searches the host for processes it does not own."""

    def __init__(self, label: str, *, prefix=(), contain=True):
        self.id = uuid.uuid4().hex
        self.label = label
        self.prefix = list(prefix)
        self.identity: dict = {}
        self._info_r: int | None = None
        self._info_w: int | None = None
        self._pidfd: int | None = None
        self._buffer = b""
        self._started = time.time()
        self._finished = False
        self._launched = False
        from . import activity

        # Registered before anything runs: a round's proof needs every boundary's outcome.
        if (round_id := activity.current_round_id()) is not None:
            _boundaries.setdefault(round_id, {})[self.id] = None
        if not contain:
            self.strong, self.mode, self.reason = False, UNCONTAINED, "runs in a sandbox owned elsewhere"
        elif self.prefix:
            # The shared Mathlib view is already a bwrap PID namespace: it is the boundary. The view
            # is never offered under the legacy rollback (shared_mathlib.view refuses it).
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

    def launched(self, *, timeout: float = 5.0) -> None:
        """Close our copy of the handshake pipe and take the namespace's identity."""
        self._launched = True
        if self._info_w is not None:
            with contextlib.suppress(OSError):
                os.close(self._info_w)
            self._info_w = None
        if self._info_r is not None and not self.identity:
            self._read_identity(timeout)
            if self.identity:
                self._await_setup(timeout)
                record(self, "containment_started")

    def _read_identity(self, timeout: float) -> None:
        import json
        import select

        deadline = time.monotonic() + timeout
        value, chunk = None, b"pending"
        while self._info_r is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._info_r], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(self._info_r, 65536)
            except OSError:
                chunk = b""
            self._buffer += chunk
            try:
                value = json.loads(self._buffer.decode())
                break
            except ValueError:
                if not chunk:
                    break  # EOF: bwrap exited before reporting a namespace
        if self._info_r is not None and (value is not None or time.monotonic() >= deadline or not chunk):
            with contextlib.suppress(OSError):
                os.close(self._info_r)
            self._info_r = None
        if isinstance(value, dict):
            identity = {
                key: value[key]
                for key in ("child-pid", "pid-namespace")
                if type(value.get(key)) is int and value[key] > 0
            }
            if len(identity) == 2:
                self.identity = identity
                self._open_pidfd()

    def _await_setup(self, timeout: float) -> None:
        # bwrap reports the namespace before its init arms --die-with-parent; until init forks the
        # command, losing bwrap would orphan the namespace. Wait for that fork, so the unprotected
        # window is bwrap's own setup, and abort() still covers it while this process lives.
        pid = self.identity["child-pid"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if Path(f"/proc/{pid}/task/{pid}/children").read_text().split():
                    return
            except OSError:
                return  # gone, or this kernel does not expose children
            time.sleep(0.005)

    def _open_pidfd(self) -> None:
        # Open first, then verify the pid still names our namespace: a pidfd cannot follow pid
        # reuse, so every later signal reaches this init or nobody.
        global _HAVE_PIDFD
        if not _HAVE_PIDFD:
            return
        try:
            fd = os.pidfd_open(self.identity["child-pid"])
        except OSError as error:
            if error.errno == errno.ENOSYS:
                _HAVE_PIDFD = False
            return  # otherwise the init is already gone
        if self._state() is True:
            self._pidfd = fd
        else:
            os.close(fd)

    def _state(self) -> bool | None:
        return _namespace_state(self.identity["child-pid"], self.identity["pid-namespace"])

    def _kill_init(self) -> None:
        """Kill the namespace's own init, which makes the kernel kill every other member."""
        if self._pidfd is None and self.identity:
            self._open_pidfd()
        if self._pidfd is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
        elif self.identity and not _HAVE_PIDFD and self._state() is True:
            # Kernels before 5.3 have no pidfd: recheck, then signal by pid (a narrow reuse race).
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self.identity["child-pid"], signal.SIGKILL)

    def abort(self) -> None:
        """Tear the namespace down now: on timeout or interruption, before draining its pipes."""
        if not self.strong:
            return
        self.launched(timeout=1.0)
        if self.identity:
            self._kill_init()

    def _await_empty(self, rc: int | None, grace: float) -> str:
        if not self._launched:
            return "not-started"  # Popen itself failed
        if not self.identity:
            self.launched(timeout=0.5)
        if not self.identity:
            # bwrap reports the namespace right after creating it. Without a report, a normal exit
            # means it never got that far; a killed bwrap might have died just after creating it.
            return "not-started" if rc is not None and rc >= 0 else "namespace-unconfirmed"
        deadline = time.monotonic() + grace
        while (state := self._state()) is True and time.monotonic() < deadline:
            time.sleep(0.02)
        if state is False:
            return "namespace-empty"
        if state is True:
            self._kill_init()
            deadline = time.monotonic() + 5
            while (state := self._state()) is True and time.monotonic() < deadline:
                time.sleep(0.02)
            if state is False:
                return "namespace-killed"
        return "namespace-unconfirmed"

    def finish(self, rc: int | None, *, pgid: int | None = None, grace: float = 5.0) -> str:
        """Confirm the boundary is empty, record the outcome with the round, and return it."""
        if self._finished:
            return "finished"
        if self.strong:
            cleanup = self._await_empty(rc, grace)
        elif self.mode == UNCONTAINED:
            cleanup = "external"
        elif pgid is not None:
            from .round import reap_round_group

            reap_round_group(pgid)
            cleanup = "process-group-swept-best-effort"
        else:
            cleanup = "round-group-best-effort"
        self._finished = True
        if self._info_w is not None:
            with contextlib.suppress(OSError):
                os.close(self._info_w)
            self._info_w = None
        if self._pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self._pidfd)
            self._pidfd = None
        if self._info_r is not None:
            with contextlib.suppress(OSError):
                os.close(self._info_r)
            self._info_r = None
        record(self, "containment_result", rc=rc, cleanup=cleanup)
        if cleanup == "namespace-unconfirmed":
            self._mark_unconfirmed(rc)
            raise ContainmentUnconfirmed(
                f"{self.label}: could not confirm that PID namespace {self.identity.get('pid-namespace')} "
                "is empty; leaving its checkout and leases alone, and automatic recovery is paused"
            )
        return cleanup

    def _mark_unconfirmed(self, rc: int | None) -> None:
        from . import activity
        from .runtime_status import atomic_json

        log(f"WARNING: containment: {self.label} exited {rc}; namespace {self.identity or 'unreported'} unconfirmed")
        with contextlib.suppress(OSError):
            _marker_dir().mkdir(parents=True, exist_ok=True)
            atomic_json(
                _marker_dir() / f"{self.id}.json",
                dict(
                    boundary=self.id,
                    round_id=activity.current_round_id(),
                    label=self.label,
                    child_pid=self.identity.get("child-pid"),
                    pid_namespace=self.identity.get("pid-namespace"),
                    at=time.time(),
                ),
            )


# Outcomes that prove a namespace boundary left nothing behind.
PROVEN = {"namespace-empty", "namespace-killed", "not-started"}
# round id -> boundary id -> (mode, cleanup), or None while the boundary has no confirmed outcome
_boundaries: dict[str, dict[str, tuple[str, str] | None]] = {}


def record(boundary: Boundary, kind: str, *, rc: int | None = None, cleanup: str | None = None) -> None:
    from . import activity

    if cleanup in {"namespace-killed", "namespace-unconfirmed"} or (rc is not None and rc < 0):
        log(f"containment: {boundary.label} exited {rc}; {boundary.mode} cleanup {cleanup}")
    round_id = activity.current_round_id()
    if round_id is None:
        return  # recorded only with a round; ad hoc commands have none to attach it to
    if cleanup is not None:
        _boundaries.setdefault(round_id, {})[boundary.id] = (boundary.mode, cleanup)
    activity.emit(
        kind,
        key=f"{boundary.id}:{kind}",
        boundary=boundary.id,
        round_id=round_id,
        label=boundary.label,
        mode=boundary.mode,
        child_pid=boundary.identity.get("child-pid"),
        pid_namespace=boundary.identity.get("pid-namespace"),
        started_at=boundary._started,
        **({} if kind == "containment_started" else {"exit_code": rc, "cleanup": cleanup}),
    )


def write_quiescence(state_dir, round_id: str) -> bool:
    """At the end of a round, record whether that round left any process behind.

    `state/<worker>/quiescence.json` is diagnostic: it is empty only when this host contains every
    launch and each of the round's boundaries was confirmed empty; any best-effort, external or
    unconfirmed boundary withholds it. A round killed before its cleanup writes nothing. Checkout
    recovery never waits for it: leftovers are saved first, and a checkout still in use is moved
    aside rather than reset."""
    from .runtime_status import atomic_json

    outcomes = list(_boundaries.pop(round_id, {}).values())
    empty = (
        strong() and not unconfirmed() and all(o is not None and o[0] == NAMESPACE and o[1] in PROVEN for o in outcomes)
    )
    proof = dict(
        round_id=round_id,
        empty=empty,
        boundary=f"{NAMESPACE} x{len(outcomes)}" if empty else None,
        boundaries=len(outcomes),
        at=time.time(),
    )
    try:
        atomic_json(Path(state_dir) / "quiescence.json", proof)
    except OSError as error:
        log(f"containment: could not record round quiescence ({error}); checkout recovery stays cautious")
        return False
    return empty


def run(argv, *, label: str, input=None, timeout: float | None = None, env=None, capture_output=False, **kwargs):
    """`subprocess.run` inside one boundary. On timeout or interruption the namespace is killed
    before its pipes are drained, so a survivor holding them cannot block cleanup."""
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
            try:
                boundary.launched()
                stdout, stderr = proc.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                boundary.abort()
                proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    stdout = stderr = None
                rc = proc.wait()
                raise subprocess.TimeoutExpired(argv, timeout, stdout, stderr) from None
            except BaseException:
                boundary.abort()
                proc.kill()
                rc = proc.wait()
                raise
            rc = proc.returncode
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)
    finally:
        boundary.finish(rc)

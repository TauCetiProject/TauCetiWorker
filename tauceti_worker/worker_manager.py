"""Declarative, portable supervision for persistent Tau Ceti workers.

The manager owns desired-state reconciliation.  Terminal multiplexers are deliberately only
viewers: a tmux session created here tails durable logs and can disappear without affecting work.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import json
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import tomllib
import uuid
from pathlib import Path
from typing import NoReturn

from .constants import AGENTS, ALLOWED_TASKS
from .paths import HERE, ensure_ssl_cert_file, entry_cmd, self_argv, self_env
from .quota import parse_pace_curve
from .round import signal_group
from .runtime_status import STATUS_ENV, read_json, update_status

CONFIG_VERSION = 1
DEFAULT_INTERVAL = 2.0
TMUX_SESSION = "tauceti-workers"
_WORKER_KEYS = {
    "id",
    "enabled",
    "agent",
    "only",
    "sandbox",
    "ignore_quota",
    "auto_refresh",
    "roadmap_only",
    "roadmap_skip",
    "roadmap_extra_identities",
    "respect_claims",
    "source",
    "author_model",
    "author_effort",
    "pace",
    "stream",
    "isolate_home",
    "restart",
    "env",
}

WORKERS_EPILOG = """\
quickstart:
  tauceti workers add                         add workerN and start the manager
  tauceti workers add reviewer --only review  add a focused reviewer
  tauceti workers                             inspect desired and actual state
  tauceti workers logs --follow reviewer      follow its durable log

hand-edited config:
  tauceti workers edit                        create or edit workers.toml
  tauceti workers apply --check               validate without reconciling
  tauceti workers apply                       reconcile, starting a manager if needed

long-running service:
  tauceti workers service install             install and start the native user service
  tauceti workers service status              inspect the native user service
  tauceti workers manager-stop                stop a detached manager and its workers
  tauceti workers manager-stop --leave-workers
                                              stop only the manager

configuration (first that is set wins):
  tauceti workers --config PATH apply         an explicit file, before the action
  $TAUCETI_WORKERS_CONFIG                     exact default config path
  $TAUCETI_CONFIG_HOME                        directory holding workers.toml
  $XDG_CONFIG_HOME                            root holding tauceti/workers.toml
  platform default                            macOS Application Support, else ~/.config/tauceti

  tauceti workers import workers.conf         import the legacy line-oriented format once

full reference:
  https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/workers.md
"""


class WorkersError(Exception):
    pass


def workers_die(message: str) -> NoReturn:
    print(f"tauceti workers: {message}", file=sys.stderr)
    raise SystemExit(2)


def config_home() -> Path:
    override = os.environ.get("TAUCETI_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "tauceti"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tauceti"
    return Path.home() / ".config" / "tauceti"


def default_workers_config() -> Path:
    override = os.environ.get("TAUCETI_WORKERS_CONFIG")
    return Path(override).expanduser() if override else config_home() / "workers.toml"


def workers_state_dir() -> Path:
    override = os.environ.get("TAUCETI_WORKERS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "tauceti" / "workers"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tauceti" / "state" / "workers"
    return Path.home() / ".local" / "state" / "tauceti" / "workers"


def workers_runtime_dir() -> Path:
    override = os.environ.get("TAUCETI_RUNTIME_DIR")
    if override:
        root = Path(override).expanduser()
    elif os.environ.get("XDG_RUNTIME_DIR"):
        root = Path(os.environ["XDG_RUNTIME_DIR"]) / "tauceti"
    else:
        root = Path("/tmp") / f"tauceti-{os.getuid()}"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise WorkersError(f"cannot create runtime directory {root}: {exc}") from None
    try:
        owner = root.stat().st_uid
    except OSError as exc:
        raise WorkersError(f"cannot inspect runtime directory {root}: {exc}") from None
    if owner != os.getuid():
        raise WorkersError(f"refusing runtime directory not owned by uid {os.getuid()}: {root}")
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise WorkersError(f"cannot secure runtime directory {root}: {exc}") from None
    if root.stat().st_mode & 0o077:
        raise WorkersError(f"runtime directory is accessible by other users: {root}")
    return root


def _string(value, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise WorkersError(f"{where} must be a string")
    return value


def _boolean(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise WorkersError(f"{where} must be true or false")
    return value


def _strings(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkersError(f"{where} must be an array of strings")
    return tuple(value)


def _pace(value, where: str) -> str | None:
    """A pacing curve, checked here at the CONFIGURATION boundary. Without this a typo passes `workers
    apply --check`, and the manager discovers it only by launching a worker that rejects its own --pace
    and dies — which reads as a crash-looping worker rather than a bad line in workers.toml."""
    spec = _string(value, where, optional=True)
    if spec is not None:
        try:
            parse_pace_curve(spec)
        except ValueError as e:
            raise WorkersError(f"{where}: {e}") from None
    return spec


# Names the manager or the worker's own bootstrap owns. Setting one from the config would break the
# thing it configures, and the failure would read as a worker bug rather than a configuration error:
# the first four decide which state file the worker heartbeats into, whether it knows it is managed,
# where its log goes, and which fd is its parent pipe. TAUCETI_DATA_HOME is worse than broken —
# isolate_home() reads it as an "isolation completed" sentinel, so presetting it would silently skip
# credential isolation and run the worker on the operator's own account.
_RESERVED_ENV = frozenset(
    {STATUS_ENV, "TAUCETI_MANAGED", "TAUCETI_LOG_FILE", "TAUCETI_PARENT_PIPE_FD", "TAUCETI_DATA_HOME"}
)

# A POSIX-portable variable name, which is also what a shell can refer to. `execve` accepts more, but
# a name like `1A` or `A-B` can only be reached by contortion, so it is far likelier to be a typo.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Aggregate cap on one worker's table. The spec reaches the runner as a single command-line argument,
# so an unbounded table becomes E2BIG at launch; refuse it while it is still a configuration error the
# operator can see. Generous next to anything a build setting needs.
_ENV_MAX_BYTES = 16 * 1024


def _env_pairs(value, where: str) -> tuple[tuple[str, str], ...]:
    """A table of environment variables for one worker, normalized to sorted (name, value) pairs.

    Values must be strings: TOML would happily give us `1` for a variable whose consumer expects
    `"1"`, and silently stringifying it hides the mistake in the one place it matters. Names must be
    POSIX-portable, and a value may not contain a NUL — TOML accepts `\\u0000`, `execve` does not, and
    the resulting launch failure would surface as an unexplained restart loop rather than as a
    rejected configuration. This table is not a secret store; see the field's documentation."""
    if not isinstance(value, dict):
        raise WorkersError(f"{where} must be a table of environment variables")
    pairs = []
    for name, item in value.items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise WorkersError(f"{where} has an invalid variable name: {name!r}")
        if name in _RESERVED_ENV:
            raise WorkersError(f"{where}.{name} is set by the worker itself and cannot be overridden")
        if not isinstance(item, str):
            raise WorkersError(f'{where}.{name} must be a string (quote it, e.g. "1")')
        if "\0" in item:
            raise WorkersError(f"{where}.{name} must not contain a NUL byte")
        pairs.append((name, item))
    total = sum(len(name) + len(item) + 2 for name, item in pairs)
    if total > _ENV_MAX_BYTES:
        raise WorkersError(f"{where} is too large ({total} bytes; the limit is {_ENV_MAX_BYTES})")
    return tuple(sorted(pairs))


@dataclasses.dataclass(frozen=True)
class WorkerSpec:
    id: str
    enabled: bool = True
    agent: str = "auto"
    only: tuple[str, ...] = ()
    sandbox: str = "host"
    ignore_quota: bool = False
    auto_refresh: bool = False
    roadmap_only: str | None = None
    roadmap_skip: tuple[str, ...] = ()
    roadmap_extra_identities: tuple[str, ...] = ()
    respect_claims: bool = True
    source: str | None = None
    author_model: str | None = None
    author_effort: str | None = None
    pace: str | None = None
    stream: bool = False
    isolate_home: bool = False
    restart: str = "always"
    # Extra environment for this worker's process tree, as sorted (name, value) pairs so the spec stays
    # hashable and its fingerprint is order-independent.
    env: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def from_dict(raw: dict, index: int) -> WorkerSpec:
        if not isinstance(raw, dict):
            raise WorkersError(f"workers[{index}] must be a table")
        unknown = sorted(set(raw) - _WORKER_KEYS)
        if unknown:
            raise WorkersError(f"workers[{index}] has unknown field(s): {', '.join(unknown)}")
        wid = _string(raw.get("id"), f"workers[{index}].id")
        assert wid is not None
        if not wid or len(wid) > 40 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in wid):
            raise WorkersError(f"workers[{index}].id must match [a-z0-9-]+ and be at most 40 characters")
        enabled = _boolean(raw.get("enabled", True), f"workers[{index}].enabled")
        agent = _string(raw.get("agent", "auto"), f"workers[{index}].agent")
        if agent not in AGENTS:
            raise WorkersError(f"workers[{index}].agent must be one of: {', '.join(AGENTS)}")
        only = _strings(raw.get("only", []), f"workers[{index}].only")
        bad_tasks = [task for task in only if task not in ALLOWED_TASKS]
        if bad_tasks:
            raise WorkersError(f"workers[{index}].only has unknown task(s): {', '.join(bad_tasks)}")
        sandbox = _string(raw.get("sandbox", "host"), f"workers[{index}].sandbox")
        if sandbox not in ("host", "bubble"):
            raise WorkersError(f"workers[{index}].sandbox must be 'host' or 'bubble'")
        restart = _string(raw.get("restart", "always"), f"workers[{index}].restart")
        if restart not in ("always", "on-failure", "never"):
            raise WorkersError(f"workers[{index}].restart must be 'always', 'on-failure', or 'never'")
        spec = WorkerSpec(
            id=wid,
            enabled=enabled,
            agent=agent,
            only=only,
            sandbox=sandbox,
            ignore_quota=_boolean(raw.get("ignore_quota", False), f"workers[{index}].ignore_quota"),
            auto_refresh=_boolean(raw.get("auto_refresh", False), f"workers[{index}].auto_refresh"),
            roadmap_only=_string(raw.get("roadmap_only"), f"workers[{index}].roadmap_only", optional=True),
            roadmap_skip=_strings(raw.get("roadmap_skip", []), f"workers[{index}].roadmap_skip"),
            roadmap_extra_identities=_strings(
                raw.get("roadmap_extra_identities", []), f"workers[{index}].roadmap_extra_identities"
            ),
            respect_claims=_boolean(raw.get("respect_claims", True), f"workers[{index}].respect_claims"),
            source=_string(raw.get("source"), f"workers[{index}].source", optional=True),
            author_model=_string(raw.get("author_model"), f"workers[{index}].author_model", optional=True),
            author_effort=_string(raw.get("author_effort"), f"workers[{index}].author_effort", optional=True),
            pace=_pace(raw.get("pace"), f"workers[{index}].pace"),
            stream=_boolean(raw.get("stream", False), f"workers[{index}].stream"),
            isolate_home=_boolean(raw.get("isolate_home", False), f"workers[{index}].isolate_home"),
            restart=restart,
            env=_env_pairs(raw.get("env", {}), f"workers[{index}].env"),
        )
        if spec.source is not None and ("roadmap" not in spec.only or not spec.roadmap_only):
            raise WorkersError(f"workers[{index}].source requires only to include roadmap and a non-empty roadmap_only")
        if (spec.author_model or spec.author_effort) and spec.agent == "auto":
            raise WorkersError(f"workers[{index}] author_model/author_effort require an explicit agent")
        return spec

    def as_dict(self) -> dict:
        value: dict = {"id": self.id, "enabled": self.enabled}
        if self.agent != "auto":
            value["agent"] = self.agent
        if self.only:
            value["only"] = list(self.only)
        if self.sandbox != "host":
            value["sandbox"] = self.sandbox
        if self.ignore_quota:
            value["ignore_quota"] = True
        if self.auto_refresh:
            value["auto_refresh"] = True
        if self.roadmap_only is not None:
            value["roadmap_only"] = self.roadmap_only
        if self.roadmap_skip:
            value["roadmap_skip"] = list(self.roadmap_skip)
        if self.roadmap_extra_identities:
            value["roadmap_extra_identities"] = list(self.roadmap_extra_identities)
        if not self.respect_claims:
            value["respect_claims"] = False
        for name in ("source", "author_model", "author_effort", "pace"):
            item = getattr(self, name)
            if item is not None:
                value[name] = item
        if self.stream:
            value["stream"] = True
        if self.isolate_home:
            value["isolate_home"] = True
        if self.restart != "always":
            value["restart"] = self.restart
        if self.env:
            value["env"] = dict(self.env)
        return value

    def fingerprint(self) -> str:
        import hashlib

        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def work_argv(self) -> list[str]:
        argv = self_argv("work", "--loop", "--worker-id", self.id)
        if self.only:
            argv += ["--only", ",".join(self.only)]
        if self.agent != "auto":
            argv += ["--agent", self.agent]
        if self.sandbox == "bubble":
            argv.append("--bubble")
        if self.ignore_quota:
            argv.append("--ignore-quota")
        if self.auto_refresh:
            argv.append("--auto-refresh")
        if self.roadmap_only is not None:
            argv += ["--roadmap-only", self.roadmap_only]
        if self.roadmap_skip:
            argv += ["--roadmap-skip", ",".join(self.roadmap_skip)]
        if self.roadmap_extra_identities:
            argv += ["--roadmap-extra-identities", ",".join(self.roadmap_extra_identities)]
        if not self.respect_claims:
            argv.append("--ignore-claims")
        for field, flag in (
            (self.source, "--source"),
            (self.author_model, "--author-model"),
            (self.author_effort, "--author-effort"),
            (self.pace, "--pace"),
        ):
            if field is not None:
                argv += [flag, field]
        if self.stream:
            argv.append("--stream")
        if self.isolate_home:
            argv.append("--isolate-home")
        return argv


def load_worker_specs(path: Path) -> list[WorkerSpec]:
    try:
        with path.open("rb") as src:
            raw = tomllib.load(src)
    except FileNotFoundError:
        raise WorkersError(f"configuration does not exist: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkersError(f"cannot read {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise WorkersError(f"{path} must contain a TOML table")
    unknown = sorted(set(raw) - {"version", "workers"})
    if unknown:
        raise WorkersError(f"unknown top-level field(s): {', '.join(unknown)}")
    if raw.get("version") != CONFIG_VERSION:
        raise WorkersError(f"version must be {CONFIG_VERSION}")
    workers = raw.get("workers", [])
    if not isinstance(workers, list):
        raise WorkersError("workers must be an array of tables")
    specs = [WorkerSpec.from_dict(item, index) for index, item in enumerate(workers)]
    seen: set[str] = set()
    duplicate = next((spec.id for spec in specs if spec.id in seen or seen.add(spec.id)), None)
    if duplicate:
        raise WorkersError(f"duplicate worker id: {duplicate}")
    return specs


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        # An inline table, which is what `env` is. Keys are already constrained to POSIX-portable
        # names, so they need no quoting; values go through the string branch. Written in sorted
        # order so a rewrite of an unchanged definition is byte-identical.
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in sorted(value.items())) + " }"
    raise WorkersError(f"cannot encode TOML value of type {type(value).__name__}")


@contextlib.contextmanager
def _config_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_worker_specs(path: Path, specs: list[WorkerSpec]) -> None:
    lines = ["# Managed by `tauceti workers`; edit while the manager is running and it will reconcile.", "version = 1"]
    for spec in specs:
        lines += ["", "[[workers]]"]
        for key, value in spec.as_dict().items():
            lines.append(f"{key} = {_toml_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def save_worker_specs(path: Path, specs: list[WorkerSpec]) -> None:
    """Atomically replace the desired state.

    CLI/TUI read-modify-write operations hold the same sibling lock across their read. Direct
    callers get an atomic, serialized full-generation replacement here.
    """
    with _config_lock(path):
        _write_worker_specs(path, specs)


def next_worker_id(specs: list[WorkerSpec]) -> str:
    used = {spec.id for spec in specs}
    for number in range(1, 1000):
        candidate = f"worker{number}"
        if candidate not in used:
            return candidate
    raise WorkersError("no free workerN id below worker1000")


def status_path(worker_id: str, state_dir: Path | None = None) -> Path:
    return (state_dir or workers_state_dir()) / f"{worker_id}.json"


def runner_socket(worker_id: str, runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or workers_runtime_dir()) / f"w-{worker_id}.sock"


def manager_socket(runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or workers_runtime_dir()) / "manager.sock"


def _socket_request(path: Path, request: dict, timeout: float = 1.0) -> dict | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks and len(chunks) < 1_000_000:
            data = client.recv(65536)
            if not data:
                break
            chunks.extend(data)
        if not chunks:
            return None
        value = json.loads(bytes(chunks).split(b"\n", 1)[0])
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        client.close()


def manager_request(action: str, **fields) -> dict | None:
    return _socket_request(manager_socket(), {"action": action, **fields})


def _lock_is_held(path: Path) -> bool:
    """Return whether another process owns an advisory lock, without trusting a stored PID."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def runner_status(worker_id: str, state_dir: Path | None = None, runtime_dir: Path | None = None) -> dict:
    live = _socket_request(runner_socket(worker_id, runtime_dir), {"action": "status"}, timeout=0.15)
    if live is not None:
        live["alive"] = True
        return live
    value = read_json(status_path(worker_id, state_dir))
    # A wrapper keeps this lock through its graceful child teardown. Its control socket may be
    # temporarily busy, but the lock still proves that a runner owns the slot.
    value["alive"] = _lock_is_held((runtime_dir or workers_runtime_dir()) / f"w-{worker_id}.lock")
    return value


def worker_snapshots(specs: list[WorkerSpec] | None = None, config: Path | None = None) -> list[dict]:
    specs = load_worker_specs(config or default_workers_config()) if specs is None else specs
    wanted = {spec.id: spec for spec in specs}
    ids = set(wanted)
    state = workers_state_dir()
    try:
        ids.update(path.stem for path in state.glob("*.json"))
    except OSError:
        pass
    snapshots: list[dict] = []
    for wid in sorted(ids):
        spec = wanted.get(wid)
        actual = runner_status(wid)
        alive = bool(actual.get("alive"))
        desired = "running" if spec and spec.enabled else "stopped"
        if alive:
            state_name = str(actual.get("state") or "running")
            if spec is None or not spec.enabled:
                state_name = "stopping"
            elif actual.get("spec_hash") != spec.fingerprint():
                state_name = "restarting"
        elif spec and spec.enabled:
            state_name = "missing" if not actual else str(actual.get("state") or "stale")
        else:
            state_name = str(actual.get("state") or "stopped")
        snapshots.append(
            {
                **actual,
                "id": wid,
                "desired": desired,
                "actual": state_name,
                "alive": alive,
                "enabled": bool(spec and spec.enabled),
                "actual_spec_hash": actual.get("spec_hash"),
                "spec_hash": spec.fingerprint() if spec else None,
                "spec": spec.as_dict() if spec else None,
                "agent": spec.agent if spec else actual.get("agent"),
                "only": list(spec.only) if spec else actual.get("only", []),
                "sandbox": spec.sandbox if spec else actual.get("sandbox"),
            }
        )
    return snapshots


def _encode_spec(spec: WorkerSpec) -> str:
    raw = json.dumps(spec.as_dict(), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_spec(raw: str) -> WorkerSpec:
    try:
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WorkersError(f"invalid managed worker specification: {exc}") from None
    return WorkerSpec.from_dict(value, 0)


def _bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(8)
    server.setblocking(False)
    return server


def _serve_one(server: socket.socket, handler) -> None:
    try:
        conn, _ = server.accept()
    except BlockingIOError:
        return
    with conn:
        conn.settimeout(0.5)
        try:
            request = json.loads(conn.recv(65536).split(b"\n", 1)[0])
            response = handler(request if isinstance(request, dict) else {})
        except (OSError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            conn.sendall(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


def cmd_managed_runner(args) -> int:
    spec = _decode_spec(args.spec)
    state_dir = Path(args.state_dir)
    runtime_dir = Path(args.runtime_dir)
    state = status_path(spec.id, state_dir)
    sock_path = runner_socket(spec.id, runtime_dir)
    lock_path = runtime_dir / f"w-{spec.id}.lock"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 73
        server = _bind_socket(sock_path)
        stopping = False
        child: subprocess.Popen | None = None
        parent_pipe_read: int | None = None
        parent_pipe_write: int | None = None
        started = time.time()
        prior = read_json(state)
        restart_count = int(prior.get("restart_count", 0)) + 1 if prior.get("spec_hash") == spec.fingerprint() else 0
        log_dir = state_dir / "logs" / spec.id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"work-{time.strftime('%Y%m%d-%H%M%S')}.log"

        def stop(_signum=None, _frame=None) -> None:
            nonlocal stopping
            stopping = True

        old_term = signal.signal(signal.SIGTERM, stop)
        old_int = signal.signal(signal.SIGINT, stop)
        try:
            # Per-worker configuration goes UNDER self_env, not over it: self_env derives PYTHONPATH
            # and the CA bundle from what it is given, so a configured NIX_SSL_CERT_FILE takes part in
            # that discovery while the PYTHONPATH the child needs to import itself is still prepended
            # rather than replaced. The manager's own variables are assigned last and are reserved, so
            # nothing here can shadow them.
            env = self_env({**os.environ, **dict(spec.env)})
            env[STATUS_ENV] = str(state)
            env["TAUCETI_MANAGED"] = "1"
            # stderr is already the durable console log; suppress the second log() copy.
            env["TAUCETI_LOG_FILE"] = os.devnull
            logf = log_path.open("ab", buffering=0)
            update_status(
                state,
                id=spec.id,
                state="starting",
                alive=True,
                managed=True,
                wrapper_pid=os.getpid(),
                child_pid=None,
                process_group=None,
                instance=uuid.uuid4().hex,
                spec_hash=spec.fingerprint(),
                agent=spec.agent,
                only=list(spec.only),
                sandbox=spec.sandbox,
                log_file=str(log_path),
                started_at=started,
                heartbeat_at=time.time(),
                activity_at=started,
                restart_count=restart_count,
                exit_code=None,
                stopped_at=None,
            )
            test_command = os.environ.get("TAUCETI_MANAGER_TEST_COMMAND")
            child_argv = shlex.split(test_command) if test_command else spec.work_argv()
            parent_pipe_read, parent_pipe_write = os.pipe()
            os.set_inheritable(parent_pipe_read, True)
            os.set_inheritable(parent_pipe_write, False)
            env["TAUCETI_PARENT_PIPE_FD"] = str(parent_pipe_read)
            child = subprocess.Popen(
                child_argv,
                cwd=HERE,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(parent_pipe_read,),
            )
            os.close(parent_pipe_read)
            parent_pipe_read = None
            update_status(state, child_pid=child.pid, process_group=child.pid)

            def request(req: dict) -> dict:
                nonlocal stopping
                action = req.get("action")
                if action == "status":
                    return read_json(state)
                if action == "stop":
                    stopping = True
                    return {"ok": True}
                return {"ok": False, "error": f"unknown action: {action}"}

            last_heartbeat = 0.0
            while child.poll() is None and not stopping:
                ready, _, _ = select.select([server], [], [], 0.5)
                if ready:
                    _serve_one(server, request)
                if time.monotonic() - last_heartbeat >= 2:
                    update_status(state, alive=True, heartbeat_at=time.time(), child_pid=child.pid)
                    last_heartbeat = time.monotonic()
            if stopping and child.poll() is None:
                update_status(state, state="stopping", activity_at=time.time())
                signal_group(child.pid, signal.SIGTERM)
                deadline = time.monotonic() + 20
                while child.poll() is None and time.monotonic() < deadline:
                    ready, _, _ = select.select([server], [], [], 0.2)
                    if ready:
                        _serve_one(server, request)
                if child.poll() is None:
                    signal_group(child.pid, signal.SIGKILL)
                    child.wait()
            rc = child.wait()
            final = "stopped" if stopping else ("exited" if rc == 0 else "failed")
            update_status(
                state,
                state=final,
                alive=False,
                heartbeat_at=time.time(),
                child_pid=None,
                exit_code=rc,
                stopped_at=time.time(),
            )
            return 0 if stopping else rc
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            server.close()
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass
            if child is not None and child.poll() is None:
                signal_group(child.pid, signal.SIGTERM)
            for fd in (parent_pipe_read, parent_pipe_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


def _stop_runner(worker_id: str, runtime_dir: Path | None = None) -> bool:
    response = _socket_request(runner_socket(worker_id, runtime_dir), {"action": "stop"})
    return bool(response and response.get("ok"))


def _launch_runner(spec: WorkerSpec, state_dir: Path, runtime_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        self_argv(
            "_managed-run",
            "--spec",
            _encode_spec(spec),
            "--state-dir",
            state_dir,
            "--runtime-dir",
            runtime_dir,
        ),
        cwd=HERE,
        env=self_env(),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
    )


def run_manager(config: Path, interval: float = DEFAULT_INTERVAL) -> int:
    runtime_dir = workers_runtime_dir()
    state_dir = workers_state_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / "manager.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            workers_die("another worker manager is already running")
        server_path = manager_socket(runtime_dir)
        server = _bind_socket(server_path)
        exiting = False
        stop_workers = False
        restart_ids: set[str] = set()
        children: dict[str, subprocess.Popen] = {}
        launch_failures: dict[str, tuple[str, int, float]] = {}
        last_good: list[WorkerSpec] | None = None
        last_error: str | None = None

        def stop(signum=None, frame=None) -> None:
            nonlocal exiting, stop_workers
            exiting = True
            stop_workers = True

        old_term = signal.signal(signal.SIGTERM, stop)
        old_int = signal.signal(signal.SIGINT, stop)

        def handle(req: dict) -> dict:
            nonlocal exiting, stop_workers
            action = req.get("action")
            if action in ("ping", "apply"):
                return {"ok": True, "pid": os.getpid(), "config": str(config), "error": last_error}
            if action == "restart":
                wid = req.get("id")
                if not isinstance(wid, str):
                    return {"ok": False, "error": "restart requires id"}
                restart_ids.add(wid)
                return {"ok": True}
            if action == "shutdown":
                exiting = True
                stop_workers = bool(req.get("stop_workers"))
                return {"ok": True}
            return {"ok": False, "error": f"unknown action: {action}"}

        print(f"tauceti workers: managing {config} every {interval:g}s", flush=True)
        try:
            while not exiting:
                try:
                    specs = load_worker_specs(config)
                    last_good = specs
                    last_error = None
                except WorkersError as exc:
                    specs = last_good or []
                    error = str(exc)
                    if error != last_error:
                        print(
                            f"tauceti workers: invalid configuration; keeping last good generation: {error}",
                            file=sys.stderr,
                        )
                    last_error = error
                    # On a fresh manager start there is no last-known-good generation to reconcile.
                    # Leave any surviving managed workers alone until the operator fixes the file.
                    if last_good is None:
                        ready, _, _ = select.select([server], [], [], max(0.1, interval))
                        if ready:
                            _serve_one(server, handle)
                        continue
                wanted = {spec.id: spec for spec in specs}
                for wid, process in list(children.items()):
                    if process.poll() is not None:
                        children.pop(wid, None)
                        status = read_json(status_path(wid, state_dir))
                        terminal = (
                            status.get("wrapper_pid") == process.pid
                            and not status.get("alive")
                            and status.get("stopped_at") is not None
                        )
                        if not terminal:
                            spec = wanted.get(wid)
                            fingerprint = spec.fingerprint() if spec else "removed"
                            previous = launch_failures.get(wid)
                            failures = previous[1] + 1 if previous and previous[0] == fingerprint else 1
                            delay = min(5 * (2 ** min(failures - 1, 6)), 300)
                            launch_failures[wid] = (fingerprint, failures, time.time() + delay)
                            print(
                                f"tauceti workers: wrapper {wid} exited before publishing terminal state; "
                                f"retrying in {delay}s",
                                file=sys.stderr,
                            )
                known_ids = set(wanted)
                try:
                    known_ids.update(path.stem for path in state_dir.glob("*.json"))
                except OSError:
                    pass
                for wid in sorted(known_ids):
                    spec = wanted.get(wid)
                    live = runner_status(wid, state_dir, runtime_dir)
                    is_live = bool(live.get("alive"))
                    should_run = bool(spec and spec.enabled)
                    changed = bool(is_live and spec and live.get("spec_hash") != spec.fingerprint())
                    if (
                        is_live
                        and not changed
                        and int(live.get("restart_count", 0))
                        and time.time() - float(live.get("started_at") or time.time()) >= 300
                    ):
                        update_status(status_path(wid, state_dir), restart_count=0)
                        launch_failures.pop(wid, None)
                    if is_live and (not should_run or changed or wid in restart_ids):
                        _stop_runner(wid, runtime_dir)
                        continue
                    if is_live or not should_run or wid in children:
                        if not is_live and spec is None and live.get("managed"):
                            for stale in (
                                status_path(wid, state_dir),
                                status_path(wid, state_dir).with_suffix(".json.lock"),
                            ):
                                try:
                                    stale.unlink()
                                except FileNotFoundError:
                                    pass
                        continue
                    prior = read_json(status_path(wid, state_dir))
                    forced = wid in restart_ids
                    if (
                        not forced
                        and spec.restart == "never"
                        and prior.get("spec_hash") == spec.fingerprint()
                        and prior.get("stopped_at")
                    ):
                        continue
                    # As for `never` above, a clean exit is only terminal for the generation that
                    # produced it: editing the definition (env, model, phases) must launch the new one,
                    # which is what "the manager restarts an enabled worker when any field changes"
                    # promises. Without the fingerprint test, an `on-failure` worker that exited 0
                    # could never be reconfigured without an explicit restart.
                    if (
                        not forced
                        and spec.restart == "on-failure"
                        and prior.get("spec_hash") == spec.fingerprint()
                        and prior.get("exit_code") == 0
                    ):
                        continue
                    failures = int(prior.get("restart_count", 0))
                    delay = min(5 * (2 ** min(failures, 6)), 300) if prior.get("state") == "failed" else 0
                    manager_failure = launch_failures.get(wid)
                    manager_retry_at = (
                        manager_failure[2] if manager_failure and manager_failure[0] == spec.fingerprint() else 0
                    )
                    if not forced and max(float(prior.get("stopped_at") or 0) + delay, manager_retry_at) > time.time():
                        continue
                    children[wid] = _launch_runner(spec, state_dir, runtime_dir)
                    restart_ids.discard(wid)
                ready, _, _ = select.select([server], [], [], max(0.1, interval))
                if ready:
                    _serve_one(server, handle)
            if stop_workers:
                live_ids = [item["id"] for item in worker_snapshots(last_good or []) if item.get("alive")]
                for wid in live_ids:
                    _stop_runner(wid, runtime_dir)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline and any(
                    runner_status(wid, state_dir, runtime_dir).get("alive") for wid in live_ids
                ):
                    time.sleep(0.1)
            return 0
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            server.close()
            try:
                server_path.unlink()
            except FileNotFoundError:
                pass


def _manager_lock_held() -> bool:
    return _lock_is_held(workers_runtime_dir() / "manager.lock")


def _check_manager_config(reply: dict, config: Path) -> None:
    active = Path(str(reply.get("config", ""))).expanduser().resolve()
    if active != config.resolve():
        raise WorkersError(f"manager already uses {active}, not {config}")


def ensure_manager(config: Path) -> bool:
    """Return only after the requested manager is demonstrably accepting control requests."""
    config = config.expanduser().resolve()
    spawned: subprocess.Popen | None = None
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        online = manager_request("ping")
        if online:
            _check_manager_config(online, config)
            manager_request("apply")
            return spawned is not None
        if not _manager_lock_held():
            if spawned is not None and spawned.poll() is not None:
                raise WorkersError(f"worker manager exited during startup with status {spawned.returncode}")
            if spawned is None:
                state = workers_state_dir()
                state.mkdir(parents=True, exist_ok=True)
                log = (state / "manager.log").open("ab", buffering=0)
                try:
                    spawned = subprocess.Popen(
                        self_argv("workers", "--config", config, "manager"),
                        cwd=HERE,
                        env=self_env(),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                finally:
                    log.close()
        time.sleep(0.1)
    raise WorkersError("worker manager did not become responsive within 35 seconds")


def wait_manager_stopped(timeout: float = 35) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _manager_lock_held():
            return
        time.sleep(0.1)
    raise WorkersError(f"worker manager did not stop within {timeout:g} seconds")


def _format_age(value) -> str:
    try:
        seconds = max(0, int(time.time() - float(value)))
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _status_field(label: str, values: list[str], width: int) -> list[str]:
    """Format one human-facing status field with aligned wrapped continuations."""
    prefix = f"  {label + ':':<10}"
    continuation = " " * len(prefix)
    lines: list[str] = []
    for value in values:
        lines.extend(
            textwrap.wrap(
                value,
                width=max(width, len(prefix) + 20),
                initial_indent=prefix if not lines else continuation,
                subsequent_indent=continuation,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [prefix.rstrip()]
        )
    return lines


def _format_until(value) -> str:
    try:
        seconds = max(0, int(float(value) - time.time()))
    except (TypeError, ValueError):
        return "—"
    if seconds == 0:
        return "now"
    if seconds < 60:
        return f"in {seconds}s"
    if seconds < 3600:
        return f"in {(seconds + 59) // 60}m"
    return f"in {(seconds + 3599) // 3600}h"


def _runtime_summary(detail: str) -> str:
    """Turn structured key=value launch metadata into a short human sentence."""
    parts = [part.split("=", 1) for part in detail.split(", ")]
    if not parts or any(len(part) != 2 for part in parts):
        return detail
    fields = dict(parts)
    if "provider" not in fields:
        return detail
    values = [fields.pop("provider")]
    model = fields.pop("model", None)
    if model and model != values[0]:
        values.append(model)
    effort = fields.pop("effort", None)
    if effort and effort != "none":
        values.append(f"{effort} effort")
    # The desired sandbox is already shown alongside the configured agent.
    fields.pop("sandbox", None)
    values.extend(f"{key} {value}" for key, value in fields.items())
    return " · ".join(values)


def _legacy_backoff_reason(item: dict) -> str | None:
    """Recover the useful error tail written before old loops reduced it to ``rc=N``."""
    detail = str(item.get("detail") or "")
    if not re.fullmatch(r"(?:rc=\d+|no progress|timed out)", detail):
        return detail or None
    log_file = item.get("log_file")
    if not log_file:
        return None
    try:
        with Path(log_file).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    marker = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if re.search(r"tauceti: round (?:rc=\d+|timed out|no progress)", lines[index])
        ),
        None,
    )
    if marker is None:
        return None
    summary = next(
        (
            lines[index].strip()
            for index in range(marker - 1, max(-1, marker - 25), -1)
            if lines[index].startswith("    ")
        ),
        None,
    )
    if not summary:
        return None
    provider = next(
        (
            match.group(1)
            for index in range(marker - 1, max(-1, marker - 25), -1)
            if (match := re.search(r"agent-([^:]+): exited", lines[index]))
        ),
        None,
    )
    prefix = f"{provider} agent: " if provider else ""
    return prefix + summary[-800:]


def _backoff_reason(item: dict) -> str:
    reason = item.get("failure_reason") or _legacy_backoff_reason(item)
    if reason:
        return str(reason)
    detail = str(item.get("detail") or "")
    match = re.fullmatch(r"rc=(\d+)", detail)
    return f"round exited with status {match.group(1)}" if match else (detail or "unknown failure")


def _worker_configuration_lines(item: dict, width: int) -> list[str]:
    """Render the desired worker definition independently of its transient runtime state."""
    raw = item.get("spec")
    spec = raw if isinstance(raw, dict) else {}
    selected = spec.get("only") if isinstance(spec.get("only"), list) else item.get("only")
    phases = [str(phase) for phase in selected] if selected else list(ALLOWED_TASKS)
    lines = _status_field("phases", [", ".join(phases)], width)

    agent = str(spec.get("agent") or item.get("agent") or "auto")
    sandbox = str(spec.get("sandbox") or item.get("sandbox") or "host")
    lines.extend(_status_field("agent", [f"{agent} · {sandbox} sandbox"], width))

    pacing = "ignored (--ignore-quota; hard limits still apply)" if spec.get("ignore_quota") else "normal"
    if spec.get("pace"):
        pacing += f" · curve {spec['pace']}"
    lines.extend(_status_field("pacing", [pacing], width))

    # Worth stating rather than leaving implicit: this worker rotates the operator's Claude credential.
    if spec.get("auto_refresh"):
        lines.extend(_status_field("credential", ["renews its own Claude access token (--auto-refresh)"], width))

    if "roadmap" in phases:
        focus = spec.get("roadmap_only")
        roadmap = "auto (random each round)" if focus is None else ("all areas" if focus == "" else str(focus))
        roadmap_values = [roadmap]
        skips = spec.get("roadmap_skip")
        if isinstance(skips, list) and skips:
            roadmap_values.append("skip: " + ", ".join(map(str, skips)))
        identities = spec.get("roadmap_extra_identities")
        if isinstance(identities, list) and identities:
            roadmap_values.append("also treat as self: " + ", ".join(map(str, identities)))
        if spec.get("respect_claims", True) is False:
            roadmap_values.append("claims: ignored")
        lines.extend(_status_field("roadmap", roadmap_values, width))

    if spec.get("source"):
        lines.extend(_status_field("source", [str(spec["source"])], width))

    authoring = []
    if spec.get("author_model"):
        authoring.append(f"model {spec['author_model']}")
    if spec.get("author_effort"):
        authoring.append(f"{spec['author_effort']} effort")
    if authoring:
        lines.extend(_status_field("author", [" · ".join(authoring)], width))

    options = []
    if spec.get("stream"):
        options.append("stream output")
    if spec.get("isolate_home"):
        options.append("isolated home")
    if spec.get("restart") and spec["restart"] != "always":
        options.append(f"restart: {spec['restart']}")
    if options:
        lines.extend(_status_field("options", options, width))
    # Show extra environment: a worker configured differently from its peers is otherwise
    # indistinguishable in the status output, and that is exactly when it matters (an A/B of a build
    # setting, a one-worker experiment). Names only — the values are in workers.toml, and printing
    # them onto a shared terminal or into a pasted bug report earns nothing.
    if isinstance(spec.get("env"), dict) and spec["env"]:
        lines.extend(_status_field("env", [", ".join(sorted(spec["env"]))], width))
    return lines


def _worker_status_lines(config: Path, snapshots: list[dict], online: bool, *, width: int) -> list[str]:
    lines = [f"manager: {'running' if online else 'offline'}", f"config:  {config}"]
    if not snapshots:
        return [*lines, "", "workers: none"]

    for item in snapshots:
        state = str(item["actual"])
        display_state = {
            "backoff": "backing off",
            "checking-quota": "checking quota",
            "waiting-github": "waiting for GitHub",
            "waiting-quota": "waiting for quota",
        }.get(state, state)
        heading = f"{item['id']} — {display_state}"
        if item["desired"] == "stopped" or state in {"missing", "restarting", "stale", "stopped", "stopping"}:
            heading += f" (desired: {item['desired']})"
        lines.extend(["", heading])
        lines.extend(_worker_configuration_lines(item, width))
        phase = item.get("phase")
        if phase:
            work_values = [str(phase)]
            target = item.get("target")
            if target:
                # Round targets deliberately separate a human label and its URL with two spaces.
                work_values.extend(part for part in target.split("  ") if part)
            lines.extend(_status_field("work", work_values, width))
        age = _format_age(item.get("activity_at"))
        lines.extend(_status_field("activity", [f"{age} ago" if age != "—" else age], width))
        detail = item.get("detail")
        if state == "backoff":
            lines.extend(_status_field("reason", [_backoff_reason(item)], width))
        elif detail:
            # Quota summaries use three spaces between providers. Keeping each provider on its own
            # continuation line makes the bottleneck legible without coupling this view to quota types.
            detail_values = [part for part in detail.split("   ") if part]
            label = "quota" if state == "waiting-quota" else ("runtime" if state == "running" else "status")
            if state == "running":
                detail_values = [_runtime_summary(part) for part in detail_values]
            lines.extend(_status_field(label, detail_values, width))
        if item.get("next_action_at") and state in {"backoff", "waiting-github", "waiting-quota"}:
            label = "retry" if state == "backoff" else "recheck"
            lines.extend(_status_field(label, [_format_until(item["next_action_at"])], width))
        if state == "backoff":
            lines.extend(_status_field("logs", [f"tauceti workers logs {item['id']}"], width))
    return lines


def print_worker_status(config: Path, *, as_json: bool = False) -> bool:
    specs = load_worker_specs(config)
    snapshots = worker_snapshots(specs)
    online = manager_request("ping")
    if as_json:
        print(json.dumps({"manager": online, "config": str(config), "workers": snapshots}, indent=2))
    else:
        width = shutil.get_terminal_size(fallback=(100, 24)).columns
        print("\n".join(_worker_status_lines(config, snapshots, bool(online), width=width)))
    healthy = bool(online) and all(item["desired"] == "stopped" or item.get("alive") for item in snapshots)
    return healthy


def _mutate_enabled(config: Path, wid: str, enabled: bool) -> None:
    with _config_lock(config):
        specs = load_worker_specs(config)
        if not any(spec.id == wid for spec in specs):
            raise WorkersError(f"unknown worker: {wid}")
        _write_worker_specs(
            config, [dataclasses.replace(spec, enabled=enabled) if spec.id == wid else spec for spec in specs]
        )
    if enabled:
        # Re-enabling is an explicit request to run, even for restart="never" or after a clean
        # on-failure exit. Clear the terminal policy record after persisting desired state.
        update_status(status_path(wid), stopped_at=None, exit_code=None, state="queued")


def set_worker_enabled(config: Path, wid: str, enabled: bool) -> None:
    _mutate_enabled(config, wid, enabled)
    ensure_manager(config)


def restart_worker(config: Path, wid: str) -> None:
    specs = load_worker_specs(config)
    if not any(spec.id == wid for spec in specs):
        raise WorkersError(f"unknown worker: {wid}")
    ensure_manager(config)
    response = manager_request("restart", id=wid)
    if not response or not response.get("ok"):
        raise WorkersError("worker manager did not accept the restart request")


def _remove_spec(config: Path, wid: str) -> None:
    with _config_lock(config):
        specs = load_worker_specs(config)
        if not any(spec.id == wid for spec in specs):
            raise WorkersError(f"unknown worker: {wid}")
        _write_worker_specs(config, [spec for spec in specs if spec.id != wid])


def add_dashboard_worker(
    config: Path, *, only: str | None, agent: str, bubble: bool, roadmap_only: str | None, roadmap_skip: str | None
) -> WorkerSpec:
    with _config_lock(config):
        try:
            specs = load_worker_specs(config)
        except WorkersError:
            if config.exists():
                raise
            specs = []
        spec = WorkerSpec(
            id=next_worker_id(specs),
            agent=agent,
            only=(only,) if only else (),
            sandbox="bubble" if bubble else "host",
            roadmap_only=roadmap_only,
            roadmap_skip=tuple(part.strip() for part in (roadmap_skip or "").split(",") if part.strip()),
        )
        _write_worker_specs(config, [*specs, spec])
    ensure_manager(config)
    return spec


def _follow_log(worker_id: str, initial_path: Path, lines: int, follow: bool) -> int:
    """Tail a worker across wrapper restarts, switching when status names a new durable log."""
    current = initial_path
    src = None
    try:
        while True:
            raw = read_json(status_path(worker_id)).get("log_file")
            candidate = Path(raw) if isinstance(raw, str) and raw else current
            if candidate != current or src is None:
                if not candidate.exists():
                    if not follow:
                        workers_die(f"log does not exist: {candidate}")
                    time.sleep(0.25)
                    continue
                if src is not None:
                    src.close()
                    print(f"\n--- {worker_id} restarted; following {candidate} ---", flush=True)
                current = candidate
                src = current.open(errors="replace")
                content = src.readlines()
                sys.stdout.writelines(content[-lines:])
                sys.stdout.flush()
                if not follow:
                    return 0
            chunk = src.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                time.sleep(0.25)
    finally:
        if src is not None:
            src.close()


def _tmux_shell(argv: list[str]) -> str:
    env = self_env()
    assignments = [
        f"PYTHONPATH={env['PYTHONPATH']}",
        f"TAUCETI_WORKERS_STATE_DIR={workers_state_dir()}",
        f"TAUCETI_RUNTIME_DIR={workers_runtime_dir()}",
    ]
    return shlex.join([shutil.which("env") or "/usr/bin/env", *assignments, *argv])


def cmd_tmux(config: Path, attach: bool) -> int:
    tmux = shutil.which("tmux")
    if tmux is None:
        workers_die("tmux is not installed; it is optional and workers continue without it")
    specs = load_worker_specs(config)
    exists = subprocess.run([tmux, "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0
    if not exists:
        subprocess.run(
            [
                tmux,
                "new-session",
                "-d",
                "-s",
                TMUX_SESSION,
                "-n",
                "dashboard",
                _tmux_shell(self_argv("workers", "--config", config, "status", "--watch")),
            ],
            check=True,
        )
        subprocess.run(
            [tmux, "set-window-option", "-t", f"{TMUX_SESSION}:dashboard", "@tauceti-managed", "dashboard"],
            check=True,
        )
    result = subprocess.run(
        [tmux, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}\t#{@tauceti-managed}"],
        capture_output=True,
        text=True,
        check=True,
    )
    managed = {}
    for line in result.stdout.splitlines():
        name, _, marker = line.partition("\t")
        managed[name] = marker
    windows = set(managed)
    wanted = {spec.id for spec in specs if spec.enabled}
    for spec in specs:
        name = spec.id
        if spec.enabled and name not in windows:
            subprocess.run(
                [
                    tmux,
                    "new-window",
                    "-d",
                    "-t",
                    TMUX_SESSION,
                    "-n",
                    name,
                    _tmux_shell(self_argv("workers", "--config", config, "logs", spec.id, "--follow")),
                ],
                check=True,
            )
            subprocess.run(
                [tmux, "set-window-option", "-t", f"{TMUX_SESSION}:{name}", "@tauceti-managed", "worker"],
                check=True,
            )
    for name in sorted(name for name, marker in managed.items() if marker == "worker" and name not in wanted):
        subprocess.run([tmux, "kill-window", "-t", f"{TMUX_SESSION}:{name}"], check=False)
    if attach:
        action = "switch-client" if os.environ.get("TMUX") else "attach-session"
        os.execvp(tmux, [tmux, action, "-t", TMUX_SESSION])
    print(f"tmux session {TMUX_SESSION!r} is ready")
    return 0


def _service_command(config: Path) -> list[str]:
    entry = entry_cmd()
    if len(entry) == 1 and not Path(entry[0]).is_absolute():
        entry[0] = shutil.which(entry[0]) or entry[0]
    return [*entry, "workers", "--config", str(config), "manager"]


def _systemd_quote(value: str) -> str:
    return json.dumps(value.replace("%", "%%"))


def _service_environment() -> dict[str, str]:
    env = self_env()
    service_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPATH": env["PYTHONPATH"],
    }
    runtime_bundle = ensure_ssl_cert_file({})
    configured = os.environ.get("SSL_CERT_FILE")
    advertised = os.environ.get("NIX_SSL_CERT_FILE")
    advertised_path = None
    if advertised:
        try:
            candidate = Path(advertised).expanduser()
            if candidate.is_file():
                advertised_path = str(candidate)
        except (OSError, RuntimeError):
            pass
    if configured and configured not in (runtime_bundle, advertised_path):
        # Explicit operator configuration wins, including an unusual or currently absent path.
        service_env["SSL_CERT_FILE"] = configured
    elif advertised_path:
        # The unit may outlive a Nix store path. Preserve it as a candidate, not as the final
        # SSL_CERT_FILE, so cli_main revalidates it and can fall back or warn after collection.
        service_env["NIX_SSL_CERT_FILE"] = advertised_path
    return service_env


def _systemd_unit(config: Path) -> str:
    command = " ".join(_systemd_quote(part) for part in _service_command(config))
    # Assignment values are not shell syntax: quotes become literal in WorkingDirectory=. Escape the
    # small set systemd treats specially while keeping the path absolute after parsing.
    working_directory = str(HERE).replace("\\", "\\\\").replace(" ", "\\x20").replace("%", "%%")
    environment = "\n".join(
        f"Environment={_systemd_quote(name + '=' + value)}" for name, value in _service_environment().items()
    )
    return f"""[Unit]
Description=Keep configured Tau Ceti workers running

[Service]
Type=simple
WorkingDirectory={working_directory}
ExecStart={command}
{environment}
Restart=always
RestartSec=5s
TimeoutStopSec=30s

[Install]
WantedBy=default.target
"""


def _launchd_label() -> str:
    return "org.tauceti.workers"


def _service_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{_launchd_label()}.plist"
    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "systemd" / "user" / "tauceti-workers.service"


def service_action(action: str, config: Path) -> int:
    path = _service_path()
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{_launchd_label()}"
        if action in ("install", "start", "restart"):
            probe = subprocess.run(["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode:
                raise WorkersError("macOS LaunchAgents require an active graphical login session")
        if action == "install":
            path.parent.mkdir(parents=True, exist_ok=True)
            workers_state_dir().mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": _launchd_label(),
                "ProgramArguments": _service_command(config),
                "WorkingDirectory": str(HERE),
                "RunAtLoad": True,
                "KeepAlive": True,
                "EnvironmentVariables": _service_environment(),
                "StandardOutPath": str(workers_state_dir() / "manager.log"),
                "StandardErrorPath": str(workers_state_dir() / "manager.log"),
            }
            if path.is_symlink():
                path.unlink()
            path.write_bytes(plistlib.dumps(payload))
            subprocess.run(["launchctl", "bootout", service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
            print(f"installed and started {path}")
            return 0
        if action == "uninstall":
            subprocess.run(["launchctl", "bootout", service], check=False)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return 0
        if action == "status":
            return subprocess.run(["launchctl", "print", service]).returncode
        if action in ("stop", "restart"):
            result = subprocess.run(["launchctl", "bootout", service])
            if action == "stop":
                return result.returncode
        # Bootstrap (rather than kickstart) also restores a service previously booted out by `stop`.
        return subprocess.run(["launchctl", "bootstrap", domain, str(path)]).returncode
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        workers_die("systemctl is unavailable; run `tauceti workers manager` directly")
    if action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        path.write_text(_systemd_unit(config))
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True)
        subprocess.run([systemctl, "--user", "enable", "--now", path.name], check=True)
        print(f"installed and started {path}")
        return 0
    if action == "uninstall":
        subprocess.run([systemctl, "--user", "disable", "--now", path.name], check=False)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True)
        return 0
    return subprocess.run([systemctl, "--user", action, path.name]).returncode


def parse_legacy_config(path: Path) -> list[WorkerSpec]:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise WorkersError(f"cannot read {path}: {exc}") from None
    specs: list[WorkerSpec] = []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        argv = shlex.split(line)
        if len(argv) < 3 or Path(argv[0]).name != "tauceti" or argv[1] != "work" or "--loop" not in argv:
            raise WorkersError(f"{path}:{number}: expected a `tauceti work --loop` command")
        values: dict = {"only": []}
        flags = iter(argv[2:])
        for token in flags:
            if token in ("--loop", "--host"):
                continue
            if token in ("--ignore-quota", "--auto-refresh", "--stream", "--isolate-home", "--bubble"):
                values[
                    {
                        "--ignore-quota": "ignore_quota",
                        "--auto-refresh": "auto_refresh",
                        "--stream": "stream",
                        "--isolate-home": "isolate_home",
                        "--bubble": "bubble",
                    }[token]
                ] = True
                continue
            if token == "--ignore-claims":
                values["respect_claims"] = False
                continue
            try:
                value = next(flags)
            except StopIteration:
                raise WorkersError(f"{path}:{number}: {token} needs a value") from None
            key = {
                "--worker-id": "id",
                "--agent": "agent",
                "--only": "only",
                "--roadmap-only": "roadmap_only",
                "--roadmap-skip": "roadmap_skip",
                "--roadmap-extra-identities": "roadmap_extra_identities",
                "--source": "source",
                "--author-model": "author_model",
                "--author-effort": "author_effort",
                "--pace": "pace",
            }.get(token)
            if key is None:
                raise WorkersError(f"{path}:{number}: unsupported legacy argument {token}")
            values[key] = value.split(",") if key in ("only", "roadmap_skip", "roadmap_extra_identities") else value
        if not values.get("id"):
            raise WorkersError(f"{path}:{number}: persistent workers need an explicit --worker-id")
        if values.pop("bubble", False):
            values["sandbox"] = "bubble"
        specs.append(WorkerSpec.from_dict(values, number))
    return specs


def add_workers_parser(subparsers) -> None:
    workers = subparsers.add_parser(
        "workers",
        help="configure, supervise, and monitor persistent workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Configure and inspect persistent workers declared in workers.toml. A manager\n"
        "reconciles the file, starting, stopping, and restarting workers to match it. The\n"
        "workers outlive the terminal that created them. With no action, show desired and\n"
        "actual state.",
        epilog=WORKERS_EPILOG,
    )
    workers.add_argument(
        "--config", type=Path, default=default_workers_config(), help="workers.toml path (default: discovered config)"
    )
    workers.set_defaults(json=False, watch=False)
    actions = workers.add_subparsers(dest="workers_action")
    status = actions.add_parser("status", help="show desired and actual worker state")
    status.add_argument("--json", action="store_true", help="emit the state as JSON")
    status.add_argument("--watch", action="store_true", help="refresh every two seconds until interrupted")
    apply = actions.add_parser("apply", help="validate and reconcile the configuration")
    apply.add_argument("--check", action="store_true", help="validate only; do not contact or start the manager")
    descriptions = {
        "enable": "persist desired running state",
        "disable": "persist desired stopped state",
        "restart": "restart one worker without changing desired state",
        "remove": "remove a worker definition and stop it",
    }
    for action in ("enable", "disable", "restart", "remove"):
        item = actions.add_parser(action, help=descriptions[action])
        item.add_argument("worker_id", help="worker id")
    add = actions.add_parser("add", help="add an enabled persistent worker definition")
    add.add_argument("worker_id", nargs="?", help="stable id (default: next free workerN)")
    add.add_argument("--agent", choices=AGENTS, default="auto", help="agent for each round (default: auto)")
    add.add_argument("--only", default="", help="comma-separated work phases (default: full cascade)")
    add.add_argument(
        "--sandbox", choices=("host", "bubble"), default="host", help="where eligible phases run (default: host)"
    )
    add.add_argument(
        "--ignore-quota",
        action="store_true",
        help="ignore soft pacing for explicit codex/claude; Kiro/OpenRouter are already unpaced",
    )
    add.add_argument(
        "--auto-refresh",
        action="store_true",
        help="renew this worker's Claude access token when it expires, instead of parking until a "
        "human runs `claude`. Only safe when nothing else uses the same credential file "
        "(see `tauceti work --help`)",
    )
    add.add_argument("--roadmap-only", help="pin roadmap rounds to one area")
    add.add_argument("--roadmap-skip", default="", help="comma-separated roadmap areas to exclude")
    add.add_argument("--source", help="source repository; requires roadmap in --only and one pinned roadmap area")
    add.add_argument("--author-model", help="exact authoring model; needs an explicit --agent")
    add.add_argument("--author-effort", help="reasoning effort for an explicit codex/claude/kiro agent")
    add.add_argument("--pace", help="soft pacing curve as time%%:budget%% points, e.g. 0:10,50:70,90:90")
    add.add_argument("--stream", action="store_true", help="keep the agent transcript in the console log")
    add.add_argument(
        "--isolate-home",
        action="store_true",
        help="force credential isolation for id 'default'; other ids are isolated automatically",
    )
    logs = actions.add_parser("logs", help="show a worker's durable console log")
    logs.add_argument("worker_id", help="worker id")
    logs.add_argument("--follow", "-f", action="store_true", help="keep printing, across worker restarts")
    logs.add_argument("--lines", type=int, default=100, help="initial trailing line count (default: 100)")
    tmux = actions.add_parser("tmux", help="open an optional tmux workspace that tails worker logs")
    tmux.add_argument("--no-attach", action="store_true", help="build the session but stay in this shell")
    manager = actions.add_parser("manager", help="run the portable desired-state reconciler")
    manager.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between reconciliations (default: 2)"
    )
    stop = actions.add_parser("manager-stop", help="stop the detached manager and, by default, its workers")
    stop.add_argument(
        "--leave-workers", action="store_true", help="leave workers running for another manager to take over"
    )
    service = actions.add_parser("service", help="manage the systemd user service or macOS LaunchAgent")
    service.add_argument(
        "service_action",
        choices=("install", "uninstall", "start", "stop", "restart", "status"),
        help="service operation",
    )
    edit = actions.add_parser("edit", help="open workers.toml in $VISUAL or $EDITOR")
    edit.add_argument("--editor", help="editor command to use instead of $VISUAL or $EDITOR")
    imp = actions.add_parser("import", help="import legacy workers.conf commands")
    imp.add_argument("legacy", type=Path, help="legacy workers.conf path")
    imp.add_argument("--force", action="store_true", help="overwrite an existing workers.toml")


def cmd_workers(args) -> int:
    action = args.workers_action or "status"
    config = args.config.expanduser().resolve()
    try:
        if action == "status":
            while True:
                try:
                    healthy = print_worker_status(config, as_json=args.json)
                except WorkersError as exc:
                    if not args.watch:
                        raise
                    healthy = False
                    print(f"tauceti workers: {exc}", file=sys.stderr)
                if not args.watch:
                    return 0 if healthy else 1
                time.sleep(2)
                if sys.stdout.isatty():
                    print("\033[H\033[2J", end="")
        if action == "apply":
            specs = load_worker_specs(config)
            print(f"valid: {len(specs)} worker(s) in {config}")
            if not args.check:
                ensure_manager(config)
            return 0
        if action in ("enable", "disable"):
            _mutate_enabled(config, args.worker_id, action == "enable")
            ensure_manager(config)
            return 0
        if action == "add":
            with _config_lock(config):
                try:
                    specs = load_worker_specs(config)
                except WorkersError:
                    if config.exists():
                        raise
                    specs = []
                wid = args.worker_id or next_worker_id(specs)
                if any(spec.id == wid for spec in specs):
                    raise WorkersError(f"worker already exists: {wid}")
                raw = {
                    "id": wid,
                    "agent": args.agent,
                    "only": [item for item in args.only.split(",") if item],
                    "sandbox": args.sandbox,
                    "ignore_quota": args.ignore_quota,
                    "auto_refresh": args.auto_refresh,
                    "roadmap_skip": [item for item in args.roadmap_skip.split(",") if item],
                    "stream": args.stream,
                    "isolate_home": args.isolate_home,
                }
                for key in ("roadmap_only", "source", "author_model", "author_effort", "pace"):
                    value = getattr(args, key)
                    if value is not None:
                        raw[key] = value
                spec = WorkerSpec.from_dict(raw, len(specs))
                _write_worker_specs(config, [*specs, spec])
            ensure_manager(config)
            print(f"added {spec.id}")
            return 0
        if action == "remove":
            _remove_spec(config, args.worker_id)
            ensure_manager(config)
            return 0
        if action == "restart":
            restart_worker(config, args.worker_id)
            return 0
        if action == "logs":
            item = next((item for item in worker_snapshots(config=config) if item["id"] == args.worker_id), None)
            if item is None:
                raise WorkersError(f"unknown worker: {args.worker_id}")
            raw = item.get("log_file")
            if not raw:
                raise WorkersError(f"worker {args.worker_id} has no log yet")
            return _follow_log(args.worker_id, Path(raw), args.lines, args.follow)
        if action == "tmux":
            return cmd_tmux(config, not args.no_attach)
        if action == "manager":
            return run_manager(config, args.interval)
        if action == "manager-stop":
            response = manager_request("shutdown", stop_workers=not args.leave_workers)
            if not response:
                workers_die("manager is offline")
            wait_manager_stopped()
            return 0
        if action == "service":
            try:
                return service_action(args.service_action, config)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise WorkersError(f"service {args.service_action} failed: {exc}") from None
        if action == "edit":
            if not config.exists():
                save_worker_specs(config, [])
            editor = args.editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            argv = shlex.split(editor) + [str(config)]
            return subprocess.run(argv).returncode
        if action == "import":
            if config.exists() and not args.force:
                raise WorkersError(f"refusing to overwrite {config}; pass --force")
            specs = parse_legacy_config(args.legacy)
            save_worker_specs(config, specs)
            print(f"imported {len(specs)} worker(s) into {config}")
            return 0
    except WorkersError as exc:
        workers_die(str(exc))
    return 64

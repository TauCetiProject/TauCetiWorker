"""Optional native worker containment. Failure never prevents a worker launch."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

SLICE_ENV = "TAUCETI_WORKER_SLICE"


def enabled() -> bool:
    return sys.platform == "linux" and bool(os.environ.get(SLICE_ENV))


def prefix(runtime_dir: Path, worker_id: str = "") -> str:
    namespace = hashlib.sha256(str(runtime_dir.resolve()).encode()).hexdigest()[:16]
    worker = hashlib.sha256(worker_id.encode()).hexdigest()[:16] + "-" if worker_id else ""
    return f"tauceti-worker-{namespace}-{worker}"


def cgroup(pid: int | str = "self") -> Path:
    relative = Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].splitlines()[0]
    return Path("/sys/fs/cgroup") / relative.lstrip("/")


def enter(worker_id: str, runtime_dir: Path) -> dict:
    """Move this wrapper, before it has children, into a transient scope.

    Registering our own PID avoids a second launcher, exit-code translation, or
    an ambiguous fallback that might start duplicate work. Even a late D-Bus
    reply can only move this same process; it cannot create another worker.
    """
    if not enabled():
        return {"scope_unit": None, "isolation_error": None}
    name = prefix(runtime_dir, worker_id) + uuid.uuid4().hex + ".scope"
    try:
        result = subprocess.run(
            [
                "busctl",
                "--user",
                "--timeout=2",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "StartTransientUnit",
                "ssa(sv)a(sa(sv))",
                name,
                "fail",
                "6",
                "PIDs",
                "au",
                "1",
                str(os.getpid()),
                "Slice",
                "s",
                os.environ[SLICE_ENV],
                "Description",
                "s",
                f"TauCeti worker {worker_id}",
                "CollectMode",
                "s",
                "inactive-or-failed",
                "OOMPolicy",
                "s",
                "continue",
                "TimeoutStopUSec",
                "t",
                "30000000",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode:
            raise OSError(f"scope registration exited {result.returncode}: {result.stderr.strip()[-500:]}")
        # StartTransientUnit returns a queued job, not its completion. Only this
        # wrapper waits, briefly; the manager and all other workers keep running.
        deadline = time.monotonic() + 2
        while True:
            if cgroup().name == name:
                return {"scope_unit": name, "isolation_error": None}
            if time.monotonic() >= deadline:
                raise OSError("scope registration did not complete within 2s")
            time.sleep(0.01)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        error = f"worker {worker_id}: {exc}; continuing with existing process containment"
        print(f"tauceti workers: {error}", file=sys.stderr, flush=True)
        return {"scope_unit": None, "isolation_error": error}


class Scopes:
    """Discover only this runtime's scopes; cleanup uses no status receipts or bus waits."""

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.root = None
        if enabled():
            try:
                current = cgroup()
                self.root = next(p for p in (current, *current.parents) if p.name == os.environ[SLICE_ENV])
            except (OSError, ValueError, IndexError, StopIteration):
                # A non-Fleet caller may not itself be in the selected slice.
                # Losing this optional discovery must not prevent reconciliation.
                print("tauceti workers: cannot locate worker slice for scope cleanup", file=sys.stderr)

    def units(self, worker_id: str = "") -> list[Path]:
        if self.root is None:
            return []
        try:
            return list(self.root.glob(prefix(self.runtime_dir, worker_id) + "*.scope"))
        except OSError:
            return []

    @staticmethod
    def kill(units: list[Path]) -> None:
        for unit in units:
            try:
                # cgroup.kill includes detached sessions and nested descendants.
                # It queues SIGKILL without waiting for unit garbage collection.
                (unit / "cgroup.kill").write_text("1")
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"tauceti workers: scope cleanup {unit.name}: {exc}", file=sys.stderr)

    def cleanup(self, worker_id: str, *, force: bool = False) -> None:
        if self.root is None:
            return
        if force:
            self.kill(self.units(worker_id))
            return
        try:
            # Coordinate with the wrapper's existing instance lock. A wrapper
            # can start between a status probe and this sweep (e.g. after manager
            # adoption); never mistake its newly created scope for an orphan.
            with (self.runtime_dir / f"w-{worker_id}.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                self.kill(self.units(worker_id))
        except OSError as exc:
            print(f"tauceti workers: cannot sweep scopes for {worker_id}: {exc}", file=sys.stderr)

    def cleanup_unknown(self, known_ids: set[str]) -> None:
        known = tuple(prefix(self.runtime_dir, wid) for wid in known_ids)
        self.kill([p for p in self.units() if not p.name.startswith(known)])


def collect_oom_events(config: Path, since: float) -> None:
    """Bounded observations on the existing activity thread, never the supervisor loop."""
    if not enabled():
        return
    from . import activity, worker_manager
    from .runtime_status import atomic_json, read_json

    state = worker_manager.workers_state_dir()
    target = state / "diagnostics" / "isolation.json"
    prior = read_json(target)
    events = {e["id"]: e for e in prior.get("events", [])}
    try:
        runtime = worker_manager.workers_runtime_dir()
        specs = worker_manager.load_worker_specs(config)
        result = subprocess.run(
            [
                "journalctl",
                "--unit=systemd-oomd.service",
                f"--since=@{since:.6f}",
                "--lines=128",
                "--output=json",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        for line in result.stdout.splitlines():
            record = json.loads(line)
            message = record.get("MESSAGE", "")
            # Parse the native oomd diagnostic, never model prose. Preserve its
            # full evidence when an installed version uses a different format.
            if " for killing due to " not in message or not message.startswith("Marked "):
                continue
            victim, _, reason = message.removeprefix("Marked ").partition(" for killing due to ")
            if os.environ[SLICE_ENV] not in victim.split("/"):
                continue
            unit = victim.rsplit("/", 1)[-1]
            affected = [s.id for s in specs if unit.startswith(prefix(runtime, s.id))]
            if unit == "tauceti-fleet-workers.service":
                affected = [s.id for s in specs]
            ancestor = reason.removeprefix("memory pressure for ").partition(" being ")[0]
            identity = record["__CURSOR"]
            event = dict(
                id=identity,
                at=int(record["__REALTIME_TIMESTAMP"]) / 1_000_000,
                unit=unit,
                workers=affected,
                ancestor=ancestor,
                evidence=message,
            )
            events[identity] = event
            activity.emit("worker_oom_kill", key=identity, **{k: v for k, v in event.items() if k != "id"})
        recent = sorted(events.values(), key=lambda e: e["at"])[-20:]
        for event in recent:
            event["replacement_alive"] = {
                wid: bool(
                    (s := worker_manager.runner_status(wid)).get("alive") and s.get("scope_unit") != event["unit"]
                )
                for wid in event["workers"]
            }
        atomic_json(target, dict(observed_at=time.time(), events=recent, error=None))
    except Exception as exc:
        # No journal or diagnostics failure may influence recovery or admission.
        try:
            atomic_json(target, {**prior, "observed_at": time.time(), "error": type(exc).__name__})
        except OSError:
            pass

"""Host process resource accounting for a work round, including its subprocesses."""

from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
import uuid
from pathlib import Path

from .runtime_status import atomic_json, read_json, report_runtime


def read_bytes(pid: int) -> int | None:
    """Kernel storage reads, including children already reaped by this process."""
    try:
        values = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/io").read_text().splitlines())
        return int(values["read_bytes"])
    except (OSError, ValueError, KeyError):
        return None


def pressure(directory: Path) -> dict:
    """Cumulative stall counters for this scope, including its descendants."""
    result = {}
    for name in ("cpu", "memory", "io"):
        try:
            result[name] = {
                category: int(dict(field.split("=", 1) for field in fields)["total"])
                for category, *fields in (
                    line.split() for line in (directory / f"{name}.pressure").read_text().splitlines()
                )
            }
        except (OSError, ValueError, KeyError):
            pass
    return result


def scope_directory(cfg) -> Path | None:
    """An unavailable dedicated scope degrades telemetry, never worker execution."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        from .worker_manager import workers_runtime_dir
        from .worker_scopes import cgroup, prefix

        directory = cgroup()
        if not directory.name.startswith("tauceti-worker-") or not directory.name.endswith(".scope"):
            return None
        if directory.name.startswith(prefix(workers_runtime_dir(), cfg.wid)) and directory.name.endswith(".scope"):
            return directory
    except Exception:
        pass
    return None


def values(path: Path) -> dict:
    try:
        return {name: int(value) for name, value in (line.split() for line in path.read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def number(path: Path) -> int | None:
    try:
        return int(path.read_text())
    except (OSError, ValueError):
        return None


def scope_usage(directory: Path) -> dict:
    cpu = values(directory / "cpu.stat").get("usage_usec")
    memory = values(directory / "memory.stat")
    io = None
    try:
        # Keep devices separate: adding stacked-device accounting can double count I/O.
        io = {
            device: {name: int(value) for name, value in (field.split("=", 1) for field in fields)}
            for device, *fields in (line.split() for line in (directory / "io.stat").read_text().splitlines())
        }
    except (OSError, ValueError):
        pass
    return dict(
        unit=directory.name,
        cgroup=str(directory),
        observed_at=time.time(),
        scope="worker wrapper, loop, coding rounds and background verification; cumulative since scope creation",
        cpu_seconds=cpu / 1_000_000 if cpu is not None else None,
        memory_anon_bytes=memory.get("anon"),
        memory_file_bytes=memory.get("file"),
        memory_kernel_bytes=memory.get("kernel"),
        memory_charged_bytes=number(directory / "memory.current"),
        memory_peak_bytes=number(directory / "memory.peak"),
        memory_peak_scope="scope lifetime; charged memory including file cache and kernel memory, not RSS",
        task_count=number(directory / "pids.current"),
        io=io,
        pressure_stall_us=pressure(directory),
    )


def usage() -> tuple[float, int]:
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    unit = 1 if sys.platform == "darwin" else 1024
    return own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime, int(
        max(own.ru_maxrss, child.ru_maxrss) * unit
    )


class RoundResources:
    """Keep round CPU accounting separate from inexpensive worker-scope observations.

    A worker scope also contains sibling verification jobs. Its counters must not
    be accumulated into round CPU totals or presented as round RSS.
    """

    def __init__(self, cfg, *, publish_interval: float = 60.0):
        from .activity import current_round_id

        self.cfg = cfg
        self.interval = publish_interval
        self.publish_interval = publish_interval
        self.root = os.getpid()
        self.id = uuid.uuid4().hex
        self.activity_id = current_round_id()
        self.started_at = time.time()
        self.started = time.monotonic()
        self.baseline_cpu = usage()[0]
        self.baseline_reads = read_bytes(self.root)
        self.directory = scope_directory(cfg)
        self.cpu = 0.0
        self.sample_count = 0
        self.error = None
        self.stop_event = threading.Event()
        self.path = cfg.state / "resources.json"
        self.previous = read_json(self.path).get("worker", {})
        self.thread = threading.Thread(target=self._run, daemon=True, name="round-resources")

    def start(self):
        self.thread.start()
        return self

    def sample(self) -> dict:
        kernel_cpu, max_rss = usage()
        elapsed = time.monotonic() - self.started
        self.cpu = max(self.cpu, kernel_cpu - self.baseline_cpu)
        self.sample_count += 1
        reads = read_bytes(self.root)
        return dict(
            id=self.id,
            activity_id=self.activity_id,
            activity_schema=1,
            started_at=self.started_at,
            elapsed_seconds=round(elapsed, 3),
            cpu_seconds=round(self.cpu, 3),
            average_cpu_percent=round(100 * self.cpu / elapsed, 2) if elapsed > 0 else 0,
            max_process_rss_bytes=max_rss,
            sample_count=self.sample_count,
            sample_interval_seconds=self.interval,
            scope="round process and reaped descendants; live unreaped children excluded from CPU",
            cpu_complete=False,
            read_bytes=max(0, reads - self.baseline_reads)
            if reads is not None and self.baseline_reads is not None
            else None,
            read_bytes_scope="supervisor and reaped children; excludes live or unreaped descendants",
            worker_scope=scope_usage(self.directory) if self.directory else None,
        )

    def publish(self, current: dict, *, complete: bool = False) -> dict:
        old = self.previous
        cpu = old.get("cpu_seconds", 0) + current["cpu_seconds"]
        measured = old.get("measured_seconds", 0) + current["elapsed_seconds"]
        now = time.time()
        first = old.get("started_at", self.started_at)
        total = dict(
            started_at=first,
            elapsed_seconds=round(max(0, now - first), 3),
            measured_seconds=round(measured, 3),
            cpu_seconds=round(cpu, 3),
            average_cpu_percent=round(cpu / max(0.001, now - first) * 100, 2),
            max_process_rss_bytes=max(old.get("max_process_rss_bytes", 0), current["max_process_rss_bytes"]),
            completed_rounds=old.get("completed_rounds", 0) + int(complete),
        )
        value = dict(
            worker=total,
            round={**{key: value for key, value in current.items() if key != "worker_scope"}, "complete": complete},
            worker_scope=current["worker_scope"],
            sampled_at=now,
            sampling_error=self.error,
        )
        report_runtime(resources=value)
        # Only completed rounds advance persistent totals, so a live sample is never counted twice.
        if complete:
            atomic_json(self.path, value)
            self.cfg.logdir.mkdir(parents=True, exist_ok=True)
            with (self.cfg.logdir / "resources.jsonl").open("a") as file:
                file.write(json.dumps({"worker_id": self.cfg.wid, **value}) + "\n")
        return value

    def _publish_sample(self):
        try:
            self.publish(self.sample())
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"

    def _run(self):
        self._publish_sample()
        while not self.stop_event.wait(self.publish_interval):
            self._publish_sample()

    def finish(self):
        from .config import log

        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            log("resources: sampling did not stop; skipping final accounting")
            return
        try:
            current = self.sample()
            # Keep the existing round CPU meaning: sibling background verification
            # belongs only to worker_scope. Unreaped or orphaned children are excluded.
            current["cpu_seconds"] = round(max(0, usage()[0] - self.baseline_cpu), 3)
            current["average_cpu_percent"] = round(
                100 * current["cpu_seconds"] / max(0.001, current["elapsed_seconds"]), 2
            )
            current["cpu_complete"] = True
            self.publish(current, complete=True)
            log(
                f"resources: {current['elapsed_seconds']:.1f}s wall, {current['cpu_seconds']:.1f}s CPU "
                f"({current['average_cpu_percent']:.1f}% average; 100%=one core), "
                f"max process RSS {current['max_process_rss_bytes'] / 2**20:.1f} MiB"
            )
            if self.error:
                log(f"resources: some samples unavailable: {self.error}")
        except Exception as error:
            log(f"resources: final accounting unavailable ({type(error).__name__}: {error})")

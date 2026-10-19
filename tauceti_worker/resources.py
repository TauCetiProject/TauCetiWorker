"""Host process resource accounting for a work round, including its subprocesses."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .runtime_status import atomic_json, read_json, report_runtime


def process_table() -> dict[int, dict]:
    """Read process identities, resident memory, and CPU time without command lines or credentials."""
    ticks = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    result = {}
    if sys.platform.startswith("linux"):
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                result[int(path.name)] = dict(
                    ppid=int(fields[1]),
                    identity=int(fields[19]),
                    cpu_seconds=sum(int(fields[i]) for i in (11, 12, 13, 14)) / ticks,
                    rss_bytes=max(0, int(fields[21])) * page,
                )
            except (OSError, ValueError, IndexError):
                continue  # A process can exit while the snapshot is being read.
    else:
        # ps reports CPU for live processes only; the final rusage reading also covers reaped children.
        proc = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,time="], capture_output=True, text=True, timeout=5)
        for line in proc.stdout.splitlines():
            try:
                pid, parent, rss, cpu = line.split()
                days, sep, cpu = cpu.rpartition("-")
                day_seconds = int(days) * 86400 if sep else 0
                seconds = 0.0
                for part in cpu.split(":"):
                    seconds = seconds * 60 + float(part)
                seconds += day_seconds
                result[int(pid)] = dict(ppid=int(parent), identity=None, cpu_seconds=seconds, rss_bytes=int(rss) * 1024)
            except ValueError:
                continue
    return result


def process_tree(table: dict[int, dict], root: int) -> dict[int, dict]:
    """Select descendants; unrelated workers and processes are excluded."""
    children: dict[int, list[int]] = {}
    for pid, value in table.items():
        children.setdefault(value["ppid"], []).append(pid)
    selected = {}
    pending = [root]
    while pending:
        pid = pending.pop()
        if pid in selected or pid not in table:
            continue
        selected[pid] = table[pid]
        pending.extend(children.get(pid, ()))
    return selected


def usage() -> tuple[float, int]:
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    unit = 1 if sys.platform == "darwin" else 1024
    return own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime, int(
        max(own.ru_maxrss, child.ru_maxrss) * unit
    )


class RoundResources:
    """Sample summed tree RSS, preserve kernel CPU accounting, and publish cumulative worker totals.

    100% CPU means one logical core. RSS sums can double-count shared pages; they are not PSS.
    The tree peak is sampled, whereas max_process_rss_bytes is the kernel's single-process high water.
    """

    def __init__(self, cfg, *, interval: float = 2.0, publish_interval: float = 60.0):
        from .activity import current_round_id

        self.cfg = cfg
        self.interval = interval
        self.publish_interval = publish_interval
        self.root = os.getpid()
        self.id = uuid.uuid4().hex
        self.activity_id = current_round_id()
        self.started_at = time.time()
        self.started = time.monotonic()
        self.baseline_cpu = usage()[0]
        self.peak_rss = 0
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
        tree = process_tree(process_table(), self.root)
        elapsed = time.monotonic() - self.started
        rss = sum(p["rss_bytes"] for p in tree.values())
        # The parent's waited-child counters retain CPU from processes that disappeared between samples.
        live_children_cpu = sum(p["cpu_seconds"] for pid, p in tree.items() if pid != self.root)
        self.cpu = max(self.cpu, kernel_cpu - self.baseline_cpu + live_children_cpu)
        self.peak_rss = max(self.peak_rss, rss)
        self.sample_count += 1
        return dict(
            id=self.id,
            activity_id=self.activity_id,
            activity_schema=1,
            started_at=self.started_at,
            elapsed_seconds=round(elapsed, 3),
            cpu_seconds=round(self.cpu, 3),
            average_cpu_percent=round(100 * self.cpu / elapsed, 2) if elapsed > 0 else 0,
            rss_bytes=rss,
            peak_tree_rss_bytes=self.peak_rss,
            max_process_rss_bytes=max_rss,
            process_count=len(tree),
            sample_count=self.sample_count,
            sample_interval_seconds=self.interval,
            scope="host process tree",
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
            peak_tree_rss_bytes=max(old.get("peak_tree_rss_bytes", 0), current["peak_tree_rss_bytes"]),
            max_process_rss_bytes=max(old.get("max_process_rss_bytes", 0), current["max_process_rss_bytes"]),
            completed_rounds=old.get("completed_rounds", 0) + int(complete),
        )
        value = dict(worker=total, round={**current, "complete": complete}, sampled_at=now, sampling_error=self.error)
        report_runtime(resources=value)
        # Only completed rounds advance persistent totals, so a live sample is never counted twice.
        if complete:
            atomic_json(self.path, value)
            self.cfg.logdir.mkdir(parents=True, exist_ok=True)
            with (self.cfg.logdir / "resources.jsonl").open("a") as file:
                file.write(json.dumps({"worker_id": self.cfg.wid, **value}) + "\n")
        return value

    def _run(self):
        last_publish = 0.0
        while not self.stop_event.is_set():
            try:
                current = self.sample()
                if time.monotonic() - last_publish >= self.publish_interval:
                    self.publish(current)
                    last_publish = time.monotonic()
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
            self.stop_event.wait(self.interval)

    def finish(self):
        from .config import log

        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            log("resources: sampling did not stop; skipping final accounting")
            return
        try:
            current = self.sample()
            # Use the kernel's accounting at exit, not a maximum live estimate: a process exiting
            # during /proc traversal can transiently appear in both live and waited-child counters.
            # Deliberately orphaned, unreaped children are not included in this final CPU reading.
            current["cpu_seconds"] = round(max(0, usage()[0] - self.baseline_cpu), 3)
            current["average_cpu_percent"] = round(
                100 * current["cpu_seconds"] / max(0.001, current["elapsed_seconds"]), 2
            )
            self.publish(current, complete=True)
            log(
                f"resources: {current['elapsed_seconds']:.1f}s wall, {current['cpu_seconds']:.1f}s CPU "
                f"({current['average_cpu_percent']:.1f}% average; 100%=one core), "
                f"peak tree RSS {current['peak_tree_rss_bytes'] / 2**20:.1f} MiB (sampled), "
                f"max process RSS {current['max_process_rss_bytes'] / 2**20:.1f} MiB"
            )
            if self.error:
                log(f"resources: some samples unavailable: {self.error}")
        except Exception as error:
            log(f"resources: final accounting unavailable ({type(error).__name__}: {error})")

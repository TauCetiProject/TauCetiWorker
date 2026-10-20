#!/usr/bin/env python3
"""Resource accounting includes real child work and persists totals without double counting."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_worker import resources
from tauceti_worker.resources import RoundResources, process_tree
from tauceti_worker.runtime_status import read_json

assert set(process_tree({1: {"ppid": 0}, 2: {"ppid": 1}, 3: {"ppid": 2}, 4: {"ppid": 0}}, 1)) == {1, 2, 3}

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    cfg = SimpleNamespace(state=root / "state", logdir=root / "logs", wid="resource-test")
    status = root / "status.json"
    os.environ["TAUCETI_RUNTIME_STATUS"] = str(status)
    monitor = RoundResources(cfg, interval=0.03, publish_interval=0.05).start()
    baseline_rss = monitor.sample()["rss_bytes"]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; a=bytearray(48*1024*1024); end=time.process_time()+0.25\nwhile time.process_time()<end: pass\ntime.sleep(0.3)",
        ]
    )
    child.wait()
    monitor.finish()
    first = read_json(cfg.state / "resources.json")
    current = first["round"]
    assert current["cpu_seconds"] >= 0.20, current  # Child already exited: sampling alone would lose it.
    assert current["peak_tree_rss_bytes"] >= baseline_rss + 32 * 1024**2, current
    assert current["max_process_rss_bytes"] >= 48 * 1024**2, current
    assert current["sample_count"] > 1, current
    assert first["worker"]["completed_rounds"] == 1
    assert read_json(status)["resources"]["round"]["complete"] is True
    assert abs(current["average_cpu_percent"] - 100 * current["cpu_seconds"] / current["elapsed_seconds"]) < 0.1

    second = RoundResources(cfg, interval=0.03, publish_interval=0.03).start()
    time.sleep(0.15)  # Repeated live publications must not compound the first round's CPU.
    second.finish()
    last = read_json(cfg.state / "resources.json")
    assert last["worker"]["completed_rounds"] == 2, last
    assert abs(last["worker"]["cpu_seconds"] - first["worker"]["cpu_seconds"] - last["round"]["cpu_seconds"]) < 0.002
    assert last["worker"]["peak_tree_rss_bytes"] >= first["worker"]["peak_tree_rss_bytes"]
    assert len((cfg.logdir / "resources.jsonl").read_text().splitlines()) == 2
    assert last["sampling_error"] is None, last
    with (
        patch.object(resources, "read_bytes", side_effect=[4096, 12288]),
        patch.object(resources, "host_pressure", side_effect=[{"io": {"some": 10}}, {"io": {"some": 35}}]),
    ):
        measured = RoundResources(cfg).sample()
    assert measured["read_bytes"] == 8192, measured
    assert measured["host_pressure_stall_us"] == {"io": {"some": 25}}, measured
    assert "co-tenants" in measured["pressure_scope"]
    with (
        patch.object(resources, "read_bytes", return_value=None),
        patch.object(resources, "host_pressure", return_value={}),
    ):
        unavailable = RoundResources(cfg).sample()
    assert unavailable["read_bytes"] is None, unavailable
    assert unavailable["host_pressure_stall_us"] == {}, unavailable

    os.environ.pop("TAUCETI_RUNTIME_STATUS")

print("[OK ] child CPU, sampled tree memory, kernel high water, live status, and durable totals")

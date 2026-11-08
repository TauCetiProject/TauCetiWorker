#!/usr/bin/env python3
"""Round CPU stays independent of cheap, optional scope telemetry."""

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
from tauceti_worker.resources import RoundResources
from tauceti_worker.runtime_status import read_json

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    cfg = SimpleNamespace(state=root / "state", logdir=root / "logs", wid="resource-test")
    status = root / "status.json"
    os.environ["TAUCETI_RUNTIME_STATUS"] = str(status)
    scope = root / "worker.scope"
    scope.mkdir()
    (scope / "cpu.stat").write_text("usage_usec 500000000\nuser_usec 400000000\nsystem_usec 100000000\n")
    (scope / "memory.stat").write_text("anon 1000\nfile 2000\nkernel 3000\n")
    (scope / "memory.current").write_text("6000")
    (scope / "memory.peak").write_text("9000")
    (scope / "pids.current").write_text("12")
    (scope / "memory.pressure").write_text("some avg10=0.00 total=123\nfull avg10=0.00 total=45\n")

    with patch.object(resources, "scope_directory", return_value=scope):
        monitor = RoundResources(cfg).start()
    assert monitor.interval == 60
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; a=bytearray(48*1024*1024); end=time.process_time()+0.25\nwhile time.process_time()<end: pass",
        ],
    )
    child.wait()
    # Independent background work changes scope totals, not round CPU accounting.
    (scope / "cpu.stat").write_text("usage_usec 900000000\n")
    monitor.finish()
    first = read_json(cfg.state / "resources.json")
    current = first["round"]
    assert 0.20 <= current["cpu_seconds"] < 10, current
    assert current["max_process_rss_bytes"] >= 48 * 1024**2, current
    assert current["sample_count"] == 2, current  # Start/end only; short-lived children aren't lost.
    assert current["cpu_complete"] is True
    assert first["worker_scope"]["cpu_seconds"] == 900
    assert first["worker_scope"]["memory_anon_bytes"] == 1000
    assert first["worker_scope"]["memory_peak_bytes"] == 9000
    assert first["worker_scope"]["task_count"] == 12
    assert first["worker_scope"]["io"] is None
    assert first["worker_scope"]["pressure_stall_us"] == {"memory": {"some": 123, "full": 45}}
    assert "background verification" in first["worker_scope"]["scope"]
    assert "scope lifetime" in first["worker_scope"]["memory_peak_scope"]
    assert first["worker"]["completed_rounds"] == 1
    assert read_json(status)["resources"]["round"]["complete"] is True
    assert abs(current["average_cpu_percent"] - 100 * current["cpu_seconds"] / current["elapsed_seconds"]) < 0.1

    second = RoundResources(cfg, publish_interval=0.03).start()
    time.sleep(0.10)
    second.finish()
    last = read_json(cfg.state / "resources.json")
    assert last["worker"]["completed_rounds"] == 2, last
    assert abs(last["worker"]["cpu_seconds"] - first["worker"]["cpu_seconds"] - last["round"]["cpu_seconds"]) < 0.002
    assert len((cfg.logdir / "resources.jsonl").read_text().splitlines()) == 2
    assert last["sampling_error"] is None, last
    assert last["worker_scope"] is None

    (scope / "memory.current").write_text("unreadable counter")
    partial = resources.scope_usage(scope)
    assert partial["memory_charged_bytes"] is None
    assert partial["cpu_seconds"] == 900
    (scope / "io.stat").write_text("8:0 rbytes=100 wbytes=200 rios=1\n253:0 rbytes=100 wbytes=200 rios=1\n")
    assert resources.scope_usage(scope)["io"]["8:0"]["rbytes"] == 100
    with patch("tauceti_worker.worker_scopes.cgroup", side_effect=OSError("unavailable")):
        assert resources.scope_directory(cfg) is None
    with patch("tauceti_worker.worker_scopes.cgroup", return_value=root / "shared-manager.service"):
        assert resources.scope_directory(cfg) is None
    from tauceti_worker.worker_manager import WorkersError

    with (
        patch("tauceti_worker.worker_scopes.cgroup", return_value=root / "tauceti-worker-fixture.scope"),
        patch("tauceti_worker.worker_manager.workers_runtime_dir", side_effect=WorkersError("unavailable runtime")),
    ):
        assert RoundResources(cfg).sample()["worker_scope"] is None
    with patch.object(resources, "read_bytes", side_effect=[4096, 12288]):
        measured = RoundResources(cfg).sample()
    assert measured["read_bytes"] == 8192, measured
    os.environ.pop("TAUCETI_RUNTIME_STATUS")

print("[OK ] round CPU, minute refreshes, independent scope counters, and unavailable telemetry")

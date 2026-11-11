#!/usr/bin/env python3
"""Compose-style SIGTERM tears down the active round before the loop exits 143."""

import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(started: Path, cleaned: Path) -> int:
    def stop(_signum, _frame):
        cleaned.touch()
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    started.touch()
    while True:
        time.sleep(60)


def driver(started: Path, cleaned: Path) -> int:
    import tauceti_worker.loop as loop
    import tauceti_worker.round as round_lifecycle

    loop.choose_model = lambda *_args, **_kwargs: ("codex", {})
    loop.github_budget = lambda: {}
    round_lifecycle.spawn_round = lambda _tail, **_kwargs: subprocess.Popen(
        [sys.executable, __file__, "child", str(started), str(cleaned)],
        start_new_session=True,
    )
    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
    cfg = SimpleNamespace(wid="docker")
    return loop.cmd_loop(args, cfg, only=[], agent="codex")


def wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


if len(sys.argv) > 1 and sys.argv[1] == "child":
    raise SystemExit(child(Path(sys.argv[2]), Path(sys.argv[3])))
if len(sys.argv) > 1 and sys.argv[1] == "driver":
    raise SystemExit(driver(Path(sys.argv[2]), Path(sys.argv[3])))

with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    started, cleaned = base / "started", base / "cleaned"
    process = subprocess.Popen([sys.executable, __file__, "driver", str(started), str(cleaned)])
    if not wait_for(started):
        process.kill()
        raise AssertionError("round child did not start")
    process.terminate()
    return_code = process.wait(15)
    if return_code != 143:
        raise AssertionError(f"loop exited {return_code}, expected 143")
    if not wait_for(cleaned):
        raise AssertionError("active round did not receive SIGTERM cleanup")
    print("[OK ] loop SIGTERM exits 143 after terminating the active round")

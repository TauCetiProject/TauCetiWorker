#!/usr/bin/env python3
"""Per-round private bytes: incremental measurement, the cap, abandonment and view validation."""

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MIB = 1024**2
SCRATCH = Path(tempfile.mkdtemp(prefix="tauceti-round-storage-test-"))
PINNED = {
    "HOME": SCRATCH / "home",
    "TAUCETI_SHARED_MATHLIB_ROOT": SCRATCH / "mathlib-builds",
    "TAUCETI_BUILD_CACHE_ROOT": SCRATCH / "builds",
    "TAUCETI_ACTIVITY_DIR": SCRATCH / "activity",
    "TAUCETI_WORKERS_STATE_DIR": SCRATCH / "workers-state",
}
for name, path in PINNED.items():
    path.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
os.environ["TAUCETI_BACKGROUND_CLEANUP"] = "off"
for name in ("TAUCETI_ROUND_PRIVATE_BYTES", "TAUCETI_ROUND_PRIVATE_CAP_GIB", "TAUCETI_ROUND_PRIVATE_SCAN_SECONDS"):
    os.environ.pop(name, None)

from tauceti_worker import agents, round_storage, shared_mathlib  # noqa: E402
from tauceti_worker.config import Die  # noqa: E402


def assert_pinned():
    """Nothing here may scan, kill or delete outside this test's scratch directory."""
    for name in PINNED:
        assert Path(os.environ[name]).resolve().is_relative_to(SCRATCH.resolve()), name
    assert shared_mathlib.root().is_relative_to(SCRATCH.resolve())


def write(path, size, byte=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as out:
        out.write(byte * size)
        out.flush()
        os.fsync(out.fileno())


def allocated(path):
    return path.stat().st_blocks * 512


def fresh(name):
    root = SCRATCH / name
    root.mkdir()
    return root


def scanner_tests():
    assert_pinned()
    base = fresh("scanner")
    checkout, private_store, store = base / "checkout", base / "lake-store", base / "shared-store"
    write(checkout / ".lake/build/lib/A.olean", MIB)
    write(private_store / "artifacts/old", MIB)
    write(store / "objects/shared", 4 * MIB)
    scanner = round_storage.Scanner([checkout, private_store])
    scanner.since_ns = time.time_ns()
    baseline = scanner.scan(full=True)
    assert baseline == allocated(checkout / ".lake/build/lib/A.olean") + allocated(private_store / "artifacts/old")

    # Hard links to a store outside the roots add no private bytes, however many there are.
    for index in range(8):
        os.link(store / "objects/shared", checkout / f".lake/build/lib/linked-{index}.olean")
    assert scanner.scan() == baseline

    # A new output linked into both the checkout and the private store counts once.
    write(private_store / "artifacts/new", 2 * MIB)
    os.link(private_store / "artifacts/new", checkout / ".lake/build/lib/B.olean")
    new = allocated(private_store / "artifacts/new")
    assert scanner.scan() == baseline + new

    # Once the store copy is gone, the shared file's links all lie inside the roots.
    os.unlink(store / "objects/shared")
    scanner.scan(full=True)
    assert scanner.private_bytes() == baseline + new + allocated(checkout / ".lake/build/lib/linked-0.olean")
    for index in range(8):
        os.unlink(checkout / f".lake/build/lib/linked-{index}.olean")
    assert scanner.scan() == baseline + new
    print("[OK] hard links to a shared store are not private; files linked only inside the roots count once")

    # A file created this round that grows in place, without its directory changing, is caught.
    log = checkout / "build.log"
    write(log, MIB)
    scanner.scan()
    time.sleep(2.2)  # past the racy window: only per-file tracking can see the growth
    scanner.scan()
    stamp = checkout.stat().st_mtime_ns
    with open(log, "ab") as out:
        out.write(b"y" * 3 * MIB)
        out.flush()
        os.fsync(out.fileno())
    assert checkout.stat().st_mtime_ns == stamp
    assert scanner.scan() == baseline + new + allocated(log)
    log.unlink()
    assert scanner.scan() == baseline + new
    print("[OK] a file growing in place is measured on the next pass")

    # Unchanged directories are not reread: a quiet pass lists only the rotating slice.
    for index in range(64):
        write(checkout / f".lake/build/ir/d{index}/x.c", 10)
    time.sleep(2.2)  # past the racy window, so these directories count as settled
    scanner.scan()
    listed = scanner.listed
    scanner.scan()
    assert scanner.listed - listed < 30, scanner.listed - listed
    print("[OK] quiet passes reread only a rotating slice of directories")

    # Unreadable directories (overlay work dirs are left mode 000) do not stop measurement.
    blocked = checkout / ".lake/packages/mathlib/.lake/tauceti-overlay-work/work"
    blocked.mkdir(parents=True)
    blocked.chmod(0)
    try:
        assert scanner.scan() == baseline + new + 64 * allocated(checkout / ".lake/build/ir/d0/x.c")
    finally:
        blocked.chmod(0o700)
    print("[OK] unreadable directories count as empty")


def policy_tests():
    assert_pinned()
    path = round_storage.policy_path()
    assert path.is_relative_to(SCRATCH.resolve()) or path.is_relative_to(SCRATCH)
    assert round_storage.policy({})["mode"] == "off"
    path.write_text(json.dumps({"mode": "report", "cap_gib": 2}))
    assert round_storage.policy({}) == dict(mode="report", cap_bytes=2 * 1024**3, scan_seconds=30.0)
    env = {"TAUCETI_ROUND_PRIVATE_BYTES": "enforce", "TAUCETI_ROUND_PRIVATE_CAP_GIB": "0.5"}
    assert round_storage.policy(env)["mode"] == "enforce" and round_storage.policy(env)["cap_bytes"] == 1024**3 // 2
    for bad in ({"TAUCETI_ROUND_PRIVATE_BYTES": "on"}, {"TAUCETI_ROUND_PRIVATE_CAP_GIB": "-1"}):
        try:
            round_storage.policy(bad)
        except Die:
            pass
        else:
            raise AssertionError(bad)
    path.write_text("[]")
    try:
        round_storage.policy({})
    except Die:
        pass
    else:
        raise AssertionError("a malformed host policy must be loud")
    path.unlink()
    print("[OK] the per-host file selects the mode; the environment overrides it; invalid values are fatal")

    base = fresh("roots")
    checkout, store = base / "checkout", base / "lake"
    (checkout / ".lake").mkdir(parents=True)
    store.mkdir()
    env = {
        "LAKE_CACHE_DIR": str(store),
        "MATHLIB_CACHE_DIR": str(checkout / ".lake"),
        "TMPDIR": "/tmp",
        "TAUCETI_SHARED_MATHLIB_ROOT": str(checkout / ".lake"),
    }
    assert round_storage.round_roots(checkout, env) == [store.resolve()]  # overlaps shared state
    env.pop("TAUCETI_SHARED_MATHLIB_ROOT")
    assert round_storage.round_roots(checkout, env) == [checkout.resolve(), store.resolve()]
    print("[OK] roots exclude shared state and shared temporary directories, and nest once")


def publish_entry(key):
    pool = shared_mathlib.root()
    entry = pool / "entries" / key
    write(entry / "build/lib/lean/Mathlib.olean", 4096)
    (entry / "ready.json").write_text(json.dumps(dict(key=key, inventory=shared_mathlib.inventory(entry / "build"))))
    return pool, entry


def view_tests():
    assert_pinned()
    pool, entry = publish_entry("k1")
    env = {"TAUCETI_SHARED_MATHLIB_KEY": "k1", "TAUCETI_SHARED_MATHLIB_ROOT": str(pool)}
    assert round_storage.validate_shared_views(env, enforce=True) == dict(shared_mathlib="matches ready.json")
    with open(entry / "build/lib/lean/Mathlib.olean", "ab") as out:
        out.write(b"tampered")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        assert "does not match" in round_storage.validate_shared_views(env, enforce=False)["shared_mathlib"]
    assert not (pool / "safety-stop.json").exists()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            round_storage.validate_shared_views(env, enforce=True)
    except round_storage.SharedViewMismatch as error:
        assert isinstance(error, agents.BuildCacheUnavailable)  # never charged: the agent did not start
        assert "nothing deleted" in str(error)
    else:
        raise AssertionError("a modified shared entry must stop the round")
    assert (entry / "build/lib/lean/Mathlib.olean").is_file()  # retained for inspection
    assert "does not match" in json.loads((pool / "safety-stop.json").read_text())["reason"]
    (pool / "safety-stop.json").unlink()
    print("[OK] a shared Mathlib entry that differs from ready.json stops the round and deletes nothing")


AGENT = r"""
import os, sys, time
from pathlib import Path
mode, cwd, store = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
print("started", flush=True)
if mode.startswith("copy"):
    # 7 "GiB" at test scale: real one-link files, as an unpacked private Mathlib would be.
    for index in range(7 * 16 if mode == "copy" else 24):
        target = cwd / ".lake/packages/mathlib/.lake/build/lib" / f"M{index}.olean"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            out.write(b"o" * (1 << 20))
            out.flush()
            os.fsync(out.fileno())
        time.sleep(0.02)
    if mode == "copy":
        time.sleep(120)  # only reached if the cap never fires
    time.sleep(1)
elif mode in ("grow", "store"):
    # grow: append to the inherited build file in place; store: fill the private Lake store,
    # which did not exist when the round started.
    target = cwd / ".lake/build/lib/Base.olean" if mode == "grow" else store.parent / "lake-store/a/b.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "ab") as out:
        for _ in range(24):
            out.write(b"g" * (1 << 20))
            out.flush()
            os.fsync(out.fileno())
            time.sleep(0.02)
    time.sleep(120 if mode == "store" else 0.5)
elif mode == "link":
    for index, source in enumerate(sorted((store / "objects").iterdir()) * 4):
        target = cwd / ".lake/build/lib" / f"L{index}.olean"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        time.sleep(0.01)
    time.sleep(1.5)
print("finished", flush=True)
"""


def round_tests():
    assert_pinned()
    base = fresh("rounds")
    script = base / "agent.py"
    script.write_text(AGENT)
    store = base / "shared-store"
    for index in range(32):
        write(store / "objects" / f"o{index}", MIB)
    state = base / "state" / "w1"
    state.mkdir(parents=True)
    cfg = SimpleNamespace(state=state, wid="w1")
    runs = []

    def run(mode, setting, *, cap_mib=16, scan="0.3", patches=()):
        runs.append(mode)
        checkout = base / f"checkout-{mode}-{setting}-{len(runs)}"
        write(checkout / ".lake/build/lib/Base.olean", 8 * MIB)  # inherited build state: baseline
        (checkout / "Main.lean").write_text("-- source\n")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "TAUCETI_ACTIVITY_DIR": os.environ["TAUCETI_ACTIVITY_DIR"],
            "TAUCETI_WORKERS_STATE_DIR": os.environ["TAUCETI_WORKERS_STATE_DIR"],
            "TAUCETI_SHARED_MATHLIB_ROOT": os.environ["TAUCETI_SHARED_MATHLIB_ROOT"],
            "TAUCETI_BUILD_CACHE_ROOT": os.environ["TAUCETI_BUILD_CACHE_ROOT"],
            "TAUCETI_BACKGROUND_CLEANUP": "off",
            "TAUCETI_CLAUDE_BILLING": "api",
            "TAUCETI_ROUND_PRIVATE_BYTES": setting,
            "TAUCETI_ROUND_PRIVATE_CAP_GIB": str(cap_mib / 1024),
            "TAUCETI_ROUND_PRIVATE_SCAN_SECONDS": scan,
            "LAKE_CACHE_DIR": str(base / "lake-store"),
        }

        def launch(prompt, profile):
            return [sys.executable, "-I", str(script), mode, str(checkout), str(store)], dict(os.environ), None

        output = io.StringIO()
        started = time.monotonic()
        error = None
        with (
            contextlib.ExitStack() as stack,
            patch.dict(os.environ, env, clear=True),
            patch.object(agents, "host_agent_launch", launch),
            patch.object(round_storage, "TICK_SECONDS", 0.1),
            patch.object(round_storage, "KILL_GRACE", 3.0),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            for extra in patches:
                stack.enter_context(extra)
            assert_pinned()
            try:
                rc = agents.run_agent_host(checkout, "prompt", "claude", base / "logs", quota_cfg=cfg)
            except round_storage.RoundAbandoned as raised:
                rc, error = None, raised
        report = json.loads((state / "round-storage.json").read_text())
        return checkout, rc, error, report, time.monotonic() - started, output.getvalue()

    checkout, rc, error, report, elapsed, text = run("copy", "enforce")
    assert error is not None, text
    assert isinstance(error, agents.NoProgress) and not isinstance(error, agents.BuildCacheUnavailable)
    assert elapsed < 60, elapsed  # killed, not left to sleep
    assert report["killed"] and report["exceeded"] and report["new_private_bytes"] > 16 * MIB, report
    assert report["baseline_private_bytes"] >= 8 * MIB, report
    assert not checkout.exists(), "the checkout must not be retained"
    discarded = list((state / "recovery-cleanup" / "discarded").iterdir())
    assert len(discarded) == 1 and discarded[0].name.startswith("abandoned-"), discarded
    assert (discarded[0] / "Main.lean").is_file()  # moved whole, for the collector, never saved
    assert not (state / "recovery").exists() and not (checkout.parent / "retained-checkouts").exists()
    print("[OK] a round writing one-link files past its cap is killed and abandoned without recovery")

    checkout, rc, error, report, elapsed, text = run("link", "enforce")
    assert error is None and rc == 0, text
    assert not report["exceeded"] and not report["killed"], report
    assert report["new_private_bytes"] < MIB, report  # 128 links to 32 MiB of shared objects
    assert checkout.is_dir() and len(list((checkout / ".lake/build/lib").iterdir())) == 129
    print("[OK] a round that only hard-links a shared store is not killed")

    checkout, rc, error, report, elapsed, text = run("copy-exit", "report")
    assert error is None and rc == 0, text
    assert report["exceeded"] and not report["killed"] and checkout.is_dir(), report
    assert "report mode" in text
    print("[OK] report mode measures and reports the excess without killing")

    checkout, rc, error, report, elapsed, text = run("link", "enforce", cap_mib=4)
    assert error is None and rc == 0 and not report["exceeded"], report
    assert report["baseline_private_bytes"] > 4 * MIB
    print("[OK] inherited private bytes above the cap are the baseline, not an excess")

    checkout, rc, error, report, elapsed, text = run("grow", "enforce", scan="1000")
    assert error is not None and not report["killed"] and report["new_private_bytes"] > 16 * MIB, report
    assert not checkout.exists()
    print("[OK] the final full walk sees an inherited file grown in place, and abandons the round")

    checkout, rc, error, report, elapsed, text = run("store", "enforce")
    assert error is not None and report["killed"] and elapsed < 60, report
    assert str(base / "lake-store") in report["roots"]
    print("[OK] a private store created during the round is measured")

    def failing_telemetry(*args, **kwargs):
        raise OSError(28, "No space left on device")

    checkout, rc, error, report, elapsed, text = run(
        "copy", "enforce", patches=[patch("tauceti_worker.overlap_support.atomic", failing_telemetry)]
    )
    assert error is not None and report["killed"] and not checkout.exists(), report
    print("[OK] a write failure after the kill still abandons the round")

    from tauceti_worker import retention

    name = f"checkout-copy-enforce-{len(runs) + 1}"
    queued = len(list((state / "recovery-cleanup" / "discarded").iterdir()))
    checkout, rc, error, report, elapsed, text = run(
        "copy",
        "enforce",
        patches=[patch.object(retention, "mount_points", lambda: [base / name / ".lake/mounted"])],
    )
    assert error is not None and not checkout.exists(), text
    hidden = [path for path in base.iterdir() if path.name.startswith(f".{name}.abandoned-")]
    assert len(hidden) == 1 and "operator removal" in str(error), (hidden, error)
    assert len(list((state / "recovery-cleanup" / "discarded").iterdir())) == queued  # never queued with a mount
    print("[OK] a checkout with a mount inside is moved aside, never queued for deletion")

    # The shared view check runs before the baseline; a mismatch never launches the agent.
    pool, entry = publish_entry("k2")
    write(entry / "build/lib/lean/Extra.olean", 10)  # not in ready.json
    launched = []
    with (
        patch.dict(
            os.environ,
            {"TAUCETI_ROUND_PRIVATE_BYTES": "enforce", "TAUCETI_SHARED_MATHLIB_KEY": "k2"},
        ),
        patch.object(agents, "host_agent_launch", lambda *a: (["true"], dict(os.environ), None)),
        patch.object(agents, "owned_process", lambda *a, **k: launched.append(a)),
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        assert_pinned()
        try:
            agents.run_agent_host(base, "prompt", "claude", base / "logs", quota_cfg=cfg)
        except round_storage.SharedViewMismatch:
            pass
        else:
            raise AssertionError("mismatch must stop the round")
    assert not launched and entry.is_dir()
    (pool / "safety-stop.json").unlink()
    print("[OK] round start stops on a shared view mismatch before the agent launches")


def reporting_tests():
    from tauceti_worker import activity_summary, cache_storage

    assert_pinned()
    base = fresh("reporting")
    write(base / "a", 3 * 4096)
    os.link(base / "a", base / "b")
    os.link(base / "a", base / "c")
    write(base / "d", 4096)
    row = next(iter(cache_storage.inventory([base]).values()))
    assert row["named_bytes"] == 3 * allocated(base / "a") + allocated(base / "d"), row
    assert row["managed_bytes"] == allocated(base / "a") + allocated(base / "d"), row
    assert row["unique_inodes"] == 2, row
    output = io.StringIO()
    summary = dict(
        storage_cleanup=dict(managed_bytes=2**30, named_bytes=3 * 2**30, unique_inodes=12345, free_bytes=5 * 2**30)
    )
    with patch.object(activity_summary, "WINDOWS", ()), contextlib.redirect_stdout(output):
        with contextlib.suppress(Exception):
            activity_summary.display(dict(available=True, summary=dict(summary, prepared_at=time.time(), rounds={})))
    assert "3.0 GiB named" in output.getvalue() and "12,345 unique inodes" in output.getvalue(), output.getvalue()
    assert "5.0 GiB filesystem free" in output.getvalue()
    print("[OK] status separates named bytes, unique inodes and free space")


def main():
    if not sys.platform.startswith("linux"):
        print("[SKIP] round storage tests need Linux process supervision")
        return 0
    try:
        scanner_tests()
        policy_tests()
        view_tests()
        round_tests()
        reporting_tests()
    finally:
        for here, directories, _ in os.walk(SCRATCH):
            for name in directories:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(here, name), 0o700)
        import shutil

        shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("timed out"))
    signal.alarm(600)
    sys.exit(main())

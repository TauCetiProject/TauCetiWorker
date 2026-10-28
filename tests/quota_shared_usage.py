#!/usr/bin/env python3
"""Workers share Claude observations and Retry-After without external services or scripts."""

import fcntl
import multiprocessing
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import quota


def payload(at, used=10, reset_in=7200):
    return {
        "five_hour": {"utilization": used, "resets_at": datetime.fromtimestamp(at + reset_in, UTC).isoformat()},
        "seven_day": {"utilization": used, "resets_at": datetime.fromtimestamp(at + 3 * 86400, UTC).isoformat()},
    }


def worker(root, wid, source="source"):
    home = root / wid
    credentials = home / ".claude"
    credentials.mkdir(parents=True, exist_ok=True)
    (root / source).mkdir(exist_ok=True)
    (credentials / ".tauceti-creds-source").write_text(str(root / source))
    q = quota.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))
    q._idle_notes = lambda _readings: ({}, False)
    return q


def run_concurrent(root, wid, start, calls, results, code):
    q = worker(root, wid)

    def http(*_args, **_kwargs):
        with calls.get_lock():
            calls.value += 1
        time.sleep(0.05)
        return (200, payload(time.time()), None) if code == 200 else (429, {}, 1800)

    with patch.object(quota, "_http_get_json", http):
        start.wait(10)
        p, _ = q._claude_pass("fp", "not-a-real-token", refresh=True, account="account")
        results.put((p.available, p.rate_limited))


def test_concurrent_workers_fetch_once(root, code, *, recovering=False):
    if recovering:
        q = worker(root, "seed")
        observed = time.time() - 300
        data = payload(observed, used=27, reset_in=10800)
        data["seven_day"]["utilization"] = 1
        q._store_raw("claude", data, "fp", observed + 10800, observed)
        old, _ = q._from_cached_claude(q._cached_claude("fp"))
        assert not old.available and old.next_eligible < time.time()
    ctx = multiprocessing.get_context("fork")
    start, calls, results = ctx.Event(), ctx.Value("i", 0), ctx.Queue()
    processes = [
        ctx.Process(target=run_concurrent, args=(root, f"worker{i}", start, calls, results, code)) for i in range(4)
    ]
    for p in processes:
        p.start()
    try:
        start.set()
        received = [results.get(timeout=15) for _ in processes]
        for p in processes:
            p.join(5)
            assert p.exitcode == 0
        assert calls.value == 1, calls.value
        assert received == ([(True, False)] * 4 if code == 200 else [(False, True)] * 4), received
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join(5)
        results.close()


def test_shared_recent_read_and_429_fallback(root):
    qa, qb = worker(root, "one"), worker(root, "two")
    assert qa.cache_dir != qb.cache_dir
    assert qa._usage_dir("claude") == qb._usage_dir("claude")
    clock = [time.time()]
    observed = clock[0]
    calls = []
    responses = [(200, payload(observed), None), (429, {}, 3600), (429, {}, 600)]

    def http(*_args, **_kwargs):
        calls.append(clock[0])
        return responses.pop(0)

    with patch.object(quota.time, "time", side_effect=lambda: clock[0]), patch.object(quota, "_http_get_json", http):
        assert qa._claude_pass("fp", "token", refresh=True, account="account")[0].available
        clock[0] += quota.CLAUDE_USAGE_REFRESH_S - 1
        assert qb._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert len(calls) == 1
        clock[0] += 2
        p, _ = qb._claude_pass("fp", "token", refresh=True, account="account")
        assert p.available and p.rate_limited  # old headroom is useful despite telemetry throttling
        quota._COOLDOWNS.clear()  # another process sees only the persisted state
        p, _ = qa._claude_pass("fp", "token", refresh=True, account="account")
        assert p.available and p.rate_limited and len(calls) == 2
        assert qa._cooldown("claude", "account")[0] > 890
        p, _ = qa._claude_pass("renewed-fp", "renewed-token", refresh=True, account="account")
        assert p.rate_limited and len(calls) == 2  # token rotation never resets the cooldown
        clock[0] = observed + quota.QUOTA_TTL["claude"] + 1
        p, _ = qa._claude_pass("fp", "token", refresh=True, account="account")
        assert not p.available and p.rate_limited and len(calls) == 3


def test_pace_recovery_refreshes_before_twenty_minutes(root):
    q = worker(root, "one")
    start = time.time()
    data = payload(start, used=27, reset_in=10800)  # 27% used, pace budget 26.67%
    data["seven_day"]["utilization"] = 1
    q._store_raw("claude", data, "fp", start + 10800, start)
    with (
        patch.object(quota.time, "time", return_value=start + 300),
        patch.object(quota, "_http_get_json", return_value=(200, data, None)) as http,
    ):
        p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
        assert p.available and http.call_count == 1


def test_busy_fetch_does_not_block_existing_headroom(root):
    q = worker(root, "one")
    now = time.time()
    q._store_raw("claude", payload(now), "fp", now + 7200, now - 300)

    def busy(_fd, flags):
        assert flags & fcntl.LOCK_NB, "must not wait on the lock with usable headroom"
        raise BlockingIOError

    with patch.object(quota.fcntl, "flock", busy), patch.object(quota, "_http_get_json") as http:
        p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
        assert p.available and http.call_count == 0


def test_account_separation_and_reset(root):
    q = worker(root, "one")
    other = worker(root, "two", source="other-source")
    assert q._usage_dir("claude") != other._usage_dir("claude")
    clock = [time.time()]
    with patch.object(quota.time, "time", side_effect=lambda: clock[0]):
        q._store_raw("claude", payload(clock[0], reset_in=30), "account-a", clock[0] + 30)
        assert q._cached_claude("account-a") is not None
        assert q._cached_claude("account-b") is None
        assert other._cached_claude("account-a") is None
        clock[0] += 31
        assert q._cached_claude("account-a") is None
        with patch.object(quota, "_http_get_json", return_value=(429, {}, 1800)) as http:
            p, _ = q._claude_pass("account-a", "token", refresh=True, account="account")
            assert not p.available and p.rate_limited and http.call_count == 1


def test_headerless_backoff_and_recovery(root):
    q = worker(root, "one")
    clock = [time.time()]
    with (
        patch.object(quota.time, "time", side_effect=lambda: clock[0]),
        patch.object(quota.random, "uniform", return_value=1),
    ):
        q._start_cooldown("claude", "account", 429, None)
        first = q._cooldown("claude", "account")[0]
        clock[0] += first + 1
        q._start_cooldown("claude", "account", 429, None)
        second = q._cooldown("claude", "account")[0]
        assert second == min(first * 2, quota.RATE_LIMIT_RECHECK_MAX_S)
        clock[0] += second + 1
        with patch.object(quota, "_http_get_json", return_value=(200, payload(clock[0]), None)):
            assert q._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert q._cooldown("claude", "account")[0] == 0
        q._start_cooldown("claude", "account", 429, None)
        assert q._cooldown("claude", "account")[0] == first


def test_read_only_credentials_keep_shared_caching(root):
    qa, qb = worker(root, "one"), worker(root, "two")
    (root / "source" / quota.CLAUDE_QUOTA_DIRNAME).write_text("cannot create a cache here")
    with patch.object(quota, "_http_get_json", return_value=(200, payload(time.time()), None)) as http:
        assert qa._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert qb._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert http.call_count == 1
    assert qa._usage_dir("claude") == qb._usage_dir("claude")
    assert qa._usage_dir("claude").is_relative_to(root / ".quota-usage")


if __name__ == "__main__":
    with patch.dict(os.environ):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("TAUCETI_PACE", None)
        for code in (200, 429):
            with tempfile.TemporaryDirectory() as directory:
                test_concurrent_workers_fetch_once(Path(directory), code)
                print(f"PASS concurrent workers share HTTP {code}")
        with tempfile.TemporaryDirectory() as directory:
            test_concurrent_workers_fetch_once(Path(directory), 200, recovering=True)
            print("PASS concurrent workers all resume after pace recovery")
        for test in (
            test_shared_recent_read_and_429_fallback,
            test_account_separation_and_reset,
            test_headerless_backoff_and_recovery,
            test_read_only_credentials_keep_shared_caching,
            test_pace_recovery_refreshes_before_twenty_minutes,
            test_busy_fetch_does_not_block_existing_headroom,
        ):
            with tempfile.TemporaryDirectory() as directory:
                test(Path(directory))
                print(f"PASS {test.__name__}")

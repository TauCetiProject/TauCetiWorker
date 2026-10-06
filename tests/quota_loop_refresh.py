#!/usr/bin/env python3
"""Loop pacing bypasses the raw quota cache before every round.

The cache is useful for dashboard/status reads, but a productive round can materially
change usage. Reusing the pre-round reading let loop mode launch round after round until
Claude was exhausted. This pins the integration point without network access.
"""

import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

calls = []


class FakeQuota:
    def __init__(self, cfg):
        self.cfg = cfg

    def choose(self, forced, *, refresh=False, renew=False):
        calls.append((forced, refresh, renew))
        return "claude", {}


saved = tc.loop.Quota
tc.loop.Quota = FakeQuota
try:
    tc.loop.choose_model(object(), "claude", None, refresh=True, renew=True)
    tc.loop.choose_model(object(), "auto", None)
    # resolve_work_model runs in the round that will launch the model it picks, so it renews an
    # expiring access token; a bare choose_model (status, dashboard) must not. A `_round` child accepts
    # the cached read its loop driver just forced; a one-shot `work` (fresh) has nothing behind it and
    # must look for itself, or it would decide on a verdict as old as the TTL.
    tc.loop.resolve_work_model(object(), "claude", dry=False, ignore_quota=False)
    tc.loop.resolve_work_model(object(), "claude", dry=False, ignore_quota=False, fresh=True)
finally:
    tc.loop.Quota = saved

want = [("claude", True, True), (None, False, False), ("claude", False, True), ("claude", True, True)]
ok = calls == want
print(f"[{'OK ' if ok else 'XX '}] loop refresh/renew vs ordinary cached read: got={calls!r} want={want!r}")

# Pin the semantic behind the plumbing: a forced read must contact the endpoint even with a valid
# cache, refresh that cache on success, and retain the valid cached verdict across a transient error.
reset_session = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
reset_weekly = (datetime.now(UTC) + timedelta(days=6)).isoformat()
payload = {
    "five_hour": {"utilization": 1, "resets_at": reset_session},
    "seven_day": {"utilization": 1, "resets_at": reset_weekly},
}
q = tc.Quota.__new__(tc.Quota)
q.cache_dir = Path(tempfile.mkdtemp(prefix="tauceti-quota-stub-"))
q._cached_claude = lambda _fp: (payload, time.time())
q._idle_notes = lambda _readings: ({}, False)
stores = []
q._store_raw = lambda *args: stores.append(args)
http_calls = []
saved_http = tc.quota._http_get_json
tc.quota._http_get_json = lambda *_a, **_k: http_calls.append("fetch") or (200, payload, None)
try:
    q._claude_pass("fp", "token")
    q._claude_pass("fp", "token", refresh=True)
    fetched_on_refresh = http_calls == ["fetch"] and len(stores) == 1

    def offline(*_a, **_k):
        raise tc.GitHubError("temporary network failure")

    tc.quota._http_get_json = offline
    fallback, _ = q._claude_pass("fp", "token", refresh=True)
    cached_fallback = fallback.available
finally:
    tc.quota._http_get_json = saved_http

semantic_ok = fetched_on_refresh and cached_fallback
print(
    f"[{'OK ' if semantic_ok else 'XX '}] forced refresh fetches + stores, transient failure uses valid cache: "
    f"fetches={http_calls!r} stores={len(stores)} fallback={cached_fallback!r}"
)

# ...but a cached payload is only evidence about the MOMENT it was fetched. Usage never falls inside a
# window, so a stale figure is a lower bound; pacing it against a budget that has climbed since would let
# a reading that was over pace when taken authorize a launch on nothing but the passage of time. This
# entry was over pace when fetched (30% used at 40% elapsed, budget 26.7%) and would read as under pace
# now (60% elapsed, budget 40%) if the clock were allowed to reinterpret it.
stale = {
    "five_hour": {"utilization": 30, "resets_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()},
    "seven_day": {"utilization": 1, "resets_at": reset_weekly},
}
fetched_at = time.time() - 3599  # as old as the TTL allows
q._cached_claude = lambda _fp: (stale, fetched_at)
at_fetch = tc._classify_window("session", 30, 40, None, False).status
if_reinterpreted = tc._classify_window("session", 30, 60, None, False).status
frozen, _ = q._claude_pass("fp", "token")  # the ordinary cached read: one-shot `work`, status, dashboard
tc.quota._http_get_json = offline
stale_fallback, _ = q._claude_pass("fp", "token", refresh=True)  # ...and the failed forced refresh
tc.quota._http_get_json = saved_http
frozen_ok = (
    (at_fetch, if_reinterpreted) == ("over-pace", "under-pace")
    and [w.status for w in frozen.windows] == ["over-pace", "under-pace"]
    and not frozen.available
    and not stale_fallback.available
)
print(
    f"[{'OK ' if frozen_ok else 'XX '}] a stale cached reading cannot go available on the clock alone: "
    f"cached={[w.status for w in frozen.windows]!r} fallback_available={stale_fallback.available!r} "
    f"(fresh at that elapsed would be {if_reinterpreted!r})"
)
q._cached_claude = lambda _fp: (payload, time.time())

# The fallback after a failed forced refresh must RE-READ the cache rather than reuse what it validated
# BEFORE the request. A usage fetch can block for its whole timeout, and an entry that was live when we
# set out may have passed a reset it describes while we waited — so hand out a live entry first and
# nothing second, and the fallback must decline rather than serve the stale one.
reads = []


def serving(entries):
    def _cached(_fp):
        reads.append(1)
        return entries.pop(0) if entries else None

    return _cached


tc.quota._http_get_json = offline
q._cached_claude = serving([(payload, time.time()), None])
expired_mid_flight, _ = q._claude_pass("fp", "token", refresh=True)
q._cached_claude = serving([None, (payload, time.time())])  # the other order: a concurrent writer filled it
appeared_mid_flight, _ = q._claude_pass("fp", "token", refresh=True)
tc.quota._http_get_json = saved_http
q._cached_claude = lambda _fp: (payload, time.time())
race_ok = (
    len(reads) == 4  # two reads per pass: once before the request, once in the fallback
    and not expired_mid_flight.available
    and expired_mid_flight.error
    and appeared_mid_flight.available
)
print(
    f"[{'OK ' if race_ok else 'XX '}] the fallback re-reads the cache instead of trusting a pre-request "
    f"copy: reads={len(reads)} expired_mid_flight={expired_mid_flight.available!r} "
    f"appeared_mid_flight={appeared_mid_flight.available!r}"
)

# The same thing through the REAL cache: store an entry, read it back, and check what the file's own
# recorded fetch time does to the verdict. The stubs above pin the pacing rule; this pins the plumbing
# that has to carry the instant — and the entries that must not be believed at all.
cache = tc.Quota.__new__(tc.Quota)
cache.cache_dir = Path(tempfile.mkdtemp(prefix="tauceti-quota-cache-"))
cache._idle_notes = lambda _readings: ({}, False)
try:
    cache._store_raw("claude", stale, "fp", time.time() + 7200, fetched_at)
    served = cache._cached_claude("fp")
    round_trip = served is not None and served[0] == stale and abs(served[1] - fetched_at) < 0.001
    prov, _ = cache._claude_pass("fp", "token")
    # next_eligible must be computed from the same instant as the windows, so re-reading one entry
    # answers the same thing every time rather than sliding forward with the wall clock.
    first = prov.next_eligible
    again, _ = cache._claude_pass("fp", "token")
    stable = first is not None and again.next_eligible == first
    # ...and the wake-up it names is the one the snapshot supports: under the default curve the budget
    # reaches 30% used at 45% elapsed, which for a 5h window is 15 minutes past the 40% the reading was
    # taken at — measured from the FETCH time, not from now.
    from_fetch = first - fetched_at
    honest = abs(from_fetch - (900 + tc.PACE_EASE_S)) < 5

    # An entry whose fetch time sits in the FUTURE would be paced where the budget line is higher, so it
    # is not a cache entry at all — with no margin, since that is the whole guarantee. Same for one past
    # the TTL, and for a timestamp that is not a number: the check has to happen BEFORE the arithmetic,
    # or a corrupt entry raises inside the pacer instead of being re-fetched.
    cache._store_raw("claude", stale, "fp", time.time() + 7200, time.time() + 3600)
    future_refused = cache._cached_claude("fp") is None
    cache._store_raw("claude", stale, "fp", time.time() + 7200, time.time() + 2)
    barely_future_refused = cache._cached_claude("fp") is None
    cache._store_raw("claude", stale, "fp", time.time() + 7200, time.time() - 7200)
    expired_refused = cache._cached_claude("fp") is None
    corrupt_refused = True
    entry_file = cache.cache_dir / "quota-claude.json"
    for junk in ("yesterday", [1], {"at": 1}, True, "MISSING"):
        entry = {"fp": "fp", "payload": stale, "valid_until": time.time() + 7200}
        if junk != "MISSING":
            entry["fetched_at"] = junk
        entry_file.write_text(json.dumps(entry))
        try:
            corrupt_refused = corrupt_refused and cache._cached_claude("fp") is None
        except TypeError:
            corrupt_refused = False
finally:
    shutil.rmtree(cache.cache_dir, ignore_errors=True)

cache_ok = (
    round_trip
    and stable
    and honest
    and future_refused
    and barely_future_refused
    and expired_refused
    and corrupt_refused
)
print(
    f"[{'OK ' if cache_ok else 'XX '}] the cache carries the fetch instant, and refuses what it cannot "
    f"trust: round_trip={round_trip!r} next_eligible_stable={stable!r} wake_from_fetch={from_fetch:.0f}s "
    f"future_refused={future_refused and barely_future_refused!r} expired_refused={expired_refused!r} "
    f"corrupt_refused={corrupt_refused!r}"
)

# Pin the actual driver integration too: both paced and --ignore-quota loops must
# make a fresh availability check before the child is authorized to launch.
driver_calls = []
saved_choose = tc.loop.choose_model
saved_budget = tc.loop.github_budget
saved_round = tc.loop.run_round_subprocess
saved_sleep = tc.loop.time.sleep


def choose(_cfg, agent, _cmd, *, refresh=False, renew=False):
    driver_calls.append((agent, refresh, renew))
    return "claude", {"claude": tc.Provider("claude", True, "opus")}


def stop(_tail):
    raise KeyboardInterrupt


tc.loop.choose_model = choose
tc.loop.github_budget = lambda: {}
tc.loop.run_round_subprocess = stop
tc.loop.time.sleep = lambda seconds: (_ for _ in ()).throw(AssertionError(f"unexpected sleep({seconds})"))
try:
    for ignore in (False, True):
        args = SimpleNamespace(ignore_quota=ignore, bubble=False, quota_cmd=None, source=None)
        tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["review"], agent="claude")
finally:
    tc.loop.choose_model = saved_choose
    tc.loop.github_budget = saved_budget
    tc.loop.run_round_subprocess = saved_round
    tc.loop.time.sleep = saved_sleep

driver_want = [("claude", True, True), ("claude", True, True)]
driver_ok = driver_calls == driver_want
print(
    f"[{'OK ' if driver_ok else 'XX '}] paced and ignore-quota drivers refresh: "
    f"got={driver_calls!r} want={driver_want!r}"
)

# A nonzero round result must carry the child's structured diagnostic into backoff status instead of
# reducing every failure to rc=N.
events = []
saved_choose = tc.loop.choose_model
saved_budget = tc.loop.github_budget
saved_round = tc.loop.run_round_subprocess
saved_sleep = tc.loop.time.sleep
saved_report = tc.loop.report_runtime
saved_snapshot = tc.loop.runtime_snapshot
tc.loop.choose_model = lambda *_a, **_k: ("claude", {"claude": tc.Provider("claude", True, "opus")})
tc.loop.github_budget = lambda: {}
tc.loop.run_round_subprocess = lambda _tail: 1
tc.loop.runtime_snapshot = lambda: {
    "failure_reason": "claude agent: API Error 529 Overloaded",
    "phase": "fix",
    "target": "PR #1441",
}
tc.loop.report_runtime = lambda state=None, **changes: events.append((state, changes))
tc.loop.time.sleep = lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt)
try:
    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None, source=None)
    tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent="claude")
finally:
    tc.loop.choose_model = saved_choose
    tc.loop.github_budget = saved_budget
    tc.loop.run_round_subprocess = saved_round
    tc.loop.time.sleep = saved_sleep
    tc.loop.report_runtime = saved_report
    tc.loop.runtime_snapshot = saved_snapshot

backoff = next(changes for state, changes in events if state == "backoff")
status_ok = (
    backoff["detail"] == "claude agent: API Error 529 Overloaded"
    and backoff["phase"] == "fix"
    and backoff["target"] == "PR #1441"
)
print(f"[{'OK ' if status_ok else 'XX '}] backoff preserves the child diagnostic: {backoff['detail']!r}")

# An automatic worker must keep polling an unresolved alternative provider even when the other
# provider has a known, distant pacing recovery. The Claude bootstrap may become visible on the very
# next read; Codex's weekly pacing clock says nothing about when that will happen.
wait_at = time.time()
waiting_cases = [
    (
        "an idle alternative keeps the normal poll",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, next_eligible=wait_at + 10 * 3600),
            "claude": tc.Provider(
                "claude",
                False,
                None,
                [tc.Window("session", None, None, None, "idle", detail="bootstrap attempted; awaiting fresh usage")],
            ),
        },
        tc.POLL,
    ),
    (
        "an unreadable alternative keeps the normal poll",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, next_eligible=wait_at + 10 * 3600),
            "claude": tc.Provider("claude", False, None, error="usage unavailable"),
        },
        tc.POLL,
    ),
    (
        "all alternatives with known recovery may sleep to the earliest",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, next_eligible=wait_at + 2000),
            "claude": tc.Provider("claude", False, None, next_eligible=wait_at + 1000),
        },
        1005,
    ),
    (
        "a forced provider retains the hourly cap",
        "codex",
        {"codex": tc.Provider("codex", False, None, next_eligible=wait_at + 10 * 3600)},
        3600,
    ),
    (
        "an alternative's HTTP 429 Retry-After is capped at 15 minutes",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, next_eligible=wait_at + 10 * 3600),
            "claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=1200),
        },
        900,
    ),
    (
        "a short HTTP 429 Retry-After is still respected",
        "claude",
        {"claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=580)},
        580,
    ),
    (
        "an HTTP 429 without Retry-After keeps the normal poll",
        "claude",
        {"claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True)},
        tc.POLL,
    ),
    (
        "a non-429 Retry-After remains unchanged",
        "claude",
        {"claude": tc.Provider("claude", False, None, error="usage HTTP 503", retry_after=3600)},
        3600,
    ),
    (
        "a rate-limited provider does not hide another's earlier recovery",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, next_eligible=wait_at + 60),
            "claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=3600),
        },
        tc.POLL,
    ),
    (
        "a long non-429 Retry-After does not hide an unknown alternative",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, error="usage HTTP 503", retry_after=3600),
            "claude": tc.Provider("claude", False, None, error="usage unavailable"),
        },
        tc.POLL,
    ),
    (
        "another provider's long Retry-After does not delay a rate-limited recheck",
        "auto",
        {
            "codex": tc.Provider("codex", False, None, error="usage HTTP 503", retry_after=3600),
            "claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=3600),
        },
        900,
    ),
]
wait_ok = True
saved_choose = tc.loop.choose_model
saved_sleep = tc.loop.time.sleep
saved_clock = tc.loop.time.time
saved_report = tc.loop.report_runtime
try:
    tc.loop.time.time = lambda: wait_at
    tc.loop.report_runtime = lambda *_a, **_k: None
    for label, agent, snap, expected in waiting_cases + [
        (
            "an --ignore-quota Claude worker caps HTTP 429 Retry-After at 15 minutes",
            "claude",
            {"claude": tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=3600)},
            900,
        )
    ]:
        sleeps = []
        tc.loop.choose_model = lambda *_a, snapshot=snap, **_k: (None, snapshot)

        def record_wait(seconds, recorded=sleeps):
            recorded.append(seconds)
            raise KeyboardInterrupt

        tc.loop.time.sleep = record_wait
        args = SimpleNamespace(
            ignore_quota=label.startswith("an --ignore-quota"), bubble=False, quota_cmd=None, source=None
        )
        tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["fix"], agent=agent)
        passed = sleeps == [expected]
        wait_ok &= passed
        print(f"[{'OK ' if passed else 'XX '}] {label}: got={sleeps!r} want={[expected]!r}")
finally:
    tc.loop.choose_model = saved_choose
    tc.loop.time.sleep = saved_sleep
    tc.loop.time.time = saved_clock
    tc.loop.report_runtime = saved_report

# The 15-minute cap on a 429 recheck holds even when the operator polls less often than that.
saved_poll = tc.loop.POLL
try:
    tc.loop.POLL = 1800
    limited = tc.Provider("claude", False, None, error="usage HTTP 429", rate_limited=True, retry_after=3600)
    long_poll = tc.loop._recheck_in(limited, wait_at)
    slow_other = tc.loop._recheck_in(tc.Provider("codex", False, None), wait_at)
finally:
    tc.loop.POLL = saved_poll
poll_ok = (long_poll, slow_other) == (900, 1800)
print(f"[{'OK ' if poll_ok else 'XX '}] a long POLL does not stretch a 429 recheck: got={(long_poll, slow_other)!r}")


# A 429 starts a cooldown on disk: until it ends, a forced read does not ask the endpoint again. It
# reuses a still-valid cached reading, marked rate-limited, or else reports the 429 with the time left.
def cooldown_quota(prefix):
    q = tc.Quota.__new__(tc.Quota)
    q.cache_dir = Path(tempfile.mkdtemp(prefix=f"tauceti-quota-{prefix}-"))
    q._idle_notes = lambda _readings: ({}, False)
    return q


cool_checks = {}
cool_fetches = []
responses = []
saved_http, saved_poll = tc.quota._http_get_json, tc.quota.POLL
try:
    tc.quota._http_get_json = lambda *_a, **_k: cool_fetches.append("fetch") or responses.pop(0)

    cool = cooldown_quota("cooldown")
    responses[:] = [(429, {}, 3600)]
    first, _ = cool._claude_pass("tok1", "token", refresh=True, account="acct")
    second, _ = cool._claude_pass("tok1", "token", refresh=True, account="acct")
    renewed, _ = cool._claude_pass("tok2", "token2", refresh=True, account="acct")
    cool_checks["a 429 is asked once, then cools down"] = (
        cool_fetches == ["fetch"]
        and first.rate_limited
        and second.rate_limited
        and second.error is not None
        and 0 < second.retry_after <= tc.RATE_LIMIT_RECHECK_MAX_S
    )
    cool_checks["renewing the token does not end the account's cooldown"] = (
        renewed.rate_limited and len(cool_fetches) == 1
    )
    cool._store_raw("claude", payload, "tok1", time.time() + 7200, time.time())
    forced, _ = cool._claude_pass("tok1", "token", refresh=True, account="acct")
    ordinary, _ = cool._claude_pass("tok1", "token", account="acct")
    cool_checks["a cached reading answers during the cooldown, marked rate-limited"] = (
        forced.available and forced.rate_limited and ordinary.available and ordinary.rate_limited
    )

    two = cooldown_quota("two-accounts")
    responses[:] = [(429, {}, 600), (429, {}, 600)]
    two._claude_pass("a", "token", refresh=True, account="A")
    two._claude_pass("b", "token", refresh=True, account="B")
    cool_checks["one account's 429 keeps another account's cooldown"] = (
        two._cooldown("claude", "A")[0] > 0
        and two._cooldown("claude", "B")[0] > 0
        and two._cooldown("claude", "C")[0] == 0
    )

    odd = cooldown_quota("corrupt")
    odd._cooldown_path("claude", "x").write_text(json.dumps({"until": time.time() + 86400, "wait": 86400, "code": 429}))
    odd._cooldown_path("claude", "y").write_text(json.dumps([1]))
    odd._cooldown_path("claude", "z").write_text(json.dumps({"until": time.time() + 901, "wait": 900, "code": 429}))
    cool_checks["a corrupt record is ignored, a clock stepped back is bounded"] = (
        odd._cooldown("claude", "x")[0] == 0
        and odd._cooldown("claude", "y")[0] == 0
        and 899 < odd._cooldown("claude", "z")[0] <= 900
    )

    ro = cooldown_quota("unwritable")
    shutil.rmtree(ro.cache_dir)
    ro.cache_dir.write_text("not a directory")
    responses[:] = [(429, {}, 600)]
    ro_first, _ = ro._claude_pass("t", "token", refresh=True, account="acct")
    ro_second, _ = ro._claude_pass("t", "token", refresh=True, account="acct")
    cool_checks["an unwritable cache still cools down in-process"] = (
        ro_first.rate_limited and ro_second.rate_limited and responses == [] and cool_fetches.count("fetch") == 4
    )
    ro.cache_dir.unlink()
    ro.cache_dir.mkdir()

    other = cooldown_quota("non-429")
    responses[:] = [(503, {}, 1800)]
    other._claude_pass("t", "token", refresh=True, account="acct")
    waited, _ = other._claude_pass("t", "token", refresh=True, account="acct")
    cool_checks["another status's Retry-After is honored at the fetch, unmarked"] = (
        cool_fetches.count("fetch") == 5 and not waited.rate_limited and 1700 < waited.retry_after <= 1800
    )

    bare = cooldown_quota("headerless")
    tc.quota.POLL = 1800
    responses[:] = [(429, {}, None)]
    bare_first, _ = bare._claude_pass("t", "token", refresh=True, account="acct")
    cool_checks["a headerless 429 cools down for at most the cap"] = (
        0 < bare._cooldown("claude", "acct")[0] <= tc.RATE_LIMIT_RECHECK_MAX_S
    )
finally:
    tc.quota._http_get_json, tc.quota.POLL = saved_http, saved_poll

saved_loop_poll = tc.loop.POLL
try:
    tc.loop.POLL = 1800
    cool_checks["a long POLL does not stretch a headerless 429 recheck"] = (
        tc.loop._recheck_in(bare_first, wait_at) == 900
    )
finally:
    tc.loop.POLL = saved_loop_poll

for label, passed in cool_checks.items():
    print(f"[{'OK ' if passed else 'XX '}] {label}")
cooldown_ok = all(cool_checks.values())

# The same cooldown guards the codex endpoint.
cx = tc.Quota.__new__(tc.Quota)
cx.cache_dir = Path(tempfile.mkdtemp(prefix="tauceti-quota-codex-cooldown-"))
cx._codex_credentials_store = lambda: None
cx._codex_creds = lambda: {"tokens": {"access_token": "token", "account_id": "acct"}}
cx_fetches = []
saved_http, saved_mirror = tc.quota._http_get_json, tc.quota.mirror_creds
try:
    tc.quota.mirror_creds = lambda _cfg: None
    cx.cfg = None
    tc.quota._http_get_json = lambda *_a, **_k: cx_fetches.append("fetch") or (429, {}, 1200)
    cx_first = cx.codex(refresh=True)
    cx_second = cx.codex(refresh=True)
finally:
    tc.quota._http_get_json, tc.quota.mirror_creds = saved_http, saved_mirror
codex_cooldown_ok = (
    cx_fetches == ["fetch"]
    and cx_first.rate_limited
    and cx_second.rate_limited
    and 0 < cx_second.retry_after <= tc.RATE_LIMIT_RECHECK_MAX_S
)
print(
    f"[{'OK ' if codex_cooldown_ok else 'XX '}] codex's 429 cooldown stops re-asking too: fetches={cx_fetches!r} "
    f"second={cx_second.error!r}/{cx_second.retry_after!r}"
)
shutil.rmtree(cx.cache_dir, ignore_errors=True)

sys.exit(
    0
    if ok
    and semantic_ok
    and race_ok
    and frozen_ok
    and cache_ok
    and driver_ok
    and status_ok
    and wait_ok
    and poll_ok
    and cooldown_ok
    and codex_cooldown_ok
    else 1
)

#!/usr/bin/env python3
"""A Claude agent's own rate_limit_event readings keep the shared usage cache fresh.

Every model response carries the account's rate-limit headers, and Claude Code restates them in its
stream-json output as `rate_limit_info.unifiedWindows` (utilization as a 0-1 fraction, resetsAt in unix
seconds). Storing those readings where the usage-endpoint reading is cached lets the next pacing
decision skip the endpoint, which rate-limits per account. Dependency-free; no network.
"""

import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import agents
from tauceti_worker import quota as quota_module
from tauceti_worker.transcript import AgentTranscriptRenderer

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


# A real event from Claude Code 2.1.327 (`claude -p --output-format stream-json --verbose`), observed
# at OBSERVED; only the session id and uuid are replaced.
OBSERVED = 1795134958.6121883
REAL_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1795138200,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.27, "resetsAt": 1795138200, "status": "allowed"},
            "seven_day": {"utilization": 0.27, "resetsAt": 1795593600, "status": "allowed"},
        },
    },
    "uuid": "00000000-0000-0000-0000-000000000000",
    "session_id": "00000000-0000-0000-0000-000000000001",
}
REAL_WINDOWS = REAL_EVENT["rate_limit_info"]["unifiedWindows"]


def shifted(delta):
    """The real windows moved by `delta` seconds, so cache validity can be checked against now."""
    return {k: {**v, "resetsAt": v["resetsAt"] + delta} for k, v in REAL_WINDOWS.items()}


def quota(prefix):
    q = tc.Quota.__new__(tc.Quota)
    q.cache_dir = Path(tempfile.mkdtemp(prefix=f"tauceti-quota-{prefix}-"))
    return q


ID = ("fp", "account:test")

# Conversion of the real event, at the moment it was observed.
payload = quota_module._claude_stream_payload(REAL_WINDOWS, OBSERVED)
check("fraction becomes a percentage", round(payload["five_hour"]["utilization"], 6), 27.0)
check("weekly fraction becomes a percentage", round(payload["seven_day"]["utilization"], 6), 27.0)
check(
    "reset becomes an ISO clock the classifier parses",
    quota_module._parse_iso(payload["five_hour"]["resets_at"]),
    1795138200.0,
)
check("only the two gating windows are kept", sorted(payload), ["five_hour", "seven_day"])
states = [r.state for r in quota_module._claude_readings(payload, OBSERVED)]
check("both windows read as active", states, [quota_module.STATE_ACTIVE] * 2)
check(
    "valid until the five-hour reset, which precedes the TTL",
    quota_module._claude_valid_until(quota_module._claude_readings(payload, OBSERVED), OBSERVED),
    1795138200.0,
)

five = REAL_WINDOWS["five_hour"]
for name, bad in {
    "missing weekly": {"five_hour": five},
    "window not an object": {**REAL_WINDOWS, "seven_day": 0.27},
    "boolean utilization": {**REAL_WINDOWS, "five_hour": {**five, "utilization": True}},
    "negative utilization": {**REAL_WINDOWS, "five_hour": {**five, "utilization": -0.1}},
    "NaN utilization": {**REAL_WINDOWS, "five_hour": {**five, "utilization": float("nan")}},
    "missing reset": {**REAL_WINDOWS, "seven_day": {"utilization": 0.27}},
    "string reset": {**REAL_WINDOWS, "seven_day": {"utilization": 0.27, "resetsAt": "soon"}},
    "absurd reset": {**REAL_WINDOWS, "seven_day": {"utilization": 0.27, "resetsAt": 1e300}},
    "not an object": ["five_hour"],
}.items():
    check(f"rejects {name}", quota_module._claude_stream_payload(bad, OBSERVED), None)

# Review 2: an elapsed reset clock is not a live reading. The endpoint classifier would read an
# exhausted window whose clock has passed as the idle gap after a reset, granting a launch.
exhausted = {**REAL_WINDOWS, "five_hour": {**five, "utilization": 1.0}}
after = five["resetsAt"] + 30  # inside the classifier's reset skew
check("rejects a five-hour clock already elapsed", quota_module._claude_stream_payload(exhausted, after), None)
check("rejects a clock exactly at observation", quota_module._claude_stream_payload(exhausted, five["resetsAt"]), None)
check(
    "rejects an elapsed weekly clock",
    quota_module._claude_stream_payload(REAL_WINDOWS, REAL_WINDOWS["seven_day"]["resetsAt"] + 1),
    None,
)
q = quota("elapsed")
check("an elapsed exhausted reading is never stored", q.note_claude_stream_windows(exhausted, ID, after), False)
check("so nothing is cached", q._cached_claude("fp"), None)

# Storing a reading.
now = time.time()
# The real sample's weekly 27% is over the default pace curve at that point of its week, so the
# availability checks below use the same shape with weekly headroom.
windows = shifted(now - OBSERVED)
windows["seven_day"]["utilization"] = 0.05
q = quota("stream")
check("no launch identity stores nothing", q.note_claude_stream_windows(windows, None, now), False)
check("an incomplete reading stores nothing", q.note_claude_stream_windows({"five_hour": {}}, ID, now), False)
check("a complete reading is stored", q.note_claude_stream_windows(windows, ID, now), True)
entry = json.loads(q._raw_path("claude", "fp").read_text())
check("entry is keyed by the launch credential", entry["fp"], "fp")
check("entry records when it was observed", entry["fetched_at"], now)
check("entry is tagged as a stream reading", entry["source"], "claude-stream")
check("the cache serves it", q._cached_claude("fp") is not None, True)
check("another credential does not see it", q._cached_claude("other"), None)
older = {**windows, "five_hour": {**windows["five_hour"], "utilization": 0.1}}
check("an older reading does not replace a newer one", q.note_claude_stream_windows(older, ID, now - 5), False)
check("the newer figure survives", round(q._cached_claude("fp")[0]["five_hour"]["utilization"], 6), 27.0)
check("a later reading replaces it", q.note_claude_stream_windows(windows, ID, now + 1), True)

# Review 3: the freshness check and the write happen under the endpoint fetcher's lock, which is never
# waited for. While a fetch holds it, the stream reading is skipped and the file is left alone.
q = quota("locked")
lock = os.open(q._usage_dir("claude") / f"fetch-{q._fingerprint(ID[1])}.lock", os.O_CREAT | os.O_WRONLY, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
try:
    started = time.monotonic()
    stored = q.note_claude_stream_windows(windows, ID, now)
    check(
        "a held fetch lock skips the reading without waiting", (stored, time.monotonic() - started < 1), (False, True)
    )
    check("and writes nothing", q._raw_path("claude", "fp").exists(), False)
finally:
    fcntl.flock(lock, fcntl.LOCK_UN)
    os.close(lock)
check("once released the reading is stored", q.note_claude_stream_windows(windows, ID, now), True)

# Review 1: an HTTP 401 removes the cached reading and records the rejection; a delayed stream event
# must not recreate a reading for that credential, or the cached path would admit it and skip renewal.
saved_http = quota_module._http_get_json
try:
    rejected = quota("rejected")
    # Older than the shared refresh interval, so the forced read below asks the endpoint.
    rejected.note_claude_stream_windows(windows, ID, now - quota_module.CLAUDE_USAGE_REFRESH_S - 60)
    quota_module._http_get_json = lambda *_a, **_k: (401, {}, None)
    prov, _ = rejected._claude_pass("fp", "token", refresh=True, account=ID[1])
    check("the 401 is reported", prov.error.startswith("claude usage HTTP 401"), True)
    check("the 401 removed the cached reading", rejected._cached_claude("fp"), None)
    check("a later stream reading is refused", rejected.note_claude_stream_windows(windows, ID, time.time()), False)
    check("so the cache stays empty", rejected._cached_claude("fp"), None)
    rejected._auth_refusal_path("claude", "fp").unlink()
    check(
        "without the rejection it is stored again", rejected.note_claude_stream_windows(windows, ID, time.time()), True
    )
finally:
    quota_module._http_get_json = saved_http

# Usage only rises inside a window, but an event can restate an agent's older headers after a newer
# reading was stored. Within the same window (same reset clock, which the endpoint states with a
# sub-second fraction) the higher figure stays; a new window's figure is taken as is.
q = quota("monotone")
reset = windows["five_hour"]["resetsAt"]


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


exhausted_endpoint = {
    "five_hour": {"utilization": 100.0, "resets_at": iso(reset + 0.293)},
    "seven_day": {"utilization": 5.0, "resets_at": iso(windows["seven_day"]["resetsAt"] + 0.293)},
}
q._store_raw("claude", exhausted_endpoint, "fp", reset, now - 20)
check("a delayed lower reading is still stored", q.note_claude_stream_windows(windows, ID, now - 10), True)
stored = q._cached_claude("fp")[0]
check("but keeps the window's higher figure", stored["five_hour"]["utilization"], 100.0)
check("and takes a higher figure it brings", round(stored["seven_day"]["utilization"], 6), 5.0)
prov, _ = q._claude_pass("fp", "token", refresh=False, account=ID[1])
check("so known exhaustion still blocks", prov.available, False)
next_window = {**windows, "five_hour": {**windows["five_hour"], "resetsAt": reset + 5 * 3600}}
q.note_claude_stream_windows(next_window, ID, reset + 1)
check(
    "a new window's figure is taken as is",
    round(json.loads(q._raw_path("claude", "fp").read_text())["payload"]["five_hour"]["utilization"], 6),
    27.0,
)

# A reading describes the agent's own account, so it is shared only when the agent's credential is
# the paced one: not when the agent authenticates with $CLAUDE_CODE_OAUTH_TOKEN, and not when a
# separate usage login paces it without a verified shared account.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / ".claude").mkdir()
    (root / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "agent-token"}}))
    cfg = SimpleNamespace(home=root, wid="w", state=root / "state/w", quota_cache=root / "state/w/cache")
    tied = quota_module.Quota(cfg)
    tied._claude_creds = lambda: ({"accessToken": "agent-token"}, False)
    tied._claude_creds_source = lambda: root / ".claude"
    check("the agent's own credential is shared", tied.claude_stream_identity({}) is not None, True)
    check(
        "an environment token is not",
        tied.claude_stream_identity({"CLAUDE_CODE_OAUTH_TOKEN": "other-account-token"}),
        None,
    )
    usage_login = root / "usage/.credentials.json"
    with (
        patch.object(quota_module, "claude_usage_credentials", return_value=usage_login),
        patch.object(quota_module, "claude_usage_identity_verified", return_value=False),
    ):
        check("an unverified separate usage login is not", tied.claude_stream_identity({}), None)
    with (
        patch.object(quota_module, "claude_usage_credentials", return_value=usage_login),
        patch.object(quota_module, "claude_usage_identity_verified", return_value=True),
    ):
        check("a verified separate usage login is", tied.claude_stream_identity({}) is not None, True)

# The point: a fresh stream reading answers the pacing read without asking the endpoint, even a forced
# one, and also answers while a 429 cooldown holds.
fetches = []
try:
    quota_module._http_get_json = lambda *_a, **_k: fetches.append("fetch") or (200, {}, None)
    fresh = quota("fresh")
    fresh.note_claude_stream_windows(windows, ("tok", "acct"), time.time())
    prov, _ = fresh._claude_pass("tok", "token", refresh=True, account="acct")
    check("a fresh stream reading is reused by a forced read", (prov.available, fetches), (True, []))

    cooling = quota("cooling")
    cooling._start_cooldown("claude", "acct", 429, 3600.0, "tok")
    cooling.note_claude_stream_windows(windows, ("tok", "acct"), time.time())
    prov, _ = cooling._claude_pass("tok", "token", refresh=True, account="acct")
    check("a stream reading answers during a 429 cooldown", (prov.available, fetches), (True, []))
    check("the cooldown itself still holds", cooling._cooldown("claude", "acct", "tok")[0] > 3500, True)

    over = quota("over")
    over.note_claude_stream_windows(
        {**windows, "five_hour": {**windows["five_hour"], "utilization": 1.0}}, ("tok", "acct"), time.time()
    )
    prov, _ = over._claude_pass("tok", "token", refresh=False, account="acct")
    check("an exhausted stream reading blocks", (prov.available, fetches), (False, []))
finally:
    quota_module._http_get_json = saved_http


# Review 4: a read-only credential mount uses the same shared fallback as the endpoint fetcher, and a
# reading that cannot be written anywhere is reported as not stored.
def mounted(root):
    cfg = SimpleNamespace(home=root, wid="w", state=root / "state/w", quota_cache=root / "state/w/cache")
    q = quota_module.Quota(cfg)
    q._claude_creds_source = lambda: root / ".claude"
    return q


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / ".claude").mkdir()
    (root / ".claude" / quota_module.CLAUDE_QUOTA_DIRNAME).write_text("read-only credential mount")
    writer = mounted(root)
    check("a read-only mount stores in the fallback", writer.note_claude_stream_windows(windows, ID, time.time()), True)
    fallback = writer._claude_usage_fallback()
    check("the entry is in the fallback directory", writer._raw_path("claude", "fp").parent, fallback)
    reader = mounted(root)
    with patch.object(quota_module, "_http_get_json", side_effect=AssertionError("endpoint asked")):
        prov, _ = reader._claude_pass("fp", "token", refresh=True, account=ID[1])
    check("a reader on the same mount finds it", prov.available, True)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / ".claude").mkdir()
    (root / ".claude" / quota_module.CLAUDE_QUOTA_DIRNAME).write_text("read-only credential mount")
    stuck = mounted(root)
    blocked = stuck._claude_usage_fallback()
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a directory either")
    check(
        "a reading that cannot be written is not reported stored",
        stuck.note_claude_stream_windows(windows, ID, time.time()),
        False,
    )

# With the lock available but the cache file unwritable (here a directory sits at its path), the
# reading is likewise reported as not stored.
q = quota("unwritable")
q._raw_path("claude", "fp").mkdir(parents=True)
check("a failed cache write is not reported stored", q.note_claude_stream_windows(windows, ID, time.time()), False)

# The renderer hands each reading to its callback and renders exactly as before.
seen = []
renderer = AgentTranscriptRenderer("claude", on_rate_limit=seen.append)
line = renderer.render_line(json.dumps(REAL_EVENT))
check("rendered line is unchanged", line, "[rate limit] status=allowed type=five_hour resets=1795138200\n")
check("the callback receives the windows", seen, [REAL_WINDOWS])
renderer.render_line(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}))
check("an event without windows is not passed on", len(seen), 1)


def broken(_windows):
    raise RuntimeError("cache unavailable")


line = AgentTranscriptRenderer("claude", on_rate_limit=broken).render_line(json.dumps(REAL_EVENT))
check(
    "a failing callback does not affect rendering",
    line,
    "[rate limit] status=allowed type=five_hour resets=1795138200\n",
)

# End to end: an agent run shares its reading, and the next forced pacing read does not ask the
# endpoint. The reading is keyed by the credential frozen at launch.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    cfg = SimpleNamespace(home=root, wid="w", state=root / "state/w", quota_cache=root / "state/w/cache")
    credentials = root / ".claude/.credentials.json"
    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "launch-token"}}))
    event = {**REAL_EVENT, "rate_limit_info": {**REAL_EVENT["rate_limit_info"], "unifiedWindows": windows}}
    script = f"import json; print(json.dumps({event!r}))"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    env.update(HOME=str(root), CLAUDE_CONFIG_DIR=str(credentials.parent))
    with (
        patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(credentials.parent)}),
        patch.object(quota_module.Quota, "_claude_creds_source", lambda self: root / ".claude"),
    ):
        rc = agents.run_agent_proc(
            [sys.executable, "-c", script],
            env=env,
            logdir=root / "logs",
            label="agent-claude",
            provider="claude",
            quota_cfg=cfg,
        )
        reader = quota_module.Quota(cfg)
        fp = quota_module.Quota._fingerprint("launch-token")
        with patch.object(quota_module, "_http_get_json", side_effect=AssertionError("endpoint asked")):
            prov, _ = reader._claude_pass(fp, "launch-token", refresh=True, account=reader._claude_account_key())
    check("agent run exits cleanly", rc, 0)
    check("the agent's reading paces the next round without the endpoint", prov.available, True)
    check("no request was audited", (cfg.state.parent / "quota-requests").exists(), False)

print(f"\n{'PASS' if not fails else f'FAIL ({fails})'}")
sys.exit(1 if fails else 0)

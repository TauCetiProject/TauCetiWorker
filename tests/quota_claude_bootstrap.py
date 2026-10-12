#!/usr/bin/env python3
"""Bootstrap control: WHEN TauCeti may spend a Claude request to open a reset-but-unopened window.

Four properties, none of which the parser alone can give you:

  PURE OBSERVATION      Reading quota never spends. A dashboard refresh, `tauceti status`, and an
                        `auto` selection that inspects Claude and then picks codex must all make zero
                        Claude requests.
  LAUNCH STAGE          The one controlled side effect happens only when there is real work to run,
                        the non-model preflight passed, Claude is the model selected for that work,
                        and Claude's ONLY unresolved condition is an idle window whose siblings are
                        active with positive headroom.
  PACE POLICY           A bootstrap is spending, so it obeys the operator's curve. Under a curve whose
                        budget stays 0 for the first τ₀% of a window, a fresh window may not be opened
                        at all — and TauCeti cannot wait τ₀ out, because an unopened window has no
                        clock. It blocks and says so rather than manufacturing one.
  DURABLE RESERVATION   The claim is written, fsynced and locked BEFORE the request, shared by every
                        worker on the account. A crash between claim and request cannot license a
                        second one, and a ledger we cannot write fails closed.

Cache correctness is retained here too: only fully active payloads are cached, and only until the
earliest reset they represent.

Dependency-free; no network, no real `claude`.
"""

import os
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


os.environ.pop("TAUCETI_PACE", None)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
tc.quota._claude_keychain_creds = lambda: None  # macOS: never consult the real login Keychain


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


def active(session_pct=10, weekly_pct=10, session_in=2 * 3600, weekly_in=3 * 86400):
    return {
        "five_hour": {"utilization": session_pct, "resets_at": iso(session_in)},
        "seven_day": {"utilization": weekly_pct, "resets_at": iso(weekly_in)},
    }


def weekly(pct=10, delta=3 * 86400):
    return {"utilization": pct, "resets_at": iso(delta)}


IDLE = {"five_hour": None, "seven_day": weekly()}
MALFORMED = {"five_hour": {"utilization": 5, "resets_at": "nope"}, "seven_day": weekly()}
ABSENT = {"seven_day": weekly()}

CODEX_OK = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 1, "limit_window_seconds": 604800, "reset_after_seconds": 500000},
        "secondary_window": None,
    }
}

TOKEN = "tok-abc"
FP = tc.Quota._fingerprint(TOKEN)
_REAL_BOOTSTRAP_REQUEST = tc.Quota._claude_bootstrap_request


class Endpoint:
    """A scripted usage endpoint for both providers. Claude payloads are served in order (repeating the
    last), and calls are counted so a test can prove a cache HIT or a forced re-fetch."""

    def __init__(self, *payloads, code=200, retry_after=None, codex=CODEX_OK):
        self.payloads = list(payloads) or [{}]
        self.code, self.retry_after, self.codex = code, retry_after, codex
        self.calls = 0

    def __call__(self, url, headers, timeout=15):
        if url == tc.CODEX_USAGE_URL:
            return 200, self.codex, None
        self.calls += 1
        return self.code, self.payloads[min(self.calls - 1, len(self.payloads) - 1)], self.retry_after


class Bootstrapper:
    """Stands in for the one small `claude -p` request, counting how many were actually made."""

    def __init__(self, ok=True, detail="ok"):
        self.ok, self.detail, self.calls = ok, detail, 0

    def install(self):
        boot = self

        def _req(_self):
            boot.calls += 1
            return boot.ok, boot.detail

        tc.quota.Quota._claude_bootstrap_request = _req
        return self


def make_home(codex=False):
    home = Path(tempfile.mkdtemp())
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(tc.json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    if codex:
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        (home / ".codex" / "auth.json").write_text(
            tc.json.dumps({"tokens": {"access_token": "codex-tok", "account_id": "acct"}})
        )
    return home


def make_cfg(home, wid="test"):
    state = home / "state" / wid
    return tc.Config(
        wid=wid,
        home=home,
        data_home=home,
        state=state,
        checkout=home / "co",
        store_dir=home / "store",
        sbcache=state / "cache" / "scoreboard",
        logdir=home / "logs",
        quota_cache=state / "cache",
    )


def make_quota(home=None, wid="test", codex=False):
    return tc.Quota(make_cfg(home if home is not None else make_home(codex), wid))


def reason(prov):
    return prov.error or tc._unavail_reason(prov)[1]


# --- PURE OBSERVATION -----------------------------------------------------------------------------
boot = Bootstrapper().install()
ep = Endpoint(IDLE)
tc.quota._http_get_json = ep
q = make_quota()
for _ in range(3):
    p = q.claude()
check("reading claude() never spends, however often", boot.calls, 0)
check(
    "an idle window is reported, not acted on",
    (p.available, reason(p)),
    (False, "session window reset; awaiting initialization"),
)
check("...but it is flagged as initializable", (p.bootstrap_eligible, p.pending_bootstrap), (True, ["session"]))
q.choose("claude")
q.choose(None)
check("choose() is pure too", boot.calls, 0)

# `auto` with codex healthy and claude idle: codex is selected, and claude is never asked to spend.
home = make_home(codex=True)
q = make_quota(home)
model, snap = q.choose(None)
check("auto prefers the available codex", model, "codex")
check("auto selection made no claude request", boot.calls, 0)
check("...even though claude was inspected and found idle", snap["claude"].bootstrap_eligible, True)

# --- LAUNCH STAGE: the authorized case ------------------------------------------------------------
boot = Bootstrapper().install()
ep = Endpoint(IDLE, active(session_pct=2))  # idle first, initialized afterwards
tc.quota._http_get_json = ep
q = make_quota()
p = q.authorize_claude_launch()
check("an authorized launch makes exactly one request", boot.calls, 1)
check("it re-reads usage afterwards", ep.calls, 2)
check("the FRESH telemetry decides", (p.available, p.model), (True, "opus"))
check("...on the fresh numbers", [w.used for w in p.windows], [2.0, 10.0])
check(
    "the outcome is recorded in the shared ledger",
    [r.get("state") for r in q._read_bootstrap_ledger().values()],
    ["done"],
)

# Fresh telemetry that comes back WITHOUT headroom still blocks: a bootstrap buys a reading, not a task.
boot = Bootstrapper().install()
os.environ["TAUCETI_PACE"] = "0:0,100:0"  # every budget is 0 ⇒ τ₀ = 100 ⇒ not even initializable
q = make_quota()
tc.quota._http_get_json = Endpoint(IDLE)
p = q.authorize_claude_launch()
check("a curve with no budget anywhere never initializes", boot.calls, 0)
os.environ.pop("TAUCETI_PACE", None)

boot = Bootstrapper().install()
os.environ["TAUCETI_PACE"] = "0:20,100:20"  # flat 20% budget: positive at 0 (so initializing is allowed),
q = make_quota()  # and an exact equality afterwards, independent of wall-clock drift
tc.quota._http_get_json = Endpoint(
    IDLE, {"five_hour": {"utilization": 20, "resets_at": iso(3600)}, "seven_day": weekly()}
)
p = q.authorize_claude_launch()
check("bootstrap succeeded, but the fresh window has no headroom yet", boot.calls, 1)
check("...so no task starts", (p.available, p.windows[0].status), (False, "at-budget"))
os.environ.pop("TAUCETI_PACE", None)

# A failed bootstrap is an informative hard block, and is not retried for this episode.
boot = Bootstrapper(ok=False, detail="claude exited 1: Rate limit exceeded").install()
ep = Endpoint(IDLE)
tc.quota._http_get_json = ep
q = make_quota()
p = q.authorize_claude_launch()
check("a failed bootstrap does not make claude available", p.available, False)
check("the failure is reported verbatim", reason(p), "session bootstrap failed: claude exited 1: Rate limit exceeded")
p = q.authorize_claude_launch()
check("...and is not retried for the same episode", boot.calls, 1)
check("...with the diagnosis preserved", reason(p), "session bootstrap failed: claude exited 1: Rate limit exceeded")
check("...and the parent loop no longer schedules doomed surveys", p.bootstrap_eligible, False)

# --- PACE POLICY ----------------------------------------------------------------------------------
# A curve that holds the budget at 0 through 90% of a window forbids opening one: the request would
# spend against a 0% budget, and the plateau cannot be waited out on a window that has no clock.
boot = Bootstrapper().install()
os.environ["TAUCETI_PACE"] = "0:0,90:0,100:95"
tc.quota._http_get_json = Endpoint(IDLE)
q = make_quota()
p = q.authorize_claude_launch()
check("τ₀ > 0 ⇒ no bootstrap request", boot.calls, 0)
check("τ₀ > 0 ⇒ no task", (p.available, p.model), (False, None))
check("τ₀ > 0 ⇒ not even eligible", p.bootstrap_eligible, False)
check(
    "τ₀ > 0 ⇒ the reason names the plateau",
    reason(p),
    "session window reset; awaiting initialization (pace budget stays 0% through 90% of the window)",
)
check("τ₀ is read off the curve", tc.pace_zero_plateau(tc.parse_pace_curve("0:0,90:0,100:95")), 90.0)
check(
    "...and is 0 for curves that grant budget immediately",
    [tc.pace_zero_plateau(tc.parse_pace_curve(s)) for s in ("", "0:0,100:95", "0:10,100:100")],
    [0.0, 0.0, 0.0],
)
os.environ.pop("TAUCETI_PACE", None)

# The sibling window must itself be spendable. Every one of these forbids opening the idle one.
siblings = [
    # a flat curve makes "exactly at budget" exact, rather than a wall-clock race
    (
        "weekly at budget",
        {"five_hour": None, "seven_day": {"utilization": 30, "resets_at": iso(6 * 86400)}},
        "0:30,100:30",
    ),
    ("weekly over pace", {"five_hour": None, "seven_day": {"utilization": 90, "resets_at": iso(6 * 86400)}}, None),
    ("weekly exhausted", {"five_hour": None, "seven_day": {"utilization": 100, "resets_at": iso(6 * 86400)}}, None),
    ("weekly absent", {"five_hour": None}, None),
    ("weekly malformed", {"five_hour": None, "seven_day": {"utilization": 5, "resets_at": "nope"}}, None),
]
for label, payload, pace in siblings:
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint(payload)
    if pace:
        os.environ["TAUCETI_PACE"] = pace
    q = make_quota()
    p = q.authorize_claude_launch()
    check(f"idle session + {label} ⇒ no bootstrap", boot.calls, 0)
    check(f"idle session + {label} ⇒ not available", p.available, False)
    os.environ.pop("TAUCETI_PACE", None)

# Nor does an unreadable window of any other kind buy a request.
for label, payload in (("malformed", MALFORMED), ("absent", ABSENT)):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint(payload)
    p = make_quota().authorize_claude_launch()
    check(f"{label} telemetry never triggers a bootstrap request", boot.calls, 0)
    check(f"{label} telemetry fails closed", p.available, False)

# A weekly reset is treated exactly like a session reset (independent clocks, same machinery).
boot = Bootstrapper().install()
tc.quota._http_get_json = Endpoint(
    {"five_hour": {"utilization": 5, "resets_at": iso(3600)}, "seven_day": None},
    active(session_pct=5, weekly_pct=1),
)
p = make_quota().authorize_claude_launch()
check("a weekly reset bootstraps too", boot.calls, 1)
check("weekly reset resolves to an available provider", p.available, True)

# --- DURABLE, FLEET-WIDE RESERVATION ---------------------------------------------------------------
# Two workers, separate state dirs, same account: the reservation is shared, so ONE request is made.
boot = Bootstrapper().install()
tc.quota._http_get_json = Endpoint(IDLE)
home = make_home()
qa, qb = make_quota(home, "worker1"), make_quota(home, "worker2")
check("the two workers really are separate", qa.cache_dir != qb.cache_dir, True)
check("...and share one reservation file", qa._bootstrap_paths()[0], qb._bootstrap_paths()[0])
pa = qa.authorize_claude_launch()
pb = qb.authorize_claude_launch()
check("two workers, one idle episode ⇒ one request", boot.calls, 1)
check("the second worker is told why it is waiting", reason(pb), "session bootstrap attempted; awaiting fresh usage")

# The reservation key survives an access-token refresh: a new token is not a new reset episode.
before = qa._episode_key("session")
(home / ".claude" / ".credentials.json").write_text(tc.json.dumps({"claudeAiOauth": {"accessToken": "rotated-token"}}))
check("a token refresh does not create a new bootstrap allowance", qa._episode_key("session"), before)
qa.authorize_claude_launch()
check("...so no second request after a refresh", boot.calls, 1)

# An in-progress reservation (a worker that has not reported back) suppresses duplicates until stale.
boot = Bootstrapper().install()
tc.quota._http_get_json = Endpoint(IDLE)
q = make_quota()
ledger, _lock = q._bootstrap_paths()
ledger.parent.mkdir(parents=True, exist_ok=True)
ledger.write_text(tc.json.dumps({q._episode_key("session"): {"state": "in-progress", "at": int(time.time())}}))
p = q.authorize_claude_launch()
check("a live in-progress reservation suppresses the request", boot.calls, 0)
check("...and says who holds it", reason(p), "session bootstrap in progress (another worker holds the reservation)")
stale_at = int(time.time() - tc.CLAUDE_BOOTSTRAP_TIMEOUT_S - tc.CLAUDE_BOOTSTRAP_STALE_MARGIN_S - 1)
ledger.write_text(tc.json.dumps({q._episode_key("session"): {"state": "in-progress", "at": stale_at}}))
q.authorize_claude_launch()
check("a STALE in-progress reservation is reclaimed", boot.calls, 1)

# The claim is written BEFORE the request: a crash right after the request still leaves a reservation.
boot = Bootstrapper().install()
tc.quota._http_get_json = Endpoint(IDLE)
q = make_quota()
seen = {}


def _crashing(_self):
    seen["ledger"] = tc.json.loads(q._bootstrap_paths()[0].read_text())
    boot.calls += 1
    raise KeyboardInterrupt  # the process dies mid-request


tc.quota.Quota._claude_bootstrap_request = _crashing
try:
    q.authorize_claude_launch()
except KeyboardInterrupt:
    pass
check(
    "the reservation exists while the request is in flight",
    [r.get("state") for r in seen["ledger"].values()],
    ["in-progress"],
)
Bootstrapper().install()
p = make_quota(q.cfg.home, "another").authorize_claude_launch()
check(
    "a crashed worker's claim still suppresses the next one",
    reason(p),
    "session bootstrap in progress (another worker holds the reservation)",
)

# A reservation we cannot record fails CLOSED — an unenforceable claim must not license a request.
boot = Bootstrapper().install()
tc.quota._http_get_json = Endpoint(IDLE)
q = make_quota()
blocked = q._claude_creds_source() / tc.CLAUDE_QUOTA_DIRNAME
blocked.parent.mkdir(parents=True, exist_ok=True)
blocked.write_text("not a directory")  # the ledger dir cannot be created
p = q.authorize_claude_launch()
check("an unwritable reservation ⇒ no request", boot.calls, 0)
check("...and an honest reason", reason(p), "session bootstrap reservation unavailable (cannot lock the shared ledger)")

# --- CACHE CORRECTNESS (retained) ------------------------------------------------------------------
Bootstrapper().install()
ep = Endpoint(active())
tc.quota._http_get_json = ep
q = make_quota()
p1, p2 = q.claude(), q.claude()
check("a resolved payload is cached ⇒ one HTTP call", (p1.available, ep.calls), (True, 1))
entry = tc.json.loads((q.cache_dir / "quota-claude.json").read_text())
check(
    "the entry expires at the EARLIEST reset it represents",
    round(entry["valid_until"] - time.time()) in range(2 * 3600 - 2, 2 * 3600 + 1),
    True,
)
check("...far inside the 1h TTL, which alone would not have caught it", tc.QUOTA_TTL["claude"], 3600)

q = make_quota()
crossed = {"five_hour": {"utilization": 95, "resets_at": iso(-600)}, "seven_day": weekly()}
q._store_raw("claude", crossed, FP, time.time() - 600)
check("a complete response past its reset ⇒ not served", q._cached_raw("claude", FP), None)
(q.cache_dir / "quota-claude.json").write_text(
    tc.json.dumps({"fetched_at": int(time.time()), "fp": FP, "payload": crossed})
)
check("a legacy entry with no stored expiry is revalidated", q._cached_claude(FP), None)
check("...though the raw TTL layer would have served it", q._cached_raw("claude", FP) is not None, True)
(q.cache_dir / "quota-claude.json").write_text(
    tc.json.dumps({"fetched_at": int(time.time()), "fp": FP, "payload": ["not", "an", "object"]})
)
check("a cached non-object body is discarded, not parsed", q._cached_claude(FP), None)

for label, payload in (("idle", IDLE), ("malformed", MALFORMED), ("absent", ABSENT)):
    ep = Endpoint(payload)
    tc.quota._http_get_json = ep
    q = make_quota()
    q.claude()
    q.claude()
    check(f"{label} telemetry is never cached", (ep.calls, q._cached_raw("claude", FP)), (2, None))

# --- endpoint failures are named, and never bootstrapped --------------------------------------------
for code, want, ra in (
    (401, "claude usage HTTP 401 (access token expired or rejected; log in again)", None),
    (429, "claude usage HTTP 429 (usage endpoint rate-limited)", 580.0),
    (503, "claude usage HTTP 503", None),
):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint({}, code=code, retry_after=ra)
    p = make_quota().authorize_claude_launch()
    check(f"HTTP {code} names the status code", (p.available, p.error), (False, want))
    check(f"HTTP {code} makes no bootstrap request", (boot.calls, p.retry_after), (0, ra))

# A 200 whose body is not a JSON object is an informative hard error, not an AttributeError. (An empty
# body — [], "", 0 — is caught one step earlier as "response empty", which is equally informative.)
for body in (["five_hour"], "nope", 42, True):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint(body)
    p = make_quota().authorize_claude_launch()
    check(
        f"non-object body {body!r} ⇒ named error",
        p.error,
        f"claude usage response is not a JSON object (got {type(body).__name__})",
    )
    check(f"non-object body {body!r} ⇒ no request, no exception", (boot.calls, p.available), (0, False))
for body in ([], "", 0, None):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint(body)
    p = make_quota().authorize_claude_launch()
    check(f"empty body {body!r} ⇒ named error, no exception", (p.error, boot.calls), ("claude usage response empty", 0))

# --- the bootstrap subprocess is isolated -----------------------------------------------------------
tc.quota.Quota._claude_bootstrap_request = _REAL_BOOTSTRAP_REQUEST
q = make_quota()
os.environ.update(
    {
        "HOME": str(q.cfg.home),
        "CLAUDE_CONFIG_DIR": str(q.cfg.home / ".claude"),
        "ANTHROPIC_API_KEY": "sk-should-not-leak",
        "ANTHROPIC_AUTH_TOKEN": "leak",
        "CLAUDE_CODE_OAUTH_TOKEN": "leak",
        "ANTHROPIC_BASE_URL": "https://elsewhere.example",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDECODE": "1",
    }
)
argv, env, cwd = q._bootstrap_spec()
check("the request is one minimal `claude -p` turn", (argv[1], len(argv)), ("-p", 5))
check("the bootstrap uses exact Opus 5.5", argv[argv.index("--model") + 1], "claude-opus-5-5")
for var in (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
):
    check(f"{var} is dropped", var in env, False)
check("the measured account's HOME is kept", env.get("HOME"), str(q.cfg.home))
check("the measured account's CLAUDE_CONFIG_DIR is kept", env.get("CLAUDE_CONFIG_DIR"), str(q.cfg.home / ".claude"))
check("cwd is a real temp dir", Path(cwd).is_dir(), True)
check("cwd is outside the repository", REPO in Path(cwd).resolve().parents, False)
check("cwd carries no project config", list(Path(cwd).iterdir()), [])
os.rmdir(cwd)
for var in (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CONFIG_DIR",
):
    os.environ.pop(var, None)

scripts = Path(tempfile.mkdtemp())


def stub_claude(name, body):
    p = scripts / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


tc.quota.CLAUDE_CMD = stub_claude("ok.sh", "echo ok; exit 0")
check("a successful `claude -p` ⇒ (True, ok)", q._claude_bootstrap_request(), (True, "ok"))
tc.quota.CLAUDE_CMD = stub_claude("fail.sh", "echo 'Invalid API key' >&2; exit 3")
ok, detail = q._claude_bootstrap_request()
check(
    "a failing `claude -p` ⇒ (False, exit code + last line)",
    (ok, detail.endswith("exited 3: Invalid API key")),
    (False, True),
)
tc.quota.CLAUDE_CMD = str(scripts / "does-not-exist")
ok, detail = q._claude_bootstrap_request()
check("a missing claude binary is reported, not raised", (ok, detail.endswith("not found on PATH")), (False, True))
tc.quota.CLAUDE_CMD = stub_claude("hang.sh", "sleep 30")
tc.quota.CLAUDE_BOOTSTRAP_TIMEOUT_S = 1
ok, detail = q._claude_bootstrap_request()
check("a hanging claude is bounded by the timeout", (ok, detail.endswith("timed out after 1s")), (False, True))
tc.quota.CLAUDE_CMD = stub_claude("count.sh", "exit 0")
before = len(list(Path(tempfile.gettempdir()).glob("tauceti-quota-bootstrap-*")))
q._claude_bootstrap_request()
check("the temp cwd is cleaned up", len(list(Path(tempfile.gettempdir()).glob("tauceti-quota-bootstrap-*"))), before)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

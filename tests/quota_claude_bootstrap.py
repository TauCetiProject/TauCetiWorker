#!/usr/bin/env python3
"""Idle observations permit useful work without probes, and retain real restrictions."""

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import cli, loop, quota

NOW = 1_800_000_000.0


def iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat()


def payload(session=None, weekly=10):
    return {
        "five_hour": session,
        "seven_day": None if weekly is None else {"utilization": weekly, "resets_at": iso(NOW + 3 * 86400)},
    }


def reader(root, wid="a"):
    cfg = SimpleNamespace(home=root, wid=wid, state=root / "state" / wid, quota_cache=root / "state" / wid / "cache")
    q = quota.Quota(cfg)
    q._claude_creds_source = lambda: root / ".claude"
    return q


def run(root):
    q = reader(root)
    # Existing ledgers are ignored even when unusable as files.
    old = root / ".claude/.tauceti-quota/bootstrap.json"
    old.mkdir(parents=True)
    with patch.object(quota.subprocess, "run", side_effect=AssertionError("no dummy model calls")):
        for body in [payload(), payload(None, None), payload({"utilization": 1, "resets_at": iso(NOW + 7200)}, None)]:
            p = q._claude_from_payload(body)
            assert p.available and p.model == "opus"
            assert all(w.elapsed is None and w.used is None for w in p.windows if w.status == "idle")
            assert "awaiting" not in quota.quota_line({"claude": p})
        blocked = q._claude_from_payload(payload(None, 100))
        assert not blocked.available and quota._unavail_reason(blocked) == (False, "weekly exhausted")
        paced = q._claude_from_payload(payload(None, 90))
        assert not paced.available and quota._unavail_reason(paced)[0]
        assert paced.next_eligible > NOW and loop._ignore_quota_verdict(None, paced) == "over-pace"
        with patch.dict(os.environ, {"TAUCETI_PACE": "0:0,90:0,100:95"}):
            blocked = q._claude_from_payload(payload())
            assert not blocked.available and blocked.windows[0].status == "idle-blocked"
            assert loop._ignore_quota_verdict(None, blocked) == "wait"
        for bad in [{}, {"five_hour": None}, payload({"utilization": 5, "resets_at": None})]:
            assert not q._claude_from_payload(bad).available
        assert cli.build_parser().parse_args(["_round", "--claude-bootstrap"]).claude_bootstrap

    # Cache-first and forced readers share a stable, permitted idle observation.
    workers = [reader(root, str(i)) for i in range(5)]
    with patch.object(quota, "_http_get_json", return_value=(200, payload(), None)) as http:
        for _ in range(3):
            for w in workers:
                assert w._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert http.call_count == 1
        with patch.object(quota.time, "time", return_value=NOW + quota.CLAUDE_USAGE_REFRESH_S):
            for w in workers:
                assert w._claude_pass("fp", "token", refresh=True, account="account")[0].available
            assert http.call_count == 2

    # A failed refresh preserves an unexpired idle observation, then expires at TTL.
    for failure in [(429, {}, 600), quota.UsageTransportError("offline")]:
        q._clear_cooldown("claude", "account", "fp")
        q._store_raw("claude", payload(), "fp", NOW + 3600, NOW)
        kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
        with (
            patch.object(quota.time, "time", return_value=NOW + 1201),
            patch.object(quota, "_http_get_json", **kwargs) as http,
        ):
            assert q._claude_pass("fp", "token", refresh=True, account="account")[0].available
            assert reader(root, "peer")._claude_pass("fp", "token", refresh=True, account="account")[0].available
            assert http.call_count == 1
        with patch.object(quota.time, "time", return_value=NOW + 3601):
            assert q._cached_claude("fp") is None
        q._clear_cooldown("claude", "account", "fp")

    # Cache validity derives from fetched_at, not a reset reinterpreted as idle today.
    active = payload({"utilization": 10, "resets_at": iso(NOW + 100)})
    q._store_raw("claude", active, "fp", fetched_at=NOW)  # legacy entry without valid_until
    for after in [100, 161, 500]:
        with patch.object(quota.time, "time", return_value=NOW + after):
            assert q._cached_claude("fp") is None
            q._start_cooldown("claude", "account", 429, 600, "fp")
            with patch.object(quota, "_http_get_json", side_effect=AssertionError("respect cooldown")):
                p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
                assert not p.available and p.telemetry_unavailable
            q._clear_cooldown("claude", "account", "fp")
    with patch.object(quota.time, "time", return_value=NOW + 161):
        with patch.object(quota, "_http_get_json", side_effect=quota.UsageTransportError("offline")):
            p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
            assert not p.available and p.telemetry_unavailable
        q._clear_cooldown("claude", "account", "fp")
        # A freshly observed elapsed clock IS idle and must be shareable.
        with patch.object(quota, "_http_get_json", return_value=(200, active, None)) as http:
            assert q._claude_pass("fp", "token", refresh=True, account="account")[0].available
            assert reader(root, "peer")._claude_pass("fp", "token", refresh=True, account="account")[0].available
            assert http.call_count == 1
    # Neither an overlong stored expiry nor absent valid_until extends the active reset.
    q._store_raw("claude", active, "fp", NOW + 3600, NOW)
    with patch.object(quota.time, "time", return_value=NOW + 161):
        assert q._cached_claude("fp") is None

    # Idle observations respect identity, TTL, original over-pace verdict and known refusals.
    q._store_raw("claude", payload(None, 90), "fp", NOW + 3600, NOW)
    assert q._cached_claude("other") is None
    with patch.object(quota.time, "time", return_value=NOW + 1200):
        assert not q._from_cached_claude(q._cached_claude("fp"))[0].available
    with patch.object(quota.time, "time", return_value=NOW + 3601):
        assert q._cached_claude("fp") is None
    for body in [payload(), payload({"utilization": 1, "resets_at": iso(NOW + 7200)})]:
        q._raw_path("claude", "fp").unlink(missing_ok=True)
        q._usage_token_fps = {"claude": q._fingerprint("token")}
        q.note_model_refusal("claude", q._fingerprint("token"))
        with patch.object(quota, "_http_get_json", return_value=(200, body, None)) as http:
            p, _ = reader(root, "peer")._claude_pass("fp", "token", refresh=True, account="account")
            assert p.model_refusal and not p.available
            assert http.call_count == 1
            with patch.object(quota.time, "time", return_value=NOW + quota.RATE_LIMIT_RECHECK_MAX_S + 1):
                p, _ = q._claude_pass("fp", "token", account="account")
                assert p.available and not p.model_refusal
                assert http.call_count == 1  # expiry does not demand fresh/active telemetry
        for path in q._model_refusal_paths("claude", q._fingerprint("token")):
            path.unlink(missing_ok=True)
    # A newer idle observation must not break existing credential rejection/recovery.
    q._raw_path("claude", "fp").unlink(missing_ok=True)
    with patch.object(quota, "_http_get_json", return_value=(401, {}, None)):
        assert not q._claude_pass("fp", "token", refresh=True, account="account")[0].available
    with patch.object(quota, "_http_get_json", return_value=(200, payload(), None)):
        assert q._claude_pass("fp", "token", refresh=True, account="account")[0].available
        assert not q._auth_refusal_path("claude", "fp").exists()


with tempfile.TemporaryDirectory() as td, patch.object(quota.time, "time", return_value=NOW), patch.dict(os.environ):
    os.environ.pop("TAUCETI_PACE", None)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    run(Path(td))
print("PASS: idle selection, sharing, reset/TTL expiry, refusal lifetime and auth recovery")

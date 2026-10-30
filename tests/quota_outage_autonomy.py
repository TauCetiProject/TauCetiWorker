#!/usr/bin/env python3
"""Telemetry outages permit explicit unpaced work, with auditable, bounded HTTP attempts."""

import fcntl
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, cli, loop, quota, quota_audit


def worker(root):
    cfg = SimpleNamespace(
        home=root, wid="worker1", state=root / "state/worker1", quota_cache=root / "state/worker1/cache"
    )
    q = quota.Quota(cfg)
    q._claude_creds_source = lambda: root / ".claude"
    q._idle_notes = lambda _readings: ({}, False)
    return q


def test_request_records_and_deadlines(root):
    q = worker(root)
    headers = Message()
    headers["Retry-After"] = "7200"
    headers["request-id"] = "req_test_429"
    error = urllib.error.HTTPError(quota.CLAUDE_USAGE_URL, 429, "limited", headers, io.BytesIO(b"private response"))
    with patch.object(quota.urllib.request, "urlopen", side_effect=error) as http:
        p, _ = q._claude_pass("fingerprint", "private-token", refresh=True, account="account")
        assert p.telemetry_unavailable and loop._ignore_quota_verdict(None, p) == "telemetry-unavailable"
        for _ in range(3):
            p, _ = worker(root)._claude_pass("fingerprint", "private-token", refresh=True, account="account")
            assert p.telemetry_unavailable
        assert http.call_count == 1
    remaining, code = q._cooldown("claude", "account")
    assert 7190 < remaining <= 7200 and code == 429
    audit = root / "state/quota-requests/requests.jsonl"
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [r["event"] for r in records] == ["request", "response"]
    assert records[0]["request"] == records[1]["request"]
    assert records[1]["retry_after_raw"] == "7200" and records[1]["retry_after_s"] == 7200
    assert records[1]["server_request_id"] == "req_test_429" and records[1]["status"] == 429
    assert records[1]["duration_s"] >= 0 and records[1]["worker"] == "worker1"
    assert "private-token" not in audit.read_text() and "private response" not in audit.read_text()
    assert os.stat(audit).st_mode & 0o777 == 0o600
    assert (
        loop._recheck_in(
            quota.Provider("claude", False, None, retry_after=0.2, telemetry_unavailable=True), time.time()
        )
        == 1
    )
    assert (
        loop._recheck_in(
            quota.Provider("claude", False, None, retry_after=300.2, telemetry_unavailable=True), time.time()
        )
        == 301
    )
    assert loop._recheck_in(quota.Provider("claude", False, None, retry_after=1), time.time()) == loop.POLL
    assert quota._usage_retry_after("Infinity") is None
    assert quota._usage_retry_after("NaN") is None
    assert quota._usage_retry_after("1000000000") is None
    assert quota._cooldown_left({"until": time.time() + 1e9, "wait": 1e9, "code": 429}) == (0, None)
    future = datetime.fromtimestamp(time.time() + 7200, UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 7190 < quota._usage_retry_after(future) <= 7200


def test_only_transient_failures_are_overridden(root):
    for code in (401, 403, 404, 429, 500, 502, 503, 504, 529):
        q = worker(root / str(code))
        with patch.object(quota, "_http_get_json", return_value=(code, {}, None)):
            p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
        transient = code in quota._TRANSIENT_USAGE_STATUSES
        assert p.telemetry_unavailable == transient
        assert loop._ignore_quota_verdict(None, p) == ("telemetry-unavailable" if transient else "wait")
        with patch.object(loop, "choose_model", return_value=(None, {"claude": p})):
            if transient:
                assert loop.resolve_work_model(q.cfg, "claude", dry=False, ignore_quota=True) == ("claude", False)
            try:
                loop.resolve_work_model(q.cfg, "claude", dry=False, ignore_quota=False)
            except loop.NoProgress:
                pass
            else:
                raise AssertionError("normal paced work must still require telemetry")
    for exception, transient in ((quota.UsageTransportError("offline"), True), (quota.GitHubError("bad JSON"), False)):
        q = worker(root / type(exception).__name__)
        with patch.object(quota, "_http_get_json", side_effect=exception):
            p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
        assert p.telemetry_unavailable == transient
    q = worker(root / "malformed")
    with patch.object(quota, "_http_get_json", return_value=(200, {"five_hour": "invalid"}, None)):
        p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
    assert not p.telemetry_unavailable and loop._ignore_quota_verdict(None, p) == "wait"
    q = worker(root / "codex")
    q._codex_credentials_store = lambda: None
    q._codex_creds = lambda: {"tokens": {"access_token": "token", "account_id": "account"}}
    with patch.object(quota, "mirror_creds"), patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)):
        assert q.codex(refresh=True).telemetry_unavailable
        assert q.codex(refresh=True).telemetry_unavailable


def test_known_blocks_survive_telemetry_failure(root):
    q = worker(root)
    now = time.time()
    reset = now + 7200
    payload = {
        "five_hour": {"utilization": 100, "resets_at": datetime.fromtimestamp(reset, UTC).isoformat()},
        "seven_day": {"utilization": 20, "resets_at": datetime.fromtimestamp(now + 86400, UTC).isoformat()},
    }
    q._store_raw("claude", payload, "fp", reset, now - quota.QUOTA_TTL["claude"] - 1)
    with patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)):
        p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
    assert p.telemetry_unavailable and any(w.status == "exhausted" for w in p.windows)
    assert loop._ignore_quota_verdict(None, p) == "wait"
    assert 10 <= loop._recheck_in(p, reset - 10) <= 11
    with patch.object(quota.time, "time", return_value=reset + 1):
        p = q._telemetry_failure("claude", "fp", "offline")
        assert not p.windows and loop._ignore_quota_verdict(None, p) == "telemetry-unavailable"
    # Token changes cannot inherit a different credential's negative observation.
    assert not q._telemetry_failure("claude", "other-fp", "offline").windows
    for code in (401, 403):
        q = worker(root / f"auth-{code}")
        with patch.object(quota, "_http_get_json", return_value=(code, {}, None)):
            q._claude_pass("fp", "token", refresh=True, account="account")
        with patch.object(quota, "_http_get_json", return_value=(503, {}, None)):
            p, _ = q._claude_pass("fp", "token", refresh=True, account="account")
        if code == 401:
            assert not p.telemetry_unavailable and str(code) in p.error
        else:
            assert p.telemetry_unavailable  # a previous bare 403 did not prove credential rejection
    q = worker(root / "auth-401")
    with (
        patch.object(quota, "_http_get_json", return_value=(200, payload, None)),
        patch.object(quota.time, "time", return_value=time.time() + 901),
    ):
        q._claude_pass("fp", "token", refresh=True, account="account")
    assert not q._auth_refusal_path("claude", "fp").exists()
    q = worker(root / "rotated-auth")
    with patch.object(quota, "_http_get_json", return_value=(401, {}, None)):
        q._claude_pass("old-fp", "old-token", refresh=True, account="account")
    with patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)) as http:
        rejected, _ = q._claude_pass("old-fp", "old-token", refresh=True, account="account")
        renewed, _ = worker(root / "rotated-auth")._claude_pass("new-fp", "new-token", refresh=True, account="account")
        assert http.call_count == 1 and not rejected.telemetry_unavailable and renewed.telemetry_unavailable
    q = worker(root / "forbidden-auth")
    with patch.object(quota, "_http_get_json", side_effect=[(403, {}, 7200), (429, {}, 7200)]) as http:
        refused, _ = q._claude_pass("old-fp", "old-token", refresh=True, account="account")
        assert not refused.telemetry_unavailable and q._cooldown("claude", "account")[0] == 0
        assert refused.retry_after is None
        renewed, _ = worker(q.cfg.home)._claude_pass("new-fp", "new-token", refresh=True, account="account")
        assert renewed.telemetry_unavailable and http.call_count == 2


def test_loop_attempts_work_then_backs_off(root):
    q = worker(root)
    p = quota.Provider(
        "claude", False, None, error="claude usage HTTP 429", retry_after=7200, telemetry_unavailable=True
    )
    sleeps, events = [], []

    def stop(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    with (
        patch.object(loop, "choose_model", return_value=(None, {"claude": p})),
        patch.object(loop, "github_budget", return_value={}),
        patch.object(loop, "run_round_subprocess", return_value=loop.EX_NOPROGRESS) as run,
        patch.object(loop, "runtime_snapshot", return_value={"failure_reason": "reviewer provider unavailable"}),
        patch.object(loop, "report_runtime", side_effect=lambda state=None, **kw: events.append((state, kw))),
        patch.object(loop.time, "sleep", side_effect=stop),
    ):
        args = SimpleNamespace(ignore_quota=True, bubble=False, quota_cmd=None)
        loop.cmd_loop(args, SimpleNamespace(wid=q.cfg.wid), only=["review"], agent="claude")
    assert run.call_count == 1
    assert sleeps == [min(loop.BACKOFF_BASE * 2, loop.BACKOFF_MAX)]
    assert any(state == "backoff" and "provider unavailable" in details["detail"] for state, details in events)


def test_loop_handoff_preserves_operator_choice(root):
    idle = quota.Provider("claude", False, None, bootstrap_eligible=True, pending_bootstrap=["session"])
    outage = quota.Provider("claude", False, None, telemetry_unavailable=True, error="claude usage HTTP 429")
    for ignored in (False, True):

        def child(tail, ignored=ignored):
            args = cli.build_parser().parse_args(["_round", *tail])
            assert args.loop_paced and args.ignore_quota == ignored and args.claude_bootstrap
            with patch.object(loop, "choose_model", return_value=(None, {"claude": outage})):
                try:
                    loop.resolve_work_model(
                        object(), "claude", dry=False, ignore_quota=args.ignore_quota, already_paced=args.loop_paced
                    )
                except loop.NoProgress:
                    allowed = False
                else:
                    allowed = True
            assert allowed == ignored
            raise KeyboardInterrupt

        with (
            patch.object(loop, "choose_model", return_value=(None, {"claude": idle})),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", side_effect=child) as run,
        ):
            args = SimpleNamespace(ignore_quota=ignored, bubble=False, quota_cmd=None)
            loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["review"], agent="claude")
            assert run.call_count == 1


def test_transient_failures_share_backoff(root):
    for error in (503, quota.UsageTransportError("offline")):
        q = worker(root / str(type(error).__name__))
        kwargs = {"return_value": (error, {}, None)} if isinstance(error, int) else {"side_effect": error}
        with patch.object(quota, "_http_get_json", **kwargs) as http:
            for _ in range(3):
                p, _ = worker(q.cfg.home)._claude_pass("fp", "token", refresh=True, account="account")
                assert p.telemetry_unavailable and 0 < p.retry_after <= quota.RATE_LIMIT_RECHECK_MAX_S
            assert http.call_count == 1


def test_failed_bootstrap_stops_before_another_telemetry_read(root):
    q = worker(root)
    idle = quota.Provider(
        "claude",
        False,
        None,
        [quota.Window("session", None, 0, None, "idle")],
        bootstrap_eligible=True,
        pending_bootstrap=["session"],
    )
    with (
        patch.object(q, "claude", return_value=idle) as read,
        patch.object(q, "_episode_key", return_value="episode"),
        patch.object(q, "_reserve_bootstrap", return_value=(True, "")),
        patch.object(q, "_claude_bootstrap_request", return_value=(False, "model refused")),
        patch.object(q, "_settle_bootstrap"),
        patch.object(q, "_forget_raw"),
    ):
        result = q.authorize_claude_launch()
    assert read.call_count == 1 and not result.telemetry_unavailable
    assert loop._ignore_quota_verdict(None, result) == "wait"
    assert "bootstrap failed: model refused" in result.windows[0].detail


def test_changed_unknown_account_does_not_inherit_long_wait(root):
    q = worker(root)
    now = time.time()
    payload = {
        "five_hour": {"utilization": 1, "resets_at": datetime.fromtimestamp(now + 7200, UTC).isoformat()},
        "seven_day": {"utilization": 1, "resets_at": datetime.fromtimestamp(now + 3 * 86400, UTC).isoformat()},
    }
    with (
        patch.object(quota.time, "time", return_value=now),
        patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)),
    ):
        q._claude_pass("old", "old-token", refresh=True, account="creds:/source")
    with (
        patch.object(quota.time, "time", return_value=now + 901),
        patch.object(quota, "_http_get_json", return_value=(200, payload, None)) as http,
    ):
        old, _ = q._claude_pass("old", "old-token", refresh=True, account="creds:/source")
        assert not old.available and http.call_count == 0  # same token still honors its whole ban
        changed, _ = q._claude_pass("new", "new-token", refresh=True, account="creds:/source")
        assert changed.available and http.call_count == 1
        old, _ = q._claude_pass("old", "old-token", refresh=True, account="creds:/source")
        assert not old.available and http.call_count == 1  # switching back preserves its original ban
    q = worker(root / "short-new-ban")
    with (
        patch.object(quota.time, "time", return_value=now),
        patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)),
    ):
        q._claude_pass("old", "old-token", refresh=True, account="creds:/source")
    with (
        patch.object(quota.time, "time", return_value=now + 901),
        patch.object(quota, "_http_get_json", return_value=(429, {}, 60)) as http,
    ):
        for _ in range(2):
            p, _ = q._claude_pass("new", "new-token", refresh=True, account="creds:/source")
            assert p.telemetry_unavailable and 0 < p.retry_after <= 60
        assert http.call_count == 1

    with (
        patch.object(quota.time, "time", return_value=now + 962),
        patch.object(quota, "_http_get_json", return_value=(200, payload, None)) as http,
    ):
        assert q._claude_pass("new", "new-token", refresh=True, account="creds:/source")[0].available
        assert not q._claude_pass("old", "old-token", refresh=True, account="creds:/source")[0].available
        assert http.call_count == 1


def test_cli_preserves_soft_handoff_without_granting_outage_permission(root):
    outage = quota.Provider("claude", False, None, error="usage unavailable", telemetry_unavailable=True)
    paced = quota.Provider("claude", False, None, [quota.Window("session", 20, 1, None, "over-pace")])
    for ignored, status in ((False, outage), (True, outage), (False, paced)):
        args = cli.build_parser().parse_args(
            [
                "_round",
                "--worker-id",
                "default",
                "--agent",
                "claude",
                "--loop-paced",
                *(["--ignore-quota"] if ignored else []),
            ]
        )
        with (
            patch("tauceti_worker.security_audit.require_admin"),
            patch("tauceti_worker.github_outbox.drain", return_value=[]),
            patch.object(cli, "api_mode", return_value=False),
            patch.object(loop, "choose_model", return_value=(None, {"claude": status})),
            patch.dict(
                os.environ,
                {
                    "TAUCETI_ACTIVITY_DIR": str(root / "activity"),
                    "TAUCETI_TEST_SLEEP": "",
                    "TAUCETI_TEST_HOLD": "",
                    "TAUCETI_TEST_HEARTBEAT": "",
                },
            ),
            patch.multiple(
                cli,
                Config=DEFAULT,
                RoundContext=DEFAULT,
                Worker=DEFAULT,
                GitHub=DEFAULT,
                ReviewState=DEFAULT,
                Counters=DEFAULT,
                Claims=DEFAULT,
                set_log_file=DEFAULT,
                configure_api=DEFAULT,
                run_round=DEFAULT,
            ) as mocks,
        ):
            mocks["run_round"].return_value = 0
            if not ignored and status is outage:
                try:
                    cli.cmd_work(args, only=["security-audit"], agent="claude", one_round=True)
                except loop.NoProgress:
                    pass
                else:
                    raise AssertionError("normal loop child must not gain outage permission")
                mocks["run_round"].assert_not_called()
            else:
                assert cli.cmd_work(args, only=["security-audit"], agent="claude", one_round=True) == 0
                assert mocks["run_round"].call_args.args[1].ignore_quota == ignored


def test_real_model_refusal_is_scoped_bounded_and_recoverable(root):
    q = worker(root)
    credentials = root / ".claude/.credentials.json"
    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "launch-token"}}))
    event = {
        "type": "result",
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "You've hit your session limit · resets 9:30pm (UTC)",
    }
    script = (
        "import json,sys,os; from pathlib import Path; "
        "(Path(os.environ['CLAUDE_CONFIG_DIR'])/'.credentials.json').write_text("
        "json.dumps({'claudeAiOauth':{'accessToken':'different-token'}})); "
        "print(json.dumps(" + repr(event) + ")); sys.exit(1)"
    )
    env = {
        **os.environ,
        "HOME": str(root),
        "CLAUDE_CONFIG_DIR": str(credentials.parent),
        "TAUCETI_CLAUDE_BILLING": "subscription",
    }
    with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(credentials.parent)}):
        rc = agents.run_agent_proc(
            [sys.executable, "-c", script],
            env=env,
            logdir=root / "logs",
            label="agent-claude",
            provider="claude",
            quota_cfg=q.cfg,
        )
    assert rc == 1 and agents.take_last_agent_infra_failure() == "provider subscription limit reached"
    with patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)):
        refused, _ = q._claude_pass("old", "launch-token", refresh=True, account="account")
        assert refused.model_refusal and loop._ignore_quota_verdict(None, refused) == "wait"
        changed, _ = q._claude_pass("new", "different-token", refresh=True, account="account")
        assert changed.telemetry_unavailable and not changed.model_refusal
    with patch.object(quota.time, "time", return_value=time.time() + 901):
        retried, _ = q._claude_pass("old", "launch-token", refresh=True, account="account")
        assert retried.telemetry_unavailable and not retried.model_refusal
    q.note_model_refusal("claude", quota.Quota._fingerprint("launch-token"))
    with (
        patch.object(quota.time, "time", return_value=time.time() + 7201),
        patch.object(quota, "_http_get_json", return_value=(200, {"five_hour": None}, None)),
    ):
        q._claude_pass("old", "launch-token", refresh=True, account="account")
    assert not q._model_refusal_path("claude", quota.Quota._fingerprint("launch-token")).exists()


def test_readonly_credentials_share_model_refusals(root):
    q = worker(root)
    source = root / ".claude"
    source.mkdir()
    (source / quota.CLAUDE_QUOTA_DIRNAME).write_text("read-only credential mount")
    credential = quota.Quota._fingerprint("token")
    q.note_model_refusal("claude", credential)
    sibling = worker(root)
    with patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)):
        status, _ = sibling._claude_pass("fp", "token", refresh=True, account="account")
    assert status.model_refusal
    rejected = quota.Provider("claude", False, None, error="claude usage HTTP 401")
    assert sibling._with_model_refusal("claude", rejected) is rejected
    exhausted = quota.Provider(
        "claude", False, None, [quota.Window("session", 100, 50, time.time() + 7200, "exhausted")]
    )
    assert sibling._with_model_refusal("claude", exhausted) is exhausted
    with (
        patch.object(quota.time, "time", return_value=time.time() + 7201),
        patch.object(quota, "_http_get_json", return_value=(200, {"five_hour": None}, None)),
    ):
        sibling._claude_pass("fp", "token", refresh=True, account="account")
    assert all(
        not p.exists()
        for p in sibling._model_refusal_paths("claude", credential)
        if p.parent != source / quota.CLAUDE_QUOTA_DIRNAME / "usage"
    )
    with patch.object(q, "_claude_creds", return_value=({"accessToken": 99}, False)):
        assert q.launch_credential("claude") is None
    assert (
        agents.run_agent_proc(
            [sys.executable, "-c", "print('ok')"],
            env=dict(os.environ),
            logdir=root / "logs",
            label="agent-claude",
            provider="claude",
            quota_cfg=object(),
        )
        == 0
    )


def test_diagnostics_never_gate_requests(root):
    q = worker(root)
    directory = root / "state/quota-requests"
    directory.parent.mkdir(parents=True)
    directory.write_text("not a directory")
    with patch.object(quota, "_http_get_json", return_value=(429, {}, 900)) as http:
        assert q._claude_pass("fp", "token", refresh=True, account="account")[0].telemetry_unavailable
        assert http.call_count == 1
    directory.unlink()
    directory.mkdir()
    lock = os.open(directory / "requests.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quota_audit.record(directory, event="test")
        assert not (directory / "requests.jsonl").exists()
    finally:
        os.close(lock)
    with patch.object(quota_audit, "MAX_BYTES", 1):
        for i in range(10):
            quota_audit.record(directory, event="test", number=i)
    files = sorted(directory.glob("requests.jsonl*"))
    assert len(files) == 3
    assert sorted(json.loads(f.read_text())["number"] for f in files) == [7, 8, 9]


if __name__ == "__main__":
    for test in (
        test_request_records_and_deadlines,
        test_only_transient_failures_are_overridden,
        test_known_blocks_survive_telemetry_failure,
        test_loop_attempts_work_then_backs_off,
        test_loop_handoff_preserves_operator_choice,
        test_transient_failures_share_backoff,
        test_failed_bootstrap_stops_before_another_telemetry_read,
        test_changed_unknown_account_does_not_inherit_long_wait,
        test_cli_preserves_soft_handoff_without_granting_outage_permission,
        test_real_model_refusal_is_scoped_bounded_and_recoverable,
        test_readonly_credentials_share_model_refusals,
        test_diagnostics_never_gate_requests,
    ):
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
            print(f"PASS {test.__name__}")

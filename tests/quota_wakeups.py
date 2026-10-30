#!/usr/bin/env python3
"""Exercise quota wake scheduling with real provider policy and no live requests."""

import dataclasses
import json
import os
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import loop, quota, quota_wait


class Clock:
    def __init__(self):
        self.now = 1800000000.0
        self.start = self.now
        self.mono = 0.0
        self.events = []

    def sleep(self, seconds):
        assert 0 < seconds <= 30, seconds
        self.now += seconds
        self.mono += seconds
        for at, action in list(self.events):
            if self.mono >= at:
                self.events.remove((at, action))
                action()


def claude_payload(now, used=1):
    return {
        "five_hour": {"utilization": used, "resets_at": datetime.fromtimestamp(now + 7200, UTC).isoformat()},
        "seven_day": {"utilization": used, "resets_at": datetime.fromtimestamp(now + 3 * 86400, UTC).isoformat()},
    }


def codex_payload():
    return {
        "rate_limit": {
            "primary_window": {
                "used_percent": 99,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 600000,
            }
        }
    }


def credentials(home, token="A"):
    p = home / ".claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": 9999999999999}}))


@contextmanager
def fixture(agent="auto", *, isolated=False, readonly=False):
    with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
        root = Path(raw)
        home = root / "home"
        credentials(home)
        (home / ".codex").mkdir()
        (home / ".codex/auth.json").write_text(json.dumps({"tokens": {"access_token": "codex", "account_id": "C"}}))
        source = root / "source" if isolated else home / ".claude"
        if isolated:
            source.mkdir()
            (source / ".credentials.json").write_text((home / ".claude/.credentials.json").read_text())
            (home / ".claude/.tauceti-creds-source").write_text(str(source))
        if readonly:
            (source / ".tauceti-quota").write_text("simulate a read-only credential mount")
        cfg = SimpleNamespace(
            home=home, state=root / "state/worker", quota_cache=root / "state/worker/cache", wid="test"
        )
        clock, calls = Clock(), []

        def http(url, headers):
            provider = "codex" if url == quota.CODEX_USAGE_URL else "claude"
            calls.append((provider, clock.mono))
            if provider == "codex":
                return 200, codex_payload(), None
            if headers["Authorization"] == "Bearer A":
                return 429, {}, 3600
            return 200, claude_payload(clock.now), None

        stack.enter_context(patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": ""}))
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TAUCETI_AUTO_REFRESH"):
            os.environ.pop(key, None)
        stack.enter_context(patch.object(quota.time, "time", side_effect=lambda: clock.now))
        stack.enter_context(patch.object(quota.time, "monotonic", side_effect=lambda: clock.mono))
        stack.enter_context(patch.object(quota, "_http_get_json", side_effect=http))
        stack.enter_context(patch.object(quota_wait.random, "uniform", return_value=1.0))
        stack.enter_context(patch.object(loop, "report_runtime"))
        stack.enter_context(patch.object(quota_wait, "report_runtime"))
        quota._COOLDOWNS.clear()
        wait = quota_wait.QuotaWait(cfg, agent, wall=lambda: clock.now, monotonic=lambda: clock.mono, sleep=clock.sleep)
        yield SimpleNamespace(cfg=cfg, clock=clock, calls=calls, wait=wait, source=source, home=home)


def select(f, agent="auto"):
    return loop.choose_model(f.cfg, agent, None, refresh=True, renew=True, _wait=f.wait)


def publish(f, token="B", used=1):
    (f.source / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    q = quota.Quota(f.cfg)
    if f.source != f.home / ".claude":
        # Cache publication belongs to another worker using the canonical source.
        q._claude_usage_directory = f.source / ".tauceti-quota/usage"
    if (f.source / ".tauceti-quota").is_file():
        q._claude_usage_directory = q._claude_usage_fallback()
    q._store_raw("claude", claude_payload(f.clock.now, used), q._fingerprint(token), f.clock.now + 7200)


def test_original_deadline_and_no_side_effects():
    with fixture() as f:
        assert select(f)[0] is None
        original = dict(f.wait.deadlines)
        with ExitStack() as s:
            traps = []
            for target in ("_http_get_json", "mirror_creds"):
                traps.append(s.enter_context(patch.object(quota, target, side_effect=AssertionError(target))))
            traps.append(s.enter_context(patch.object(quota.Quota, "_usage_lock", side_effect=AssertionError("lock"))))
            traps.append(
                s.enter_context(patch.object(quota.subprocess, "run", side_effect=AssertionError("subprocess")))
            )
            traps.append(s.enter_context(patch.object(Path, "mkdir", side_effect=AssertionError("mkdir"))))
            f.wait.wait()
            for trap in traps:
                trap.assert_not_called()
        assert f.clock.mono == 3600
        assert f.wait.deadlines == original
        assert f.calls == [("codex", 0), ("claude", 0)]
        select(f)
        assert [p for p, at in f.calls if at == 3600] == ["codex", "claude"]


def test_shared_recovery(*, isolated=False, readonly=False):
    with fixture(isolated=isolated, readonly=readonly) as f:
        assert select(f)[0] is None
        deadline = f.wait.deadlines["codex"]
        f.clock.events.append((100, lambda: publish(f)))
        f.wait.wait()
        assert 100 <= f.clock.mono <= 130
        assert select(f)[0] == "claude"
        assert f.wait.deadlines["codex"] == deadline
        assert f.calls == [("codex", 0), ("claude", 0)], f.calls
        if isolated:
            assert json.loads((f.home / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "B"


def test_source_cooldown_expiry():
    with fixture() as f:
        assert select(f)[0] is None
        f.clock.events.append((100, lambda: credentials(f.home, "B")))
        f.wait.wait()
        assert select(f)[0] is None
        assert f.wait.deadlines["claude"] == f.clock.start + 900
        while f.clock.mono < 900:
            f.wait.wait()
            model, _ = select(f)
        assert model == "claude"
        assert f.clock.mono == 900
        assert f.calls == [("codex", 0), ("claude", 0), ("claude", 900)]


def test_sibling_miss_and_repeated_updates():
    with fixture() as f:
        select(f)
        deadline = f.wait.deadlines["codex"]
        for i in range(1, 25):
            f.clock.events.append((i * 100, lambda: publish(f, "A", used=99)))
        while f.clock.mono < 3600:
            f.wait.wait()
            select(f)
            assert f.wait.deadlines["codex"] == deadline or f.clock.mono == 3600
        assert [at for p, at in f.calls if p == "codex"] == [0, 3600], f.calls


def test_entry_race_and_persistent_mirror_failure():
    with fixture(isolated=True) as f:
        select(f)
        publish(f)
        with patch.object(quota, "mirror_creds", return_value=None):
            for _ in range(4):
                previous = f.clock.mono
                f.wait.wait()
                assert f.clock.mono >= previous + 25
                select(f)
        assert [p for p, _ in f.calls].count("claude") <= 2


def test_failed_observation_and_signals():
    with fixture() as f:
        select(f)
        with patch.object(quota_wait, "observe", return_value=None):
            f.wait.wait()
        assert f.clock.mono == 3600
        f.wait.reset()
        select(f)
        with patch.object(quota_wait, "_read", side_effect=RuntimeError("bad peer shape")):
            f.wait.wait()
        assert f.clock.mono == 7200
        with patch.object(quota_wait, "_read", side_effect=KeyboardInterrupt):
            try:
                f.wait.observations()
                raise AssertionError("stop swallowed")
            except KeyboardInterrupt:
                pass


def test_same_account_and_same_timestamp():
    with fixture() as f:
        # A stable account identity cannot escape its full Retry-After by token rotation.
        path = f.source / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "A", "accountUuid": "same"}}))
        select(f)
        before = quota_wait.observe(f.cfg, "claude", {})
        stamp = path.stat().st_mtime_ns
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "B", "accountUuid": "same"}}))
        os.utime(path, ns=(stamp, stamp))
        after = quota_wait.observe(f.cfg, "claude", {})
        assert before.signature != after.signature
        f.wait.wait()
        assert select(f)[0] is None
        assert f.wait.deadlines["claude"] == f.clock.start + 3600
        assert len(f.calls) == 2


def test_ignore_quota_and_cache_only():
    with fixture(agent="claude") as f:
        select(f, "claude")
        f.clock.events.append((100, lambda: publish(f, used=99)))
        f.wait.wait()
        model, snap = select(f, "claude")
        assert model is None
        assert loop._ignore_quota_verdict(model, snap["claude"]) == "over-pace"
        q = quota.Quota(f.cfg)
        with patch.object(quota, "_http_get_json", side_effect=AssertionError("network")):
            model, snap = q.choose("codex", inspect={"codex"}, fetch=set())
        assert model is None
        assert not snap["codex"].telemetry_unavailable


def test_keychain_and_custom_command():
    with fixture(agent="claude") as f:
        select(f, "claude")
        with (
            patch.object(quota_wait.sys, "platform", "darwin"),
            patch.object(quota.subprocess, "run", side_effect=AssertionError("Keychain")),
        ):
            assert f.wait.observations()["claude"] is not None
        with (
            patch.object(quota_wait, "observe", side_effect=AssertionError("custom observation")),
            patch.object(loop.time, "sleep") as sleep,
        ):
            loop._wait_for_quota(f.wait, 3600, "custom-quota")
        sleep.assert_called_once_with(3600)


def test_outer_loop_reaches_launch(*, ignore=False):
    from tauceti_worker import config, repair_recovery, verification_jobs

    with fixture(agent="claude" if ignore else "auto") as f, ExitStack() as stack:
        if ignore:

            def http(*_args):
                return 200, claude_payload(f.clock.now, used=100), None

            stack.enter_context(patch.object(quota, "_http_get_json", side_effect=http))
        f.clock.events.append((100, lambda: publish(f, used=99 if ignore else 1)))
        for module, name, result in (
            (config, "rotate_console_log", None),
            (verification_jobs, "reap_continuations", None),
            (verification_jobs, "resume_pending", None),
            (repair_recovery, "due", False),
            (loop, "fleet_admission_reason", None),
            (loop, "github_budget", {}),
        ):
            stack.enter_context(patch.object(module, name, return_value=result))
        stack.enter_context(patch.object(loop.time, "sleep", side_effect=f.clock.sleep))
        launched = []

        def launch(tail):
            launched.append(tail)
            raise KeyboardInterrupt

        stack.enter_context(patch.object(loop, "run_round_subprocess", side_effect=launch))
        result = loop.cmd_loop(
            SimpleNamespace(ignore_quota=ignore, bubble=False, quota_cmd=None),
            f.cfg,
            only=["review"],
            agent="claude" if ignore else "auto",
        )
        assert result == 130
        assert 100 <= f.clock.mono <= 130, f.clock.mono
        assert len(launched) == 1
        assert launched[0][launched[0].index("--agent") + 1] == "claude"
        assert ("--ignore-quota" in launched[0]) == ignore
        if not ignore:
            assert f.calls == [("codex", 0), ("claude", 0)]


def test_wall_clock_and_original_ceiling():
    with fixture() as f:
        select(f)
        original = f.wait.deadlines["claude"]
        f.clock.events.append((100, lambda: credentials(f.home, "B")))
        f.wait.wait()
        select(f)
        f.wait.wait()
        # A failure at the advanced deadline must not discard the original hourly visit.
        with patch.object(quota, "_http_get_json", return_value=(429, {}, 7200)):
            select(f)
        assert f.wait.deadlines["claude"] <= original
        f.clock.now -= 5000
        while f.clock.mono < 3600:
            f.wait.wait()
            if f.clock.mono < 3600:
                select(f)
        assert f.clock.mono == 3600


def test_no_uncacheable_response_storm():
    with fixture() as f:
        select(f)
        credentials(f.home, "B")
        f.wait.wait()
        select(f)  # the source-scoped pause still applies
        f.wait.wait()
        with patch.object(quota, "_http_get_json", return_value=(200, {}, None)) as http:
            select(f)
            before = f.clock.mono
            f.wait.wait()
            assert f.clock.mono >= before + 300
            assert http.call_count == 1


def test_sibling_due_and_admission_interleavings():
    with fixture() as f:
        select(f)
        f.clock.sleep(10)
        publish(f)
        f.wait.deadlines["codex"] = f.clock.start + 20
        f.wait.monotonic_deadlines["codex"] = 20
        f.wait.wait()
        assert f.clock.mono == 20
        assert select(f)[0] == "claude"
    with fixture() as f:
        select(f)
        f.clock.events.append((100, lambda: publish(f)))
        f.wait.wait()
        # Simulate another credential change while the outer admission path waits.
        path = f.home / ".codex/auth.json"
        path.write_text(json.dumps({"tokens": {"access_token": "new", "account_id": "D"}}))
        assert select(f)[0] == "claude"
        assert f.calls[-1] == ("codex", 100)


def test_first_failed_observation_and_macos_selection():
    with fixture() as f:
        with patch.object(quota_wait, "observe", return_value=None):
            select(f)
        f.wait.wait()
        assert f.clock.mono == 3600
        assert len(f.calls) == 2
    with fixture(agent="claude") as f:
        select(f, "claude")
        f.wait.reset()
        with (
            patch.object(quota_wait.sys, "platform", "darwin"),
            patch.object(quota.Quota, "_claude_creds", return_value=({"accessToken": "A"}, True)),
        ):
            select(f, "claude")
        f.clock.events.append((100, lambda: publish(f, "A")))
        with (
            patch.object(quota_wait.sys, "platform", "darwin"),
            patch.object(
                quota, "_claude_keychain_creds", side_effect=AssertionError("early Keychain call")
            ) as keychain,
        ):
            f.wait.wait()
            assert select(f, "claude")[0] == "claude"
        keychain.assert_not_called()
        assert len(f.calls) == 1


def test_unreadable_primary_and_observation_recovery():
    with fixture(readonly=True) as f:
        select(f)
        original_read = quota_wait._read
        primary = f.source / ".tauceti-quota/usage"

        def read(path):
            if str(path).startswith(str(primary)):
                raise PermissionError("unreadable primary")
            return original_read(path)

        with patch.object(quota_wait, "_read", side_effect=read):
            f.clock.events.append((100, lambda: publish(f)))
            f.wait.wait()
            assert select(f)[0] == "claude"
        assert len(f.calls) == 2
    with fixture() as f:
        select(f)
        baseline = f.wait.observations()
        for p in baseline:
            f.wait.seen[p] = baseline[p]
        original_read = quota_wait._read

        def temporarily_unreadable(path):
            if path.name.startswith("quota-"):
                raise PermissionError("peer replacing cache")
            return original_read(path)

        with patch.object(quota_wait, "_read", side_effect=temporarily_unreadable):
            assert f.wait.observations() == baseline
        assert f.wait.observations() == baseline


def test_manager_status_and_signal_in_sleep():
    from tauceti_worker import worker_manager as wm

    with fixture() as f:
        select(f)
        f.clock.events.append((100, lambda: publish(f)))
        with patch.object(loop, "report_runtime") as status, patch.object(quota_wait, "report_runtime") as local:
            loop._wait_for_quota(f.wait, 3600, None)
            assert status.call_args.kwargs["next_action_at"] == f.clock.start + 3600
            assert local.call_args.kwargs["quota_local_check_seconds"] == 25
        spec = wm.WorkerSpec("test")
        desired = dataclasses.replace(spec, max_open_prs=33)
        live = {"state": "waiting-quota", "applied_spec": spec.as_dict(), "next_action_at": f.wait.recheck_at()}
        assert not wm._defer_read_policy_reload(desired, live)
        live["state"] = "surveying"
        assert wm._defer_read_policy_reload(desired, live)
        with patch.object(quota_wait, "report_runtime") as local:
            f.wait.reset()
        assert local.call_args.kwargs == {"quota_local_check_seconds": None, "quota_refresh_at": None}
    with fixture() as f:
        select(f)
        f.wait.sleep = lambda _seconds: (_ for _ in ()).throw(loop._LoopTerminated())
        try:
            f.wait.wait()
            raise AssertionError("SIGTERM swallowed")
        except loop._LoopTerminated:
            pass


def test_restart_and_restrictions_in_cache_only():
    with fixture(agent="claude") as f:
        select(f, "claude")
        quota._COOLDOWNS.clear()
        f.wait = quota_wait.QuotaWait(
            f.cfg, "claude", wall=lambda: f.clock.now, monotonic=lambda: f.clock.mono, sleep=f.clock.sleep
        )
        assert select(f, "claude")[0] is None
        assert f.wait.deadlines["claude"] == f.clock.start + 3600
        assert len(f.calls) == 1
        publish(f, "A")
        q = quota.Quota(f.cfg)
        credential = q.launch_credential("claude")
        q.note_model_refusal("claude", credential)
        model, snap = q.choose("claude", inspect={"claude"}, fetch=set())
        assert model is None and snap["claude"].model_refusal
        q._raw_path("claude", credential).unlink()
        refusal = q._auth_refusal_path("claude", credential)
        refusal.write_text(json.dumps({"code": 401}))
        model, snap = q.choose("claude", inspect={"claude"}, fetch=set())
        assert model is None and "credential rejected" in snap["claude"].error
        assert not snap["claude"].telemetry_unavailable


def test_concurrent_recovery_attempt_counts(*, cacheable=True):
    with fixture() as f:
        peers = []
        for i in range(4):
            cfg = SimpleNamespace(**vars(f.cfg))
            cfg.wid = f"worker{i}"
            cfg.quota_cache = f.cfg.state.parent / cfg.wid / "cache"
            cfg.state = cfg.quota_cache.parent
            wait = quota_wait.QuotaWait(
                cfg, "auto", wall=lambda: f.clock.now, monotonic=lambda: f.clock.mono, sleep=f.clock.sleep
            )
            assert loop.choose_model(cfg, "auto", None, refresh=True, renew=True, _wait=wait)[0] is None
            peers.append((cfg, wait))
        assert [p for p, _ in f.calls].count("claude") == 1
        f.clock.sleep(25)
        f.clock.sleep(25)
        if cacheable:
            publish(f)
            for cfg, wait in peers:
                wait.wait()
                assert loop.choose_model(cfg, "auto", None, refresh=True, renew=True, _wait=wait)[0] == "claude"
            assert [p for p, _ in f.calls].count("claude") == 1
            assert [p for p, _ in f.calls].count("codex") == 4
        else:
            (f.source / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "B", "accountUuid": "new"}})
            )
            with patch.object(quota, "_http_get_json", return_value=(200, {}, None)) as http:
                for cfg, wait in peers:
                    wait.wait()
                    loop.choose_model(cfg, "auto", None, refresh=True, renew=True, _wait=wait)
                assert http.call_count == 4
                assert all(wait.deadlines["claude"] >= f.clock.start + 350 for _, wait in peers)


def test_conflicting_stores_keep_effective_authority():
    with fixture() as f:
        primary = f.source / ".tauceti-quota/usage"
        primary.mkdir(parents=True)
        original_open = quota.os.open

        def open_file(path, *args, **kwargs):
            if str(path).startswith(str(primary)) and Path(path).name.startswith("fetch-"):
                raise PermissionError("read-only primary")
            return original_open(path, *args, **kwargs)

        with patch.object(quota.os, "open", side_effect=open_file):
            select(f)
            assert f.wait.context["directory"] == quota.Quota(f.cfg)._claude_usage_fallback()
            q = quota.Quota(f.cfg)
            fp = q._fingerprint("A")
            q._auth_refusal_path("claude", fp).write_text(json.dumps({"code": 401}))
            q._claude_usage_directory = q._claude_usage_fallback()
            q._store_raw("claude", claude_payload(f.clock.now), fp, f.clock.now + 7200)
            f.wait.wait()
            assert select(f)[0] == "claude"
        assert len(f.calls) == 2


if __name__ == "__main__":
    for test in (
        test_original_deadline_and_no_side_effects,
        test_shared_recovery,
        lambda: test_shared_recovery(isolated=True),
        lambda: test_shared_recovery(readonly=True),
        test_source_cooldown_expiry,
        test_sibling_miss_and_repeated_updates,
        test_entry_race_and_persistent_mirror_failure,
        test_failed_observation_and_signals,
        test_same_account_and_same_timestamp,
        test_ignore_quota_and_cache_only,
        test_keychain_and_custom_command,
        test_outer_loop_reaches_launch,
        lambda: test_outer_loop_reaches_launch(ignore=True),
        test_wall_clock_and_original_ceiling,
        test_no_uncacheable_response_storm,
        test_sibling_due_and_admission_interleavings,
        test_first_failed_observation_and_macos_selection,
        test_unreadable_primary_and_observation_recovery,
        test_manager_status_and_signal_in_sleep,
        test_restart_and_restrictions_in_cache_only,
        test_concurrent_recovery_attempt_counts,
        lambda: test_concurrent_recovery_attempt_counts(cacheable=False),
        test_conflicting_stores_keep_effective_authority,
    ):
        test()
        print("PASS", test.__name__)

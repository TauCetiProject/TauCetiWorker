#!/usr/bin/env python3
"""Rollout changes existing read policies only and protects observation boundaries."""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("state_rollout", ROOT / "services/github-state/scripts/rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
from tauceti_worker.worker_manager import WorkerSpec, load_worker_specs, save_worker_specs, status_path

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    config, token = directory / "workers.toml", directory / "token"
    token.write_text("private-client")
    science = [WorkerSpec(id=f"science-{n}", github_account=rollout.ACCOUNT, max_open_prs=128) for n in range(9)]
    other = WorkerSpec(
        id="unrelated",
        github_account="other",
        enabled=False,
        env=(("CACHE_PATH", '/tmp/cache "quoted"\\path'), ("EXAMPLE", "line one\nline two")),
    )
    save_worker_specs(config, [*science, other])
    original = [s.as_dict() for s in load_worker_specs(config)]
    state = {"stage": 0, "observe_from": 1000, "targets": [s.id for s in science]}
    now = 1000 + 3600
    shadow = {
        "samples": 12,
        "reference_samples": 12,
        "last_sample": {
            "at": now,
            "reference_points": 1,
            "differences": [],
            "mirrors": {env: {"available": True} for env in ("production", "test")},
        },
    }
    rollout.advance(state, shadow, config, token, directory, now - 1)
    assert state["stage"] == 0 and [s.as_dict() for s in load_worker_specs(config)] == original
    # Time alone cannot promote a broken, differing, or unmeasured comparison.
    for changes in (
        {"at": now - 601},
        {"differences": [{"fields": ["headRefOid"]}]},
        {"reference_error_type": "TimeoutError"},
        {"reference_error_types": ["RATE_LIMITED"]},
    ):
        unhealthy = {**shadow, "last_sample": {**shadow["last_sample"], **changes}}
        rollout.advance(state, unhealthy, config, token, directory, now)
        assert state["stage"] == 0 and [s.as_dict() for s in load_worker_specs(config)] == original
    for key in ("samples", "reference_samples"):
        rollout.advance(state, {**shadow, key: 11}, config, token, directory, now)
        assert state["stage"] == 0
    unmeasured = {**shadow, "last_sample": {k: v for k, v in shadow["last_sample"].items() if k != "reference_points"}}
    rollout.advance(state, unmeasured, config, token, directory, now)
    assert state["stage"] == 0
    # Explicit two-hour windows remain available without resetting the original start.
    state["observation_seconds"] = 7200
    rollout.advance(state, shadow, config, token, directory, now)
    assert state["stage"] == 0
    state.pop("observation_seconds")
    rollout.advance(state, shadow, config, token, directory, now)
    assert state["stage"] == 1
    # A paid session that has not yet reloaded cannot advance the canary stage.
    rollout.advance(state, shadow, config, token, directory, now + 300)
    assert state["stage"] == 1 and "applied_at" not in state
    first = load_worker_specs(config)[0]
    status_path(first.id, directory).write_text(json.dumps({"applied_spec": first.as_dict()}))
    shadow["last_sample"]["at"] = now + 600
    rollout.advance(state, shadow, config, token, directory, now + 600)
    assert state["applied_at"] == now + 600
    state["stage_seconds"] = 7200
    shadow["last_sample"]["at"] = now + 4200
    rollout.advance(state, shadow, config, token, directory, now + 4200)
    assert state["stage"] == 1
    state.pop("stage_seconds")
    rollout.advance(state, shadow, config, token, directory, now + 4200)
    assert state["stage"] == 3
    changed = [s.as_dict() for s in load_worker_specs(config)]
    for before, after in zip(original, changed, strict=True):
        for field in ("github_state_url", "github_state_token_file"):
            after.pop(field, None)
        assert before == after
    # Operator overrides must not be overwritten or enable disabled workers.
    disabled = WorkerSpec(id="science-8", github_account=rollout.ACCOUNT, enabled=False, max_open_prs=128)
    save_worker_specs(config, [*load_worker_specs(config)[:-2], disabled, other])
    before = config.read_bytes()
    ok, _ = rollout.apply_clients(config, state["targets"], token)
    assert not ok and config.read_bytes() == before
print("PASS: configurable hourly observation, comparison gates, paid-session deferral and configuration preservation")

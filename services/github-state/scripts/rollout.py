#!/usr/bin/env python3
"""Apply the planned 1 -> 3 -> 9 read-service rollout to existing science workers.

No worker is created/enabled, no funding is changed, and active rounds are left
to the manager's deferred read-policy reload. All observations stay local.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_worker.github_transport import atomic  # noqa: E402
from tauceti_worker.worker_manager import (  # noqa: E402
    _config_lock,
    _write_worker_specs,
    default_workers_config,
    load_worker_specs,
    status_path,
    workers_state_dir,
)

URL = "https://github-state.taucetiproject.org"
ACCOUNT = "tauceti-ai-for-science"
OBSERVATION_SECONDS = 3600
STAGE_SECONDS = 3600
MIN_COMPARISON_SAMPLES = 12  # one hour of the five-minute comparison sampler


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def apply_clients(config, targets, token_file):
    with _config_lock(config):
        specs = load_worker_specs(config)
        by_id = {s.id: s for s in specs}
        for wid in targets:
            spec = by_id.get(wid)
            if not spec or spec.github_account != ACCOUNT or not spec.enabled:
                return False, "An original rollout worker is missing or disabled; preserving operator configuration"
            if spec.github_state_url not in (None, URL) or spec.github_state_token_file not in (None, str(token_file)):
                return False, "A rollout worker has another read-service configuration; preserving it"
        proposed = [
            dataclasses.replace(s, github_state_url=URL, github_state_token_file=str(token_file))
            if s.id in targets
            else s
            for s in specs
        ]
        if proposed != specs:
            _write_worker_specs(config, proposed)
    return True, "Read policy requested; paid rounds finish before the manager applies it"


def advance(state, shadow, config, token_file, runtime_dir, now):
    # Sampling maturity is a deployment check, not a per-query freshness rule.
    observation = state.get("observation_seconds", OBSERVATION_SECONDS)
    stage_seconds = state.get("stage_seconds", STAGE_SECONDS)
    if now < state["observe_from"] + observation:
        return f"Observing the deployed mirrors for {observation / 3600:g} hour(s)"
    last = shadow.get("last_sample", {})
    if now - last.get("at", 0) > 600 or not all(
        last.get("mirrors", {}).get(env, {}).get("available") for env in ("production", "test")
    ):
        return "The latest comparison did not reach both mirrors; keeping the current rollout stage"
    if last.get("reference_error_type") or last.get("reference_error_types") or "reference_points" not in last:
        return "The latest independent reference read failed; keeping the current rollout stage"
    if last.get("differences"):
        return "The latest comparison has differences; keeping the current rollout stage"
    if shadow.get("samples", 0) < MIN_COMPARISON_SAMPLES or shadow.get("reference_samples", 0) < MIN_COMPARISON_SAMPLES:
        return "Waiting for enough independent comparison samples"
    stage = state.get("stage", 0)
    if stage:
        applied = all(
            read_json(status_path(wid, runtime_dir)).get("applied_spec", {}).get("github_state_url") == URL
            for wid in state["targets"][:stage]
        )
        if not applied:
            return "Waiting for existing paid rounds to finish and apply the read policy"
        if stage == 9:
            return "All nine original science workers have applied the service read policy"
        state.setdefault("applied_at", now)
        if now - state["applied_at"] < stage_seconds:
            return f"Observing this client stage for {stage_seconds / 3600:g} hour(s)"
    next_stage = {0: 1, 1: 3, 3: 9}[stage]
    ok, message = apply_clients(config, state["targets"][:next_stage], token_file)
    if ok:
        state.update(stage=next_stage, stage_at=now)
        state.pop("applied_at", None)
    return message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="advance the authorized rollout; otherwise show status")
    parser.add_argument("--config", type=Path, default=default_workers_config())
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/tauceti/github-state-shadow")
    parser.add_argument("--observation-hours", type=float, help="initial mirror observation, default one hour")
    parser.add_argument(
        "--stage-hours", type=float, help="observation after each client stage applies, default one hour"
    )
    args = parser.parse_args()
    for value in (args.observation_hours, args.stage_hours):
        if value is not None and not 0 < value < float("inf"):
            parser.error("observation and stage hours must be finite and positive")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.state_dir / "rollout.json"
    with _config_lock(path):
        state = read_json(path)
        if not state:
            specs = load_worker_specs(args.config)
            targets = [s.id for s in specs if s.enabled and s.github_account == ACCOUNT][:9]
            if len(targets) != 9:
                raise SystemExit("Expected nine already enabled science workers; no worker was added")
            state = {"targets": targets, "stage": 0, "observe_from": time.time()}
            atomic(path, state)
        for key, hours, default in (
            ("observation_seconds", args.observation_hours, OBSERVATION_SECONDS),
            ("stage_seconds", args.stage_hours, STAGE_SECONDS),
        ):
            if hours is not None:
                state[key] = hours * 3600
            else:
                state.setdefault(key, default)
        if args.apply and not (args.state_dir / "rollout-paused").exists():
            state["status"] = advance(
                state,
                read_json(args.state_dir / "summary.json"),
                args.config,
                Path.home() / ".config/tauceti-github-state/client-token",
                workers_state_dir(),
                time.time(),
            )
            state["checked_at"] = time.time()
            atomic(path, state)
        print(json.dumps(state))


if __name__ == "__main__":
    main()

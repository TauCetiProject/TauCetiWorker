#!/usr/bin/env python3
"""Synthetic transcript coverage for the read-only cost model."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tauceti_worker.cost_model import (
    _parse_codex,
    analyze,
    distribution,
    infrastructure_model,
    loc_cost_model,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    logs = root / "logs"
    state = root / "state"
    agent = logs / "worker1" / "agent-codex-20260101-000000.log"
    agent.parent.mkdir(parents=True)
    sid = "01234567-89ab-cdef-0123-456789abcdef"
    agent.write_text(f"[session] codex {sid}\n")
    roadmap = state / "worker1/refs/roadmap/TauCetiRoadmap/Area/README.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text("roadmap\n" * 20)
    transcript = state / "worker1/home/.codex/sessions/2026/01/01" / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "You are authoring a new pull request. Work ONLY within the `Area` roadmap.",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "claim",
                    "arguments": json.dumps({"cmd": 'claim.sh acquire "author/Area/item"'}),
                },
            },
            {
                "timestamp": "2026-01-01T00:01:02Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "claim", "output": "Wall time: 2 seconds"},
            },
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "edit", "input": "patch"},
            },
            {
                "timestamp": "2026-01-01T00:02:01Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "edit", "output": "Done"},
            },
            {
                "timestamp": "2026-01-01T00:03:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "build",
                    "arguments": json.dumps({"cmd": "lake build"}),
                },
            },
            {
                "timestamp": "2026-01-01T00:03:12Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "build", "output": "Wall time: 12 seconds"},
            },
        ],
    )

    report = analyze(logs, state, limit=10)
    assert report["coverage"]["sessions_parsed"] == 1
    group = report["groups"]["codex/preparation"]
    assert group["tools"]["lake-build"]["calls"]["median"] == 12
    assert report["orientation"]["to_claim_seconds"]["median"] == 60
    assert report["orientation"]["to_first_edit_seconds"]["median"] == 120
    assert report["orientation"]["roadmap_bytes"]["median"] == roadmap.stat().st_size

    modern = root / "modern.jsonl"
    write_jsonl(
        modern,
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "search",
                    "input": 'const r = await tools.exec_command({\n  cmd: "gh run view 1 --log | rg \\"lake build\\"",\n  yield_time_ms: 30000\n});',
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01.300Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "search",
                    "output": "Script completed\nWall time 1.3 seconds",
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01.300Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["bash", "-lc", 'gh run view 1 --log | rg "lake build"'],
                        "started_at_ms": 1767225600000,
                        "completed_at_ms": 1767225601300,
                    },
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "build",
                    "input": 'const r = await tools.exec_command({\n  cmd: "set -e\\nlake build\\nlake exe axioms",\n  yield_time_ms: 30000\n});',
                },
            },
            {
                "timestamp": "2026-01-01T00:00:32Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "build",
                    "output": "Script running with session ID 16678\nWall time 30.0 seconds",
                },
            },
            {
                "timestamp": "2026-01-01T00:00:33Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "poll",
                    "input": 'const r = await tools.write_stdin({session_id:16678,chars:"",yield_time_ms:30000});',
                },
            },
            {
                "timestamp": "2026-01-01T00:01:03Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "poll",
                    "output": "Script completed\nWall time 30.0 seconds",
                },
            },
            {
                "timestamp": "2026-01-01T00:01:04Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "patch",
                    "input": 'const r = await tools.apply_patch("*** Begin Patch\\n*** End Patch");',
                },
            },
            {
                "timestamp": "2026-01-01T00:01:05Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "patch", "output": "Done"},
            },
            {
                "timestamp": "2026-01-01T00:03:11Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["bash", "-lc", "set -e\nlake build\nlake exe axioms"],
                        "started_at_ms": 1767225602000,
                        "completed_at_ms": 1767225791000,
                    },
                },
            },
        ],
    )
    modern_tools, _, _, _ = _parse_codex(modern)
    assert len(modern_tools) == 3
    assert modern_tools[0].category is None
    assert modern_tools[0].seconds == 1.3
    assert modern_tools[1].category == "mixed-lean"
    assert modern_tools[1].seconds == 189
    assert modern_tools[2].mutates

assert distribution([1, 100])["median"] == 50.5
assert distribution([1, 2, 10, 20])["median"] == 6

infra = infrastructure_model(
    pr_builds=4,
    pr_runner_minutes=10,
    pr_runner_vcpus=8,
    main_runner_minutes=10,
    main_runner_vcpus=2,
    pr_runner_usd_minute=0.02,
    main_runner_usd_minute=0.01,
    cache_objects=1000,
    cache_gib=1,
    retained_cache_gib=5,
    merged_prs_month=1000,
)
assert abs(infra["ci"]["vcpu_hours_per_merged_pr"] - 340 / 60) < 1e-9
assert abs(infra["ci"]["equivalent_usd_per_merged_pr"] - 0.9) < 1e-9
assert abs(infra["r2_complete_cache_fetch"]["gross_class_b_usd"] - 1001 / 1_000_000 * 0.36) < 1e-12
assert infra["r2_storage"]["usd_month_after_10_gb_free_tier"] == 0
assert infra["r2_complete_cache_fetch"]["monthly_gib"] == 5000
assert (
    abs(
        sum(component["equivalent_usd_per_merged_pr"] for component in infra["ci"]["decomposition"].values())
        - infra["ci"]["equivalent_usd_per_merged_pr"]
    )
    < 1e-12
)
renormalized = infrastructure_model(
    pr_builds=1,
    pr_runner_minutes=1,
    pr_runner_vcpus=1,
    main_runner_minutes=1,
    main_runner_vcpus=1,
    pr_runner_usd_minute=1,
    main_runner_usd_minute=1,
    cache_objects=0,
    cache_gib=0,
    retained_cache_gib=9.5,
    pr_component_shares=(2, 3, 5),
    main_component_shares=(1, 1, 2),
)
assert (
    abs(
        sum(component["equivalent_usd_per_merged_pr"] for component in renormalized["ci"]["decomposition"].values()) - 2
    )
    < 1e-12
)
assert renormalized["r2_storage"]["usd_month_after_10_gb_free_tier"] > 0
try:
    infrastructure_model(
        pr_builds=1,
        pr_runner_minutes=1,
        pr_runner_vcpus=1,
        main_runner_minutes=1,
        main_runner_vcpus=1,
        pr_runner_usd_minute=1,
        main_runner_usd_minute=1,
        cache_objects=0,
        cache_gib=0,
        retained_cache_gib=0,
        pr_component_shares=(0, 0, 0),
    )
    raise AssertionError("all-zero component shares should fail")
except ValueError:
    pass

loc = loc_cost_model(
    infra,
    ai_usd_per_changed_loc=0.077,
    changed_loc_per_pr=100,
    changed_per_net_loc=1.1,
    reference_repo_loc=1_000_000,
    projection_locs=[1_000_000, 10_000_000],
)
scaling = loc["best_scaling_scenario"]
at_reference = loc["current_usd_per_changed_loc"]
assert (
    abs(
        scaling["marginal_intercept_usd"]
        + scaling["marginal_slope_usd_per_existing_loc"] * 1_000_000
        - at_reference["total"]
    )
    < 1e-12
)
projection = loc["projections"][0]
expected_total = scaling["integrated_linear_usd"] * 1_000_000 + scaling["integrated_quadratic_usd"] * 1_000_000**2
assert abs(projection["changed_loc"]["no_churn_cumulative_usd"] - expected_total) < 1e-9
assert (
    abs(projection["net_retained_loc"]["cumulative_usd"] - projection["changed_loc"]["no_churn_cumulative_usd"] * 1.1)
    < 1e-9
)
assert abs(scaling["retained_integrated_linear_usd"] - scaling["integrated_linear_usd"] * 1.1) < 1e-12

print("cost model: PASS")

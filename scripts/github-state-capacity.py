#!/usr/bin/env python3
"""Evidence-based current-fleet extrapolation and quota-constrained mirror scenarios."""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.github_transport import root

CATALOG = json.loads((Path(__file__).resolve().parent.parent / "tauceti_worker/github_catalog.json").read_text())


def spending(path, worker_ids, start, end):
    workers, settled = {}, {}
    for line in path.open():
        row = json.loads(line)
        stamp = row["timestamp"]
        t = (
            float(stamp)
            if isinstance(stamp, (float, int))
            else datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        )
        if t > end:
            continue
        for event in row.get("events", []):
            kind = event["type"]
            if kind == "admit":
                workers[event["id"]] = event.get("worker")
            elif kind == "settle":
                settled[event["id"]] = {"at": t, "worker": workers.get(event["id"]), "cost": float(event["cost"])}
            elif kind == "correction" and event["id"] in settled:
                settled[event["id"]]["cost"] = float(event["cost"])
    return sum(r["cost"] for r in settled.values() if start < r["at"] <= end and r["worker"] in worker_ids)


def evidence(directory, worker_ids, start, end):
    counts = collections.Counter()
    points = collections.Counter()
    measured = collections.Counter()
    unknown = collections.Counter()
    endpoints = collections.Counter()
    workers = collections.defaultdict(collections.Counter)
    earliest = None
    for filename in ["requests.previous.jsonl", "requests.jsonl"]:
        path = directory / filename
        if not path.exists():
            continue
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = r.get("time")
                wid = r.get("worker")
                if not isinstance(t, (int, float)):
                    continue
                earliest = t if earliest is None else min(earliest, t)
                if not start < t <= end or wid not in worker_ids:
                    continue
                if r.get("kind") == "http" and r.get("host") == "api.github.com":
                    resource = r.get("resource", "unknown")
                    if r.get("endpoint") == "/rate_limit":
                        counts["quota_probes"] += 1
                        continue
                    name = (
                        "graphql_attempts"
                        if r.get("endpoint") == "/graphql"
                        else "rest_core"
                        if resource == "core" and r.get("status") != 304
                        else resource
                    )
                    counts[name] += 1
                    workers[wid][name] += 1
                    endpoints[(resource, r.get("method", "?"), r.get("endpoint", "?"))] += 1
                elif r.get("kind") == "graphql_charge":
                    if isinstance(r.get("points"), int):
                        points[wid] += r["points"]
                        measured[wid] += 1
                    else:
                        unknown[wid] += 1
    return {
        "counts": dict(counts),
        "graphql_known_points": sum(points.values()),
        "graphql_measured_calls": sum(measured.values()),
        "graphql_unmeasured_calls": sum(unknown.values()),
        "per_worker": {w: dict(c) for w, c in workers.items()},
        "endpoints": [
            {"resource": r, "method": m, "endpoint": e, "requests": n} for (r, m, e), n in endpoints.most_common()
        ],
        "retained_history_covers_window": earliest is not None and earliest <= start,
    }


def scenario(
    n,
    hot,
    turnover,
    hot_reaction_comments=5,
    quiet_reaction_comments=20,
    *,
    mandatory_ci=False,
    important_active_runs=1,
    important_completions=1,
    requested_workflow_runs=0,
):
    # Fixed read installation; clients do not multiply reconciliation traffic.
    quiet = max(0, n - hot)
    limits = CATALOG["batch_limits"]
    history_batch, thread_batch = limits["issue_comments"], limits["review_threads"]
    status_batch, reaction_batch = limits["statuses"], limits["reactions"]
    gql = {
        "full_pr_inventory_hourly": math.ceil(n / 100),
        "pr_and_issue_changes_per_minute": 120 * max(1, math.ceil(turnover / 60 / 100)),
        f"issue_histories_batched_{history_batch}_quiet_hourly": math.ceil(quiet / history_batch),
        "issue_histories_hot_every_5m": 12 * math.ceil(hot / history_batch),
        f"review_threads_batched_{thread_batch}_quiet_hourly": math.ceil(quiet / thread_batch),
        "review_threads_hot_every_5m": 12 * math.ceil(hot / thread_batch),
        f"status_batches_{status_batch}_quiet_hourly": math.ceil(quiet / status_batch),
        "status_hot_every_5m": 12 * math.ceil(hot / status_batch),
        "new_head_status_baselines": math.ceil(turnover / status_batch),
    }
    rest = {
        "checks_audit_every_6h": n / 6,
        "reviews_audit_every_6h": n / 6,
        "changed_files_and_details": 2 * turnover,
        "new_head_check_baselines": turnover,
        "workflow_job_reconciliation": 2 * turnover
        if mandatory_ci
        else 60 * important_active_runs + important_completions + requested_workflow_runs,
        "review_event_repairs": 3 * turnover,
        "workflow_tail_pages": max(1, math.ceil(2 * turnover / 100)),
        "repository_and_refs_audits": 2,
    }
    # Live measurements establish a one-point floor even for a hundred nodes.
    gql["reaction_batches_hot_every_5m"] = 12 * math.ceil(hot * hot_reaction_comments / reaction_batch)
    gql["reaction_batches_quiet_daily_amortized"] = math.ceil(quiet * quiet_reaction_comments / reaction_batch) / 24
    return {
        "open_prs": n,
        "hot_prs": hot,
        "changed_prs_per_hour": turnover,
        "hot_reaction_comments_per_pr": hot_reaction_comments,
        "quiet_reaction_comments_per_pr": quiet_reaction_comments,
        "ci_policy": "mandatory_baselines" if mandatory_ci else "observed_plus_demand_and_important_runs",
        "ci_assumptions": {
            "important_active_runs": important_active_runs,
            "important_completions_per_hour": important_completions,
            "requested_workflow_runs_per_hour": requested_workflow_runs,
        },
        "graphql_points_per_hour": sum(gql.values()),
        "rest_requests_per_hour": sum(rest.values()),
        "graphql_components": gql,
        "rest_components": rest,
        "graphql_fraction_of_5000": sum(gql.values()) / 5000,
        "rest_fraction_of_5000": sum(rest.values()) / 5000,
        "assumptions": [
            "2026-10-12 measured costs: issue/status/reaction batches of 100=1; ten PRs with ten initial threads=1.",
            "History and reaction density are explicit assumptions; dense/overflow histories cost more.",
            "Existing PR/CI webhook payloads update indexed state without triggering a full repository survey.",
            "New CI policy enumerates only requested or important runs; webhook-observed jobs do not require a full list.",
            "Important active runs audit each minute and once after completion; rates above are explicit model inputs, not measured facts.",
            "Completed workflow jobs stop polling; workflow inventory reconciliation reads an overlapping incremental tail.",
            "Closed PR histories and head audits stop polling when quiet; server demand reactivates them without client age gates.",
            "REST/GQL routing can rebalance; forecasts exclude bootstrap and recovery bursts.",
            "Covered client queries use the mirror; missing requested coverage promotes one deduplicated upstream repair.",
            "Write throughput and secondary limits are separate; this read model does not prove they cannot bottleneck.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", type=Path, default=Path.home() / ".local/state/tauceti/science-ramp/summary.json")
    p.add_argument("--ledger-dir", type=Path, default=root())
    p.add_argument("--hours", type=float, default=1)
    p.add_argument("--run-rate", type=float, default=1500)
    p.add_argument("--budget-ledger", type=Path, default=Path.home() / ".local/state/tauceti/budget/events.jsonl")
    p.add_argument(
        "--measurements",
        type=Path,
        default=Path.home() / ".config/tauceti-github-state-test/large-batch-measurements.json",
    )
    p.add_argument("--important-active-runs", type=int, default=1)
    p.add_argument("--important-completions", type=float, default=1)
    p.add_argument("--requested-workflow-runs", type=float, default=0)
    p.add_argument(
        "--shadow-summary", type=Path, default=Path.home() / ".local/state/tauceti/github-state-shadow/summary.json"
    )
    a = p.parse_args()
    if (
        a.hours <= 0
        or a.run_rate <= 0
        or min(a.important_active_runs, a.important_completions, a.requested_workflow_runs) < 0
    ):
        p.error("window and target must be positive; workflow rates must be nonnegative")
    summary = json.loads(a.summary.read_text())
    hour = summary["hourly_usage"]
    end = hour["sampled_at"]
    ids = set(hour["rest_core_requests_by_worker"])
    start = end - a.hours * 3600
    observed = evidence(a.ledger_dir, ids, start, end)
    current_dollars = spending(a.budget_ledger, ids, start, end) / a.hours
    factor = a.run_rate / current_dollars if current_dollars > 0 else None
    shadow = json.loads(a.shadow_summary.read_text()) if a.shadow_summary.exists() else {}
    server = shadow.get("last_sample", {}).get("mirrors", {}).get("production", {}).get("health", {})
    ci = {
        "important_active_runs": a.important_active_runs,
        "important_completions": a.important_completions,
        "requested_workflow_runs": a.requested_workflow_runs,
    }
    result = {
        "version": 1,
        "sample_time": hour["sample_utc"],
        "window_hours": a.hours,
        "target_dollars_per_hour": a.run_rate,
        "observed_fleet_dollars_per_hour": current_dollars,
        "estimated_workers_at_target": a.run_rate / (current_dollars / len(ids))
        if current_dollars > 0 and ids
        else None,
        "extrapolation_caveat": "Quota waits depress observed hourly expenditure; the worker-count estimate assumes the same utilization and is not unconstrained capacity.",
        "baseline": observed,
        "scaled_current": {
            "rest_core_requests_per_hour": observed["counts"].get("rest_core", 0) / a.hours * factor
            if factor
            else None,
            "graphql_http_attempts_per_hour": observed["counts"].get("graphql_attempts", 0) / a.hours * factor
            if factor
            else None,
            "graphql_known_points_per_hour": observed["graphql_known_points"] / a.hours * factor if factor else None,
            "unknown_graphql_costs_prevent_full_quota_estimate": observed["graphql_unmeasured_calls"] > 0
            or observed["graphql_measured_calls"] < observed["counts"].get("graphql_attempts", 0),
        },
        "mirror_before_reconciliation_scenarios": [
            scenario(n, min(400, n // 5), 100, mandatory_ci=True) for n in [1000, 2000, 5000]
        ],
        "mirror_design_scenarios": [scenario(n, min(400, n // 5), 100, **ci) for n in [1000, 2000, 5000]],
        "mirror_stress_scenarios": [scenario(5000, 800, 200, density, **ci) for density in (5, 10, 20)],
        "shared_app_evidence": {
            "sample_time": shadow.get("last_sample", {}).get("at"),
            "hour": server.get("quota", {}).get("hour", []),
            "recipes": server.get("quota", {}).get("recipes", []),
            "lanes": server.get("state", {}).get("lanes", []),
            "repair_hour": server.get("state", {}).get("repair_hour", []),
        },
        "coverage": hour["coverage"],
        "live_query_measurements": json.loads(a.measurements.read_text()).get("groups", {})
        if a.measurements.exists()
        else {},
        "status": "query_costs_measured; live_reconciliation_observation_and_workload_density_validation_remain",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

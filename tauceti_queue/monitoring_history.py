"""Prepare bounded public measurements using the existing pilot-report definitions.

Run hourly/daily as a separate sidecar. It never publishes the private snapshot or
outcome paths. Live queue publication does not wait for this historical scan.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import urllib.request
from pathlib import Path

from .monitoring import NoRedirect
from .monitoring_protocol import encode

METRICS = (
    "landed_heads",
    "landed_prs",
    "landed_prs_per_hour",
    "landed_membership_occurrences",
    "nonempty_unique_prs_per_hour",
    "nonempty_unique_prs",
    "empty_member_occurrences",
    "already_landed_empty_occurrences",
    "emptiness_unknown",
    "baseline_ratio",
    "ci_attempts",
    "singleton_runs",
    "failed_rounds_per_large_landing",
    "claim_collisions",
    "claim_expiries",
    "opt_outs",
    "anticipated_withdrawals",
    "needs_rebase",
    "readmissions",
    "edit_summaries",
    "shrink_to_empty",
    "inference_hold_minutes",
)


def load_reporter():
    package_root = Path(__file__).resolve().parent.parent
    script = package_root / "scripts/queue_pilot_report.py"
    if not script.is_file():
        script = package_root / "tauceti_worker/scripts/queue_pilot_report.py"
    spec = importlib.util.spec_from_file_location("queue_pilot_report", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_baseline(path, reporter):
    """Accept the frozen numeric report or the original retained Bors feed."""
    value = json.loads(path.read_text())
    if "landed_prs_per_hour" not in value:
        import sys

        script = Path(reporter.__file__).with_name("staging_journal_report.py")
        spec = importlib.util.spec_from_file_location("staging_journal_report", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["staging_journal_report"] = module
        value = reporter.baseline_report([path], 1833, 1932)
    rate = value.get("landed_prs_per_hour")
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise ValueError("Bors baseline needs a positive recorded landing rate")
    return value


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def prepare(snapshot, baseline, reporter, public_repositories=("TauCetiProject/TauCeti",)):
    import copy

    snapshot = copy.deepcopy(snapshot)
    tables = snapshot["tables"]
    if any("repository" not in b for b in tables["batches"]):
        raise ValueError("history snapshot needs explicit repository attribution")
    public = set(public_repositories)
    tables["batches"] = [b for b in tables["batches"] if b["repository"] in public]
    batches = {b["id"] for b in tables["batches"]}
    for name in ("members", "ci_attempts", "tasks", "claims"):
        tables[name] = [r for r in tables[name] if r["batch_id"] in batches]
    tables["journal"] = [
        r
        for r in tables["journal"]
        if r["batch_id"] in batches or (r["batch_id"] is None and r["kind"] in ("inference-hold", "inference-resumed"))
    ]
    tables["inputs"] = [r for r in tables["inputs"] if r.get("repository") in public]
    tasks = {str(t["id"]) for t in tables["tasks"]}
    snapshot["worker_outcomes"] = [o for o in snapshot.get("worker_outcomes", []) if str(o.get("task_id")) in tasks]
    captured = snapshot["captured_at"]
    first = min((a["dispatched_at"] for a in snapshot["tables"]["ci_attempts"]), default=captured)
    # These are local SQLite IDs; public history never exports raw journal detail.
    windows = []
    for key, label, seconds in [
        ("24h", "Last 24 hours", 86400),
        ("7d", "Last seven days", 7 * 86400),
        ("pilot", "Whole pilot", None),
    ]:
        start = max(first, captured - seconds) if seconds else first
        if start >= captured:
            continue
        report = reporter.analyse(snapshot, baseline, since=start)
        pilot = report["pilot"]
        summary = {k: number(pilot.get(k)) for k in METRICS}
        for k in (
            "head_dispatch_minutes",
            "head_seal_minutes",
            "repair_task_minutes",
            "approval_to_insertion_minutes",
            "currently_waiting_age_minutes",
        ):
            summary[k] = {n: number(pilot.get(k, {}).get(n)) for n in ("count", "min", "median", "p95", "max")}
        known = [number(a.get("estimated_cost_usd")) for a in report["agent_attempts"]]
        summary["cost"] = {
            "recorded_usd": sum(v for v in known if v is not None),
            "recorded_attempts": sum(v is not None for v in known),
            "unknown_attempts": sum(v is None for v in known),
            "coverage": "recorded_subtotal_only",
        }
        heads = [
            {
                k: number(h.get(k))
                for k in (
                    "size",
                    "ci_rounds",
                    "source_failure_rounds",
                    "first_dispatch_to_landing_minutes",
                    "seal_to_landing_minutes",
                    "ci_observed_minutes",
                    "claimed_task_minutes",
                    "ci_task_overlap_minutes",
                    "other_or_unobserved_minutes",
                )
            }
            | {"batch_id": str(h["batch"])}
            for h in report["heads"][-100:]
        ]
        checks = report["exit_criteria"]
        scenario = checks["110_pr_two_source_failures_under_120_minutes"]
        week = checks["week_above_baseline_without_comment_or_attribution_regression"]
        windows.append(
            {
                "key": key,
                "label": label,
                "from": report["window"]["from"],
                "until": report["window"]["until"],
                "hours": report["window"]["hours"],
                "summary": summary,
                "heads": heads,
                "heads_truncated": len(report["heads"]) > 100,
                "criteria": {
                    "scenario": {
                        "status": scenario["status"],
                        "eligible_count": len(scenario["eligible_batches"]),
                        "qualifying_count": len(scenario["qualifying_batches"]),
                        "qualifying_batches": [str(b) for b in scenario["qualifying_batches"][:100]],
                    },
                    "week": {
                        "status": week["status"],
                        "observed_days": number(week["observed_days"]),
                        "rate_above_baseline": week["observed_rate_above_baseline"],
                    },
                },
            }
        )
    return {
        "schema": "tauceti.operations.history/v1",
        "observed_at": reporter.iso(captured),
        "scope": {"repositories": sorted(public)},
        "coverage": "observed",
        "windows": windows,
        "baseline": {
            k: number(baseline.get(k))
            for k in ("hours", "landed_prs", "landed_prs_per_hour", "singleton_runs", "failed_rounds_per_large_landing")
        },
        "limits": [
            "Costs are recorded subtotals; absent interrupted sessions remain unknown.",
            "The Bors baseline has no retained nonempty-commit comparison or inference costs.",
            "CI and claimed repair intervals use unions; overlap is not counted twice in latency.",
            "Attribution/comment correctness and warm-artifact conditions require a separate audit.",
            "Historical host telemetry starts at activation; earlier periods remain unknown.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--clone", type=Path)
    parser.add_argument("--public-repository", action="append")
    parser.add_argument("--worker-root", type=Path, action="append", default=[])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-url")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    if args.publish_url and (not args.token_file or not args.publish_url.startswith("https://")):
        parser.error("publication needs an HTTPS endpoint and token file")
    reporter = load_reporter()
    value = prepare(
        reporter.export(args.database, args.worker_root, args.clone),
        read_baseline(args.baseline, reporter),
        reporter,
        args.public_repository or ("TauCetiProject/TauCeti",),
    )
    data = encode(value)
    if len(data) > 256000:
        raise ValueError("history summary exceeds publication budget")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".tmp").write_bytes(data)
    os.replace(args.output.with_suffix(".tmp"), args.output)
    if args.publish_url:
        request = urllib.request.Request(
            args.publish_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + args.token_file.read_text().strip(),
                "User-Agent": "TauCetiQueue/history",
            },
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            response.read(65536)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

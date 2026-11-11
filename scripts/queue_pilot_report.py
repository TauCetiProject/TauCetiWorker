#!/usr/bin/env python3
"""Read-only daily queue pilot measurements, with a Bors candidate-feed baseline.

Export on the service host, then report locally. Exports omit credentials, claim tokens,
PR bodies, prompts, tool output and source text. SQLite reads share one read transaction.
Worker outcomes are independent files; their capture time and completeness are explicit.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "tauceti.queue-pilot-report/v1"
SNAPSHOT_SCHEMA = "tauceti.queue-pilot-snapshot/v1"
# Explicit allowlists keep this operational export safe to archive.
COLUMNS = {
    "batches": "id repository version base tip state created_at sealed_at landed_at landed_how first_dispatch_at",
    "ci_attempts": "id batch_id batch_version head_sha dispatched_at run_id run_status run_conclusion run_started_at run_completed_at outcome outcome_detail phases cancelled_at diagnostics",
    "tasks": "id kind batch_id state created_at resolved_at resolution ci_attempt_id claims submissions",
    "claims": "id batch_id operation task_id created_at released_at release_reason",
    "members": "id batch_id input_id number accepted_head position commit_sha provenance task_id added_at removed withdrawn no_repair",
    "inputs": "id repository number head_sha approved_at state reason batch_id landed_at passes_waited probes no_repair",
}
JOURNAL_KINDS = {
    "landed",
    "probe-missing-source",
    "probe-import-scan-unavailable",
    "sealed",
    "claimed",
    "released",
    "adopted",
    "pre-rebase-adopted",
    "ci-source-failure",
    "ci-pass",
    "ci-infrastructure",
    "ci-cancelled",
    "dispatched",
    "claim-refused",
    "claim-expired",
    "inference-hold",
    "inference-resumed",
    "no-repair",
    "problem-anticipated",
    "needs-rebase",
    "readmitted",
    "chain-invalid",
    "edit-summary",
    "predicted-collisions",
    "predicted-collisions-resolved",
    "admission-predicted",
    "admission-repair-started",
    "admission-repair-result",
}
DETAIL_KEYS = {
    "members",
    "withdrawn",
    "tip",
    "task_id",
    "attempt_id",
    "run_id",
    "version",
    "claim_id",
    "reason",
    "number",
    "findings",
    "contexts",
    "edited",
    "emptied",
    "shrink",
    "collisions",
    "predicted",
    "fixed",
    "stage",
    "interacting",
    "since",
    "count",
    "truncated",
    "error",
    "verified_fixed",
    "verification",
    "task_kind",
    "input_tip",
}


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if seconds is not None else None


def dist(values):
    values = sorted(x for x in values if x is not None)
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    import statistics

    return {
        "count": len(values),
        "min": round(values[0], 3),
        "median": round(statistics.median(values), 3),
        "p95": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
        "max": round(values[-1], 3),
    }


def compact_agent(value):
    """Keep usage and cost facts without session identifiers or model text."""
    if isinstance(value, list):
        return [compact_agent(x) for x in value]
    if not isinstance(value, dict):
        return value
    allowed = {
        "provider",
        "model",
        "seconds",
        "estimated_cost_usd",
        "telemetry",
        "usage",
        "agent_usage",
        "attempts",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "cost_usd",
        "total_cost_usd",
        "status",
        "groups",
        "agent",
        "result",
        "turns",
        "tool_calls",
    }
    return {k: compact_agent(v) for k, v in value.items() if k in allowed}


def commit_facts(clone, members):
    """Read commit objects in two batches; no Git checkout or repository writes."""
    shas = {m["commit_sha"] for m in members if m.get("commit_sha")}

    def read(objects):
        if not objects:
            return {}
        result = subprocess.run(
            ["git", "-C", str(clone), "cat-file", "--batch"],
            input="".join(x + "\n" for x in sorted(objects)).encode(),
            stdout=subprocess.PIPE,
            check=True,
        )
        data, offset, found = result.stdout, 0, {}
        while offset < len(data):
            end = data.index(b"\n", offset)
            header = data[offset:end].decode().split()
            offset = end + 1
            if header[-1] == "missing":
                continue
            size = int(header[2])
            body = data[offset : offset + size].decode(errors="replace")
            offset += size + 1
            lines = body.split("\n\n", 1)
            headers = lines[0].splitlines()
            found[header[0]] = {
                "tree": next((x[5:] for x in headers if x.startswith("tree ")), None),
                "parent": next((x[7:] for x in headers if x.startswith("parent ")), None),
                "already_landed_note": "Landed on main as " in body,
            }
        return found

    commits = read(shas)
    parents = read({v["parent"] for v in commits.values() if v["parent"]})
    return {
        sha: {
            "empty": v["tree"] == parents[v["parent"]]["tree"] if v["parent"] in parents else None,
            "already_landed_note": v["already_landed_note"],
        }
        for sha, v in commits.items()
    }


def export(database, worker_roots, clone=None):
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {}
        for table, fields in COLUMNS.items():
            actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            cols = [x for x in fields.split() if x in actual]
            tables[table] = [dict(r) for r in conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY id")]
        marks = ",".join("?" for _ in JOURNAL_KINDS)
        tables["journal"] = []
        for r in conn.execute(
            f"SELECT id,at,kind,batch_id,detail FROM journal WHERE kind IN ({marks}) ORDER BY id", sorted(JOURNAL_KINDS)
        ):
            row = dict(r)
            row["detail"] = {k: v for k, v in json.loads(row["detail"]).items() if k in DETAIL_KEYS}
            tables["journal"].append(row)
        settings = {
            r["key"]: json.loads(r["value"])
            for r in conn.execute("SELECT key,value FROM settings WHERE key='state:counters'")
        }
        # Runtime counters are historical evidence, not authorization material.
        counters = settings.get("state:counters", {})
        observed = time.time()
    outcomes = []
    outcome_errors = []
    for root in worker_roots:
        for p in sorted(root.glob("tasks/*/outcome.json")):
            try:
                raw = json.loads(p.read_text())
            except (OSError, ValueError) as error:
                outcome_errors.append({"path": str(p), "reason": type(error).__name__})
                continue
            outcomes.append(
                {
                    "path": str(p),
                    "task_id": raw.get("task_id"),
                    "worker": raw.get("worker"),
                    "status": raw.get("status"),
                    "seconds": raw.get("seconds"),
                    "agent": compact_agent(raw.get("agent")),
                    "parallel": compact_agent(raw.get("parallel")),
                    "agent_usage": compact_agent(raw.get("agent_usage")),
                    "modified_at": p.stat().st_mtime,
                }
            )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": observed,
        "database": str(database),
        "worker_roots": [str(p) for p in worker_roots],
        "tables": tables,
        "counters": counters,
        "worker_outcomes": outcomes,
        "outcome_errors": outcome_errors,
        "commit_facts": commit_facts(clone, tables["members"]) if clone else {},
        "clone": str(clone) if clone else None,
    }


def union_seconds(intervals, start, end):
    spans = sorted(
        (max(start, a), min(end, b)) for a, b in intervals if a is not None and b is not None and b > start and a < end
    )
    total = 0
    right = start
    for a, b in spans:
        if b > right:
            total += b - max(a, right)
            right = b
    return total


def cost_records(outcomes):
    records = []

    def walk(value, task, path):
        if isinstance(value, list):
            for i, x in enumerate(value):
                walk(x, task, path + f"/{i}")
        elif isinstance(value, dict):
            # Provider attempt records are leaves; parent summaries can repeat cost totals.
            if "telemetry" in value or "estimated_cost_usd" in value:
                records.append(
                    {
                        "task_id": task,
                        "path": path,
                        "provider": value.get("provider"),
                        "model": value.get("model"),
                        "seconds": value.get("seconds"),
                        "estimated_cost_usd": value.get("estimated_cost_usd"),
                        "usage": (value.get("telemetry") or {}).get("usage"),
                    }
                )
            else:
                for k, x in value.items():
                    walk(x, task, path + "/" + k)

    for o in outcomes:
        walk(o.get("agent"), o["task_id"], o["path"] + "/agent")
        walk(o.get("parallel"), o["task_id"], o["path"] + "/parallel")
    return records


def baseline_report(paths, first, last):
    import staging_journal_report as sjr

    merged = sjr.merge_snapshots([sjr.load_journal(p) for p in paths], [str(p) for p in paths])
    window = sjr.select_window(merged["candidates"], first, last)
    raw = sjr.analyse(merged, window)
    rows = [sjr.describe(c) for c in window.values()]
    landed = [r for r in rows if r["outcome"] == "promoted"]
    start = min(r["dispatched_at"] for r in rows if r["run"]) / 1e6
    end = merged["horizon"] / 1e6
    promoted_ids = {r["id"] for r in landed}
    prs = len({m["pr"] for cid, c in window.items() if cid in promoted_ids for m in sjr.members(c)})
    return {
        "sources": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths],
        "candidate_range": [first, last],
        "from": iso(start),
        "until": iso(end),
        "hours": (end - start) / 3600,
        "landed_prs": prs,
        "landed_prs_per_hour": prs / ((end - start) / 3600),
        "head_cycle_minutes": None,
        "head_cycle_unknown": "Bors feed records candidate runs, not seal-to-landing head cycles",
        "run_minutes": raw["slot_occupancy"]["green_run_minutes"],
        "failed_rounds_per_large_landing": raw["failed_rounds"]["window_aggregate"],
        "singleton_runs": sum(v["runs"] for v in raw["singletons"]["by_kind"].values()),
        "repair_minutes": raw["handoffs"]["readmission_minutes"],
        "cost_usd": None,
        "cost_unknown": "Bors candidate journal contains no inference usage",
        "claim_collisions": None,
        "waiting_age_minutes": None,
        "shrink_events": None,
        "holds": None,
        "stalls": None,
    }


def prediction_metrics(events, attempts):
    """Predicted names and edits are observations; successful repair needs verification."""
    attempt_map = {a["id"]: a for a in attempts}
    predicted = set()
    before = []
    results = []
    for e in events:
        detail = e["detail"]
        if e["kind"] == "admission-predicted":
            for c in detail.get("collisions", []):
                predicted.add((e["batch_id"], c.get("name")))
        elif e["kind"] == "admission-repair-result":
            submitted_attempt = detail.get("attempt_id")
            binding = "submitted-attempt"
            if submitted_attempt is None:
                # Background preparation precedes the first head CI dispatch. Bind
                # only to the first subsequent dispatch of this same batch; a
                # later completed retry cannot substitute for an unobserved one.
                following = [
                    a
                    for a in attempts
                    if a["batch_id"] == e["batch_id"] and a.get("dispatched_at", float("-inf")) >= e["at"]
                ]
                a = min(following, key=lambda a: (a["dispatched_at"], a["id"])) if following else {}
                binding = "first-subsequent-same-batch-dispatch"
            else:
                a = attempt_map.get(submitted_attempt, {})
                if a.get("batch_id") != e["batch_id"]:
                    a = {}
            completed = a.get("run_completed_at")
            row = {
                "task_id": detail.get("task_id"),
                "batch": e["batch_id"],
                "attempt_id": submitted_attempt,
                "timing_attempt_id": a.get("id"),
                "completion_binding": binding if a else "unknown",
                "task_kind": detail.get("task_kind"),
                "input_tip": detail.get("input_tip"),
                "edited": detail.get("edited", []),
                "before_completed_verdict": e["at"] < completed if completed else None,
                "verified_fixed": detail.get("verified_fixed"),
                "verification": detail.get("verification"),
            }
            results.append(row)
            if row["before_completed_verdict"]:
                before.append(row)
    discovered = set()
    for a in attempts:
        raw = a.get("diagnostics") or []
        findings = json.loads(raw) if isinstance(raw, str) else raw
        for f in findings:
            if not isinstance(f, dict):
                continue
            if "duplicate" not in f.get("message", "").lower() and "duplicate" not in f.get("kind", "").lower():
                continue
            name = f.get("declaration")
            if not name:
                import re

                match = re.search(r"duplicate-declarations:\s*(\S+)", f.get("message", ""))
                name = match.group(1) if match else None
            if name:
                discovered.add((a["batch_id"], name))
    return {
        "predicted_unique_batch_names": len(predicted),
        "ci_discovered_unique_batch_names": len(discovered),
        "predicted_also_discovered_in_ci": len(predicted & discovered),
        "ci_discovered_without_prediction": len(discovered - predicted),
        "repair_results_before_verdict": len(before),
        "verified_fixed_before_verdict": sum(
            r["verified_fixed"] if isinstance(r["verified_fixed"], int) else len(r["verified_fixed"]) for r in before
        )
        if before and all(r["verified_fixed"] is not None for r in before)
        else None,
        "result_records": results,
        "coverage": "Only emitted admission-predicted/result events and named duplicate findings; zero before deployment is absence of instrumentation, not perfect prediction.",
    }


def analyse(snapshot, baseline, since=None, until=None):
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unexpected queue snapshot schema")
    tables = snapshot["tables"]
    captured = snapshot["captured_at"]
    batches = tables["batches"]
    landed_events = [e for e in tables["journal"] if e["kind"] == "landed"]
    # The first actual dispatch excludes shadow construction and its earlier seals.
    first_dispatch = min(a["dispatched_at"] for a in tables["ci_attempts"])
    start = since if since is not None else first_dispatch
    end = min(until, captured) if until is not None else captured
    if end <= start:
        raise ValueError("empty reporting time window")
    events = [e for e in tables["journal"] if start <= e["at"] < end]
    all_members = collections.defaultdict(list)
    for m in tables["members"]:
        all_members[m["batch_id"]].append(m)
    all_attempts = collections.defaultdict(list)
    for a in tables["ci_attempts"]:
        all_attempts[a["batch_id"]].append(a)
    task_claims = collections.defaultdict(list)
    for c in tables["claims"]:
        if c.get("task_id"):
            task_claims[c["task_id"]].append(c)
    heads = []
    for b in batches:
        if b["state"] != "landed" or not (start <= b["landed_at"] < end):
            continue
        members = [m for m in all_members[b["id"]] if not m.get("removed") and not m.get("withdrawn")]
        # Landing events positively record membership; the table alone may include withdrawn entries.
        landing = next((e for e in landed_events if e["batch_id"] == b["id"]), None)
        prs = landing["detail"].get("members") if landing else [m["number"] for m in members]
        attempts = all_attempts[b["id"]]
        claim_intervals = [
            (c["created_at"], c.get("released_at") or end)
            for cs in task_claims.values()
            for c in cs
            if c["batch_id"] == b["id"]
        ]
        task_intervals = claim_intervals
        head_start = b.get("first_dispatch_at")
        seal = b.get("sealed_at")
        ci_intervals = [(a["dispatched_at"], a.get("run_completed_at")) for a in attempts]
        horizon_start = seal or head_start
        total = b["landed_at"] - horizon_start if horizon_start else None
        ci = union_seconds(ci_intervals, horizon_start, b["landed_at"]) if horizon_start else None
        repair = union_seconds(task_intervals, horizon_start, b["landed_at"]) if horizon_start else None
        overlap = (
            union_seconds(
                [
                    (max(a, c), min(z, d))
                    for a, z in ci_intervals
                    for c, d in task_intervals
                    if z is not None and max(a, c) < min(z, d)
                ],
                horizon_start,
                b["landed_at"],
            )
            if horizon_start
            else 0
        )
        heads.append(
            {
                "batch": b["id"],
                "size": len(prs),
                "landed_prs": prs,
                "nonempty_prs": [
                    m["number"]
                    for m in members
                    if snapshot.get("commit_facts", {}).get(m["commit_sha"], {}).get("empty") is False
                ],
                "empty_members": [
                    m["number"]
                    for m in members
                    if snapshot.get("commit_facts", {}).get(m["commit_sha"], {}).get("empty") is True
                ],
                "already_landed_empty_members": [
                    m["number"]
                    for m in members
                    if snapshot.get("commit_facts", {}).get(m["commit_sha"], {}).get("empty") is True
                    and snapshot["commit_facts"][m["commit_sha"]].get("already_landed_note")
                ],
                "emptiness_unknown": sum(
                    snapshot.get("commit_facts", {}).get(m["commit_sha"], {}).get("empty") is None for m in members
                ),
                "seal": iso(seal),
                "first_dispatch": iso(head_start),
                "landed_at": iso(b["landed_at"]),
                "first_dispatch_to_landing_minutes": (b["landed_at"] - head_start) / 60 if head_start else None,
                "seal_to_landing_minutes": total / 60 if total is not None else None,
                "ci_observed_minutes": ci / 60 if ci is not None else None,
                "claimed_task_minutes": repair / 60 if repair is not None else None,
                "ci_task_overlap_minutes": overlap / 60,
                "other_or_unobserved_minutes": (total - ci - repair + overlap) / 60 if total is not None else None,
                "ci_rounds": len(attempts),
                "source_failure_rounds": sum(a["outcome"] == "source-failure" for a in attempts),
                "repair_rounds": sum(
                    t["kind"] == "repair" and t["state"] == "submitted"
                    for t in tables["tasks"]
                    if t["batch_id"] == b["id"]
                ),
                "edited": sum(m["provenance"] == "edited" for m in members),
                "withdrawn": sum(bool(m.get("withdrawn")) for m in all_members[b["id"]]),
                "tip": b["tip"],
            }
        )
    attempts = [a for a in tables["ci_attempts"] if start <= a["dispatched_at"] < end]
    # A unique input can be represented by several attempts; throughput counts only landings.
    prs = {p for h in heads for p in h["landed_prs"]}
    input_map = {i["id"]: i for i in tables["inputs"]}
    inserted_in_window = [m for m in tables["members"] if start <= m["added_at"] < end]
    timing_inconsistencies = [
        {
            "member_id": m["id"],
            "batch": m["batch_id"],
            "pr": m["number"],
            "approved_at": iso(input_map[m["input_id"]]["approved_at"]),
            "inserted_at": iso(m["added_at"]),
            "delta_seconds": m["added_at"] - input_map[m["input_id"]]["approved_at"],
        }
        for m in inserted_in_window
        if m["input_id"] in input_map and m["added_at"] < input_map[m["input_id"]]["approved_at"]
    ]
    admitted = [
        (m["added_at"] - input_map[m["input_id"]]["approved_at"]) / 60
        for m in inserted_in_window
        if m["input_id"] in input_map and m["added_at"] >= input_map[m["input_id"]]["approved_at"]
    ]
    insertion_coverage = {
        "membership_rows": len(tables["members"]),
        "insertions_in_window": len(inserted_in_window),
        "insertions_before_window": sum(m["added_at"] < start for m in tables["members"]),
        "insertions_at_or_after_window": sum(m["added_at"] >= end for m in tables["members"]),
        "selected_approvals_before_window": sum(
            input_map[m["input_id"]]["approved_at"] < start for m in inserted_in_window if m["input_id"] in input_map
        ),
        "missing_input": sum(m["input_id"] not in input_map for m in inserted_in_window),
        "timing_inconsistencies": len(timing_inconsistencies),
        "valid_nonnegative_waits": len(admitted),
        "selection": "Membership inserted in the half-open reporting window, regardless of approval time. Earlier shadow insertions are excluded; approvals before the window are included when insertion is in the window. Negative timestamp deltas are reported separately and excluded from the wait distribution.",
    }
    waiting = [
        (end - i["approved_at"]) / 60
        for i in tables["inputs"]
        if i["state"] in ("waiting", "reserved") and i["approved_at"] < end
    ]
    task_rows = []
    for t in tables["tasks"]:
        if not (start <= t["created_at"] < end or t.get("resolved_at") and start <= t["resolved_at"] < end):
            continue
        cs = task_claims[t["id"]]
        task_rows.append(
            {
                "id": t["id"],
                "batch": t["batch_id"],
                "kind": t["kind"],
                "state": t["state"],
                "wall_minutes": ((t.get("resolved_at") or end) - t["created_at"]) / 60,
                "claimed_minutes": union_seconds(
                    [(c["created_at"], c.get("released_at") or end) for c in cs], start, end
                )
                / 60,
                "claims": len(cs),
                "resolution": t.get("resolution"),
            }
        )
    usage = cost_records([o for o in snapshot.get("worker_outcomes", []) if o.get("modified_at", 0) <= end])
    usage = [u for u in usage if any(str(t["id"]) == str(u["task_id"]) for t in task_rows)]
    costs = [u["estimated_cost_usd"] for u in usage if u.get("estimated_cost_usd") is not None]
    counts = collections.Counter(e["kind"] for e in events)
    hold_spans = []
    hold_start = None
    for e in tables["journal"]:
        if e["kind"] == "inference-hold":
            hold_start = e["at"]
        elif e["kind"] == "inference-resumed" and hold_start is not None:
            hold_spans.append((hold_start, e["at"]))
            hold_start = None
    if hold_start is not None:
        hold_spans.append((hold_start, end))
    prediction = prediction_metrics(events, tables["ci_attempts"])
    rate = len(prs) / ((end - start) / 3600)
    large = [h for h in heads if h["size"] >= 110 and h["source_failure_rounds"] >= 2]
    scenario = [h for h in large if h["first_dispatch_to_landing_minutes"] < 120 and h["ci_rounds"] <= 3]
    coverage = (end - start) / 86400
    return {
        "schema": SCHEMA,
        "generated_at": iso(time.time()),
        "snapshot_captured_at": iso(captured),
        "window": {
            "from": iso(start),
            "until": iso(end),
            "hours": (end - start) / 3600,
            "days": coverage,
            "definition": "half-open wall-clock interval; landings counted once by PR, including queue downtime",
        },
        "baseline": baseline,
        "pilot": {
            "landed_heads": len(heads),
            "landed_prs": len(prs),
            "landed_prs_per_hour": rate,
            "landed_membership_occurrences": sum(h["size"] for h in heads),
            "nonempty_unique_prs_per_hour": len({p for h in heads for p in h["nonempty_prs"]}) / ((end - start) / 3600)
            if all(h["emptiness_unknown"] == 0 for h in heads)
            else None,
            "nonempty_unique_prs": len({p for h in heads for p in h["nonempty_prs"]})
            if all(h["emptiness_unknown"] == 0 for h in heads)
            else None,
            "empty_member_occurrences": sum(len(h["empty_members"]) for h in heads),
            "already_landed_empty_occurrences": sum(len(h["already_landed_empty_members"]) for h in heads),
            "emptiness_unknown": sum(h["emptiness_unknown"] for h in heads),
            "baseline_ratio": rate / baseline["landed_prs_per_hour"],
            "head_dispatch_minutes": dist([h["first_dispatch_to_landing_minutes"] for h in heads]),
            "head_seal_minutes": dist([h["seal_to_landing_minutes"] for h in heads]),
            "ci_attempts": len(attempts),
            "attempt_outcomes": dict(collections.Counter(a["outcome"] for a in attempts)),
            "singleton_runs": sum(len(all_members[a["batch_id"]]) == 1 for a in attempts),
            "singleton_confirmation_or_bisection_runs": None,
            "failed_rounds_per_large_landing": sum(h["source_failure_rounds"] for h in heads if h["size"] >= 15)
            / sum(h["size"] >= 15 for h in heads)
            if any(h["size"] >= 15 for h in heads)
            else None,
            "repair_task_minutes": dist([t["claimed_minutes"] for t in task_rows if t["kind"] == "repair"]),
            "claim_collisions": counts["claim-refused"],
            "claim_expiries": counts["claim-expired"],
            "approval_to_insertion_minutes": dist(admitted),
            "approval_to_insertion_coverage": insertion_coverage,
            "approval_to_insertion_timing_inconsistencies": timing_inconsistencies,
            "currently_waiting_age_minutes": dist(waiting),
            "opt_outs": counts["no-repair"],
            "anticipated_withdrawals": counts["problem-anticipated"],
            "needs_rebase": counts["needs-rebase"],
            "readmissions": counts["readmitted"],
            "edited_landed_members": sum(h["edited"] for h in heads),
            "inference_hold_events": counts["inference-hold"],
            "inference_hold_minutes": union_seconds(hold_spans, start, end) / 60,
            "stall_conditions": None,
            "shrink_to_empty_counter": snapshot.get("counters", {}).get("shrink_to_empty"),
            "final_empty_edited_members": [
                {"batch": h["batch"], "pr": m["number"], "commit": m["commit_sha"]}
                for h in heads
                for m in all_members[h["batch"]]
                if m["provenance"] == "edited"
                and m["number"] in h["empty_members"]
                and m["number"] not in h["already_landed_empty_members"]
            ],
            "admission_predictions": prediction,
            "known_estimated_cost_usd": sum(costs) if costs else None,
            "cost_records": len(usage),
            "unreadable_worker_outcomes": snapshot.get("outcome_errors", []),
            "cost_records_without_estimate": sum(u.get("estimated_cost_usd") is None for u in usage),
            "total_estimated_cost_usd": sum(costs) if usage and len(costs) == len(usage) else None,
        },
        "exit_criteria": {
            "110_pr_two_source_failures_under_120_minutes": {
                "status": "timing_met_verification_required"
                if scenario
                else "not_met_in_observed_data"
                if large
                else "not_observed",
                "eligible_batches": [h["batch"] for h in large],
                "qualifying_batches": [h["batch"] for h in scenario],
                "warm_artifacts_and_zero_singleton_verification": "not established by the journal alone",
            },
            "week_above_baseline_without_comment_or_attribution_regression": {
                "status": "not_yet_observable" if coverage < 7 else "requires_attribution_and_comment_audit",
                "observed_days": coverage,
                "observed_rate_above_baseline": rate > baseline["landed_prs_per_hour"],
            },
        },
        "unknowns": [
            "No persistent history of stall conditions; current status cannot prove a stall-free window.",
            "Final empty edited commits are listed separately from commits carrying the scripted already-landed note; an empty final contribution alone does not prove intent or quantify partial shrink.",
            "Hold duration comes from journal entry/resume intervals, clipped to the observation window; an active hold ends at the snapshot horizon.",
            "Agent cost is unknown for attempts without a recorded estimate/model; no current list price is guessed. Retained outcomes can omit earlier interrupted sessions, so the known subtotal is not a full spend audit.",
            "CI durations use observed run completion; superseded runs with no completion remain unmeasured, not zero-length executions.",
            "Claimed task time includes build preparation and local checks; it is not inference-only time.",
            "Comment and attribution correctness require a separate retained-chain/GitHub audit.",
            "Queue singleton builds can be natural one-member heads; the journal has no explicit singleton purpose, so confirmation/bisection counts remain unknown.",
            "The first-day burst includes a large inherited backlog and empty contributions already on main; it is not evidence of week-long sustained throughput.",
        ],
        "heads": heads,
        "tasks": task_rows,
        "agent_attempts": usage,
        "ci_attempts": [
            {
                k: a.get(k)
                for k in (
                    "id",
                    "batch_id",
                    "batch_version",
                    "head_sha",
                    "run_id",
                    "outcome",
                    "outcome_detail",
                    "dispatched_at",
                    "run_started_at",
                    "run_completed_at",
                )
            }
            for a in attempts
        ],
    }


def render(report):
    def fmt(x):
        return f"{x:.2f}" if x is not None else "unknown"

    p, b = report["pilot"], report["baseline"]
    lines = [
        "# Queue pilot measurements",
        "",
        f"Data through {report['snapshot_captured_at']}. Window {report['window']['from']} to {report['window']['until']} ({report['window']['hours']:.2f} hours).",
        "",
        "Throughput counts each landed PR once, across the full wall-clock window. CI attempts are not landings.",
        "",
        "| Measurement | Queue pilot | Bors baseline |",
        "| --- | ---: | ---: |",
        f"| Unique landed PR identities | {p['landed_prs']} | {b['landed_prs']} |",
        f"| Nonempty unique contributions | {p['nonempty_unique_prs']} | unknown |",
        f"| Empty memberships / already-landed note | {p['empty_member_occurrences']} / {p['already_landed_empty_occurrences']} | unknown |",
        f"| Unique landed PR identities/hour | {p['landed_prs_per_hour']:.2f} | {b['landed_prs_per_hour']:.2f} |",
        f"| Nonempty unique contributions/hour | {fmt(p['nonempty_unique_prs_per_hour'])} | unknown |",
        f"| Source-failed rounds per landed large head | {fmt(p['failed_rounds_per_large_landing'])} | {fmt(b['failed_rounds_per_large_landing'])} |",
        f"| Singleton CI runs (including natural one-PR heads) | {p['singleton_runs']} | {b['singleton_runs']} |",
        f"| Claim collisions / expiries | {p['claim_collisions']} / {p['claim_expiries']} | unknown |",
        f"| Edited landed members | {p['edited_landed_members']} | unknown |",
        f"| Opt-out events / anticipated withdrawals | {p['opt_outs']} / {p['anticipated_withdrawals']} | unknown |",
        f"| Inference hold events / minutes | {p['inference_hold_events']} / {fmt(p['inference_hold_minutes'])} | unknown |",
        "| Historical stall conditions | unknown | unknown |",
        f"| Predicted / CI-discovered duplicate names | {p['admission_predictions']['predicted_unique_batch_names']} / {p['admission_predictions']['ci_discovered_unique_batch_names']} | unknown |",
        f"| Verified predictions fixed before verdict | {p['admission_predictions']['verified_fixed_before_verdict'] if p['admission_predictions']['verified_fixed_before_verdict'] is not None else 'unknown'} | unknown |",
        f"| Recorded inference cost subtotal, USD | {fmt(p['known_estimated_cost_usd'])} ({p['cost_records_without_estimate']} attempts lack estimates) | unknown |",
        "",
        "Baseline sources: " + ", ".join(f"`{s['path']}` (SHA-256 `{s['sha256']}`)" for s in b["sources"]) + ".",
        "",
        "| Head | PRs | CI rounds | Source failures | Repair rounds | Dispatch → landed, min | Seal → landed, min | CI observed, min | Claimed task, min | Other/unobserved, min |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for h in report["heads"]:
        lines.append(
            "| "
            + " | ".join(
                str(x)
                for x in [
                    h["batch"],
                    h["size"],
                    h["ci_rounds"],
                    h["source_failure_rounds"],
                    h["repair_rounds"],
                    fmt(h["first_dispatch_to_landing_minutes"]),
                    fmt(h["seal_to_landing_minutes"]),
                    fmt(h["ci_observed_minutes"]),
                    fmt(h["claimed_task_minutes"]),
                    fmt(h["other_or_unobserved_minutes"]),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "CI/task overlap is recorded separately in JSON; those columns must not simply be added. Seal time can precede publication during shadow operation.",
        "",
        "| Task | Kind | State | Wall minutes | Claimed minutes | Claims |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for t in report["tasks"]:
        lines.append(
            f"| {t['id']} | {t['kind']} | {t['state']} | {t['wall_minutes']:.2f} | {t['claimed_minutes']:.2f} | {t['claims']} |"
        )
    lines += [
        "",
        f"Approval → insertion minutes (valid nonnegative waits): `{p['approval_to_insertion_minutes']}`. Current waiting age: `{p['currently_waiting_age_minutes']}`.",
        "",
        f"Waiting-age coverage: {p['approval_to_insertion_coverage']['insertions_in_window']} insertions in the reporting window, of which {p['approval_to_insertion_coverage']['selected_approvals_before_window']} were approved before it. {p['approval_to_insertion_coverage']['insertions_before_window']} earlier insertions (including shadow preparation) are excluded. The distribution contains {p['approval_to_insertion_coverage']['valid_nonnegative_waits']} valid waits; {p['approval_to_insertion_coverage']['timing_inconsistencies']} negative approval-to-insertion timestamp deltas are listed separately in JSON as timing inconsistencies, not valid negative wait.",
        "",
        "Exit criteria:",
        "",
    ]
    for name, value in report["exit_criteria"].items():
        lines.append(f"- `{name}`: **{value['status']}**. `{json.dumps(value, sort_keys=True)}`")
    lines += ["", "Limits:", ""] + ["- " + x for x in report["unknowns"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="read SQLite and worker outcome files; write sanitized JSON")
    ex.add_argument("--database", type=Path, required=True)
    ex.add_argument("--worker-root", type=Path, action="append", default=[])
    ex.add_argument("--output", type=Path)
    ex.add_argument("--clone", type=Path, help="optional content clone for retained commit emptiness")
    rep = sub.add_parser("report", help="compare exported queue state with saved Bors feeds")
    rep.add_argument("--snapshot", type=Path, required=True)
    rep.add_argument("--baseline", type=Path, action="append", required=True)
    rep.add_argument("--baseline-first", type=int, default=1833)
    rep.add_argument("--baseline-last", type=int, default=1932)
    rep.add_argument("--since", help="UTC ISO timestamp; defaults to first production dispatch")
    rep.add_argument("--until", help="UTC ISO timestamp; defaults to snapshot capture")
    rep.add_argument("--markdown", type=Path, required=True)
    rep.add_argument("--json", type=Path, required=True)
    daily = sub.add_parser("daily", help="archive a fresh snapshot and rolling-day report in one invocation")
    daily.add_argument("--database", type=Path, required=True)
    daily.add_argument("--clone", type=Path)
    daily.add_argument("--worker-root", type=Path, action="append", default=[])
    daily.add_argument("--baseline", type=Path, action="append", required=True)
    daily.add_argument("--baseline-first", type=int, default=1833)
    daily.add_argument("--baseline-last", type=int, default=1932)
    daily.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "daily":
        snapshot = export(args.database, args.worker_root, args.clone)
        directory = args.directory / datetime.fromtimestamp(snapshot["captured_at"], UTC).strftime("%Y%m%dT%H%M%SZ")
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "snapshot.json"
        path.write_text(json.dumps(snapshot, separators=(",", ":")) + "\n")
        first = min(a["dispatched_at"] for a in snapshot["tables"]["ci_attempts"])
        since = max(first, snapshot["captured_at"] - 86400)
        return main(
            [
                "report",
                "--snapshot",
                str(path),
                "--since",
                iso(since),
                "--baseline-first",
                str(args.baseline_first),
                "--baseline-last",
                str(args.baseline_last),
                "--markdown",
                str(directory / "report.md"),
                "--json",
                str(directory / "report.json"),
                *[x for p in args.baseline for x in ("--baseline", str(p))],
            ]
        )
    if args.command == "export":
        data = json.dumps(export(args.database, args.worker_root, args.clone), separators=(",", ":")) + "\n"
        if args.output:
            args.output.write_text(data)
        else:
            print(data, end="")
    else:

        def stamp(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None

        snapshot = json.loads(args.snapshot.read_text())
        result = analyse(
            snapshot,
            baseline_report(args.baseline, args.baseline_first, args.baseline_last),
            stamp(args.since),
            stamp(args.until),
        )
        result["provenance"] = {
            "snapshot": str(args.snapshot),
            "snapshot_sha256": hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
            "database": snapshot["database"],
            "worker_roots": snapshot.get("worker_roots", []),
        }
        args.json.write_text(json.dumps(result, indent=2) + "\n")
        args.markdown.write_text(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Portable presentation and aggregation of prepared activity (stdlib only).

Fleet vendors this module from the pinned worker; accounting and presentation
policy live here so other consumers can use the same summaries.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal

WINDOWS = ("3h", "24h", "7d", "total")
COST_WINDOWS = ("7d", "total")
RECOVERY_COUNTS = ("records", "retained", "blocked", "preserved", "reset", "superseded", "incomplete", "corrupt")


def amount(value, *, rate=False):
    if value is None:
        return "—"
    n = Decimal(value)
    if n.is_infinite():
        return "$∞"
    if rate and 0 < n < Decimal("0.0001"):
        return "<$0.0001"
    return f"${n:.4f}" if rate else f"${n:.2f}"


def number(value):
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".") if "." in str(value) else str(value)


def hourly_rate(spent, covered_seconds):
    """A wall-clock average of accrued costs; reservations are not spending."""
    return (
        str(Decimal(spent) * 3600 / Decimal(str(covered_seconds)))
        if spent is not None and covered_seconds and covered_seconds > 0
        else None
    )


def measured_pr_weight(row):
    """Full weight for measured landed PRs, half for measured open PRs."""
    if row.get("measured_pr_weight") is not None:
        return Decimal(str(row["measured_pr_weight"]))
    # Older prepared summaries have only unweighted coverage counts.
    weight = Decimal(row.get("landed_prs", 0)) + Decimal(row.get("open_prs", 0)) / 2
    known = row.get("known_loc_prs", 0)
    count = known + row.get("missing_loc_prs", 0)
    return weight * known / count if count else weight


def loc_estimate(row):
    """Keep partial PR coverage useful without fetching any additional data."""
    measured = Decimal(row.get("landed_loc_added", 0)) + Decimal(row.get("open_loc_added", 0)) / 2
    weighted = measured
    missing = bool(row.get("missing_loc_prs"))
    if missing:
        known_weight = measured_pr_weight(row)
        total_weight = Decimal(row.get("landed_prs", 0)) + Decimal(row.get("open_prs", 0)) / 2
        if known_weight > 0:
            weighted = measured * total_weight / known_weight
    cost = row.get("estimated_usd")
    rate = None
    if cost is not None:
        cost = Decimal(cost)
        # No measured output means zero LOC, never an invented positive denominator.
        rate = cost / weighted if weighted else Decimal("Infinity") if cost else Decimal(0)
    return dict(
        measured_weighted_loc=str(measured),
        weighted_loc=str(weighted),
        loc_estimated=missing,
        usd_per_loc=str(rate) if rate is not None else None,
    )


def display(value, default_kinds=()):
    if not value.get("available"):
        print("Activity: " + value.get("reason", "unavailable"))
        return
    s = value["summary"]
    age = max(0, int(time.time() - s["prepared_at"]))
    print(f"Activity: prepared {age}s ago" + (" (stale)" if age > 180 else ""))
    since = s.get("history_since")
    print("Activity since: " + (datetime.fromtimestamp(since, UTC).isoformat(timespec="seconds") if since else "none"))
    kinds = list(dict.fromkeys(s.get("round_types", default_kinds)))
    kinds += sorted({kind for rows in s["rounds"].values() for kind in rows} - set(kinds))
    rounds = []
    for kind in ("ALL", *kinds):
        counts = [
            sum(r["finished"] for r in s["rounds"][w].values())
            if kind == "ALL"
            else s["rounds"][w].get(kind, {}).get("finished", 0)
            for w in WINDOWS
        ]
        rounds.append(("  " + kind, counts))
    rounds.append(("    failed/timed out", [sum(r["failed"] for r in s["rounds"][w].values()) for w in WINDOWS]))
    prs = []
    for key, label in (
        ("authored", "PRs authored"),
        ("landed", "Authored PRs landed"),
        ("assisted", "PRs assisted"),
        ("reviews", "Reviews completed"),
    ):
        if key in ("authored", "landed") or all(s["pr_windows"][w].get(key) is not None for w in WINDOWS):
            prs.append((label, [s["pr_windows"][w].get(key, 0) for w in WINDOWS]))
    ci = s.get("ci_windows")
    ci_rows = []
    if ci:
        for key, label in (
            ("attempts", "CI jobs finished"),
            ("successes", "CI jobs passed"),
            ("failures", "CI jobs failed"),
        ):
            ci_rows.append((label, [ci[w][key] for w in WINDOWS]))
        for key, label in (("wait_seconds", "Mean CI queue wait (s)"), ("run_seconds", "Mean CI runtime (s)")):
            ci_rows.append(
                (label, [f"{ci[w][key] / ci[w]['attempts']:.1f}" if ci[w]["attempts"] else "—" for w in WINDOWS])
            )
    costs = s.get("costs", {})
    spending_title = (
        "Spending (recorded usage)" if costs.get("accounting_version", 1) >= 2 else "Spending (completed sessions)"
    )
    spending_rows = []
    if costs.get("spending"):
        spending = costs["spending"]
        hours = [spending[w].get("worker_seconds") for w in WINDOWS]
        spending_rows = [
            (
                "Worker-hours recorded",
                [
                    "—" if seconds is None else "<0.01" if 0 < seconds < 36 else f"{seconds / 3600:.2f}"
                    for seconds in hours
                ],
            ),
            (
                "API spend ($/worker-hour)",
                [
                    amount(spending[w].get("usd_per_worker_hour", hourly_rate(spending[w].get("spent_usd"), hours[i])))
                    for i, w in enumerate(WINDOWS)
                ],
            ),
            ("API spend ($/elapsed hour)", [amount(spending[w].get("usd_per_hour")) for w in WINDOWS]),
        ]
    cost_rows = []
    if costs.get("windows"):
        # Compatibility for snapshots prepared before partial-coverage estimates.
        windows = {w: {**r, **loc_estimate(r)} if "loc_estimated" not in r else r for w, r in costs["windows"].items()}
        for key, label in (
            ("spent_usd", "API cost: completed sessions"),
            (
                "active_accrued_usd" if costs.get("accounting_version", 1) >= 2 else "pending_usd",
                "API cost: running/interrupted"
                if costs.get("accounting_version", 1) >= 2
                else "API cost: active estimates",
            ),
            ("estimated_usd", "API cost: estimated total"),
        ):
            cost_rows.append((label, [amount(windows[w].get(key)) for w in COST_WINDOWS]))
        for key, label in (
            ("landed_loc_added", "Authored Lean LOC: landed"),
            ("open_loc_added", "Authored Lean LOC: open"),
        ):
            cost_rows.append((label, [str(windows[w].get(key, 0)) for w in COST_WINDOWS]))
        cost_rows.append(
            (
                "Lean LOC (landed + ½ open)",
                [
                    "~" + number(f"{Decimal(windows[w]['weighted_loc']):.1f}")
                    if windows[w].get("loc_estimated")
                    else number(windows[w].get("weighted_loc", 0))
                    for w in COST_WINDOWS
                ],
            )
        )
        cost_rows.append(
            (
                "Estimated $ / weighted LOC",
                [
                    ("~" if windows[w].get("loc_estimated") else "") + amount(windows[w].get("usd_per_loc"), rate=True)
                    for w in COST_WINDOWS
                ],
            )
        )
        if any(windows[w].get("missing_loc_prs") for w in COST_WINDOWS):
            cost_rows.append(
                (
                    "Authored PRs with measured LOC",
                    [
                        f"{windows[w].get('known_loc_prs', 0)}/{windows[w].get('known_loc_prs', 0) + windows[w].get('missing_loc_prs', 0)}"
                        for w in COST_WINDOWS
                    ],
                )
            )
    label_width = max(
        len("Spending (recorded usage)"),
        *(len(label) for label, _ in [*rounds, *prs, *ci_rows, *cost_rows, *spending_rows]),
    )
    headers = ("3h", "24h", "7d", "All time")
    widths = [
        max(len(h), *(len(str(values[i])) for _, values in [*rounds, *prs, *ci_rows, *spending_rows]))
        for i, h in enumerate(headers)
    ]

    def table(title, cols, rows, col_widths=None):
        print()
        ws = col_widths or [max(len(h), *(len(str(values[i])) for _, values in rows)) for i, h in enumerate(cols)]
        if title is not None:
            print(f"{title:<{label_width}}  " + "  ".join(f"{h:>{width}}" for h, width in zip(cols, ws, strict=True)))
        for label, values in rows:
            print(
                f"{label:<{label_width}}  "
                + "  ".join(f"{str(v):>{width}}" for v, width in zip(values, ws, strict=True))
            )

    table("Finished rounds", headers, rounds, widths)
    table(None, headers, prs, widths)
    if spending_rows:
        table(spending_title, ("3h", "24h", "7d", "All time"), spending_rows, widths)
    if spending_rows and costs.get("quality") and costs.get("accounting_version", 1) >= 2:
        q = costs["quality"]
        print(
            f"Costs: native totals where available; token/interval estimates otherwise; {q.get('incomplete', 0)} conversations have incomplete usage. Reservations excluded."
        )
    if cost_rows:
        table("Costs / initiated PR output", ("7 days", "All time"), cost_rows)
        if any(windows[w].get("loc_estimated") for w in COST_WINDOWS):
            print("~ Missing LOC estimated from measured PRs; zero if none measured. Unknown PR states count as zero.")
    funding = costs.get("funding")
    if funding:
        print()
        print(
            f"Budget: {amount(funding['available'])} available; {amount(funding['pending'])} pending; {amount(funding['grant_rate'])}/hour replenishment"
        )
    sharing = s.get("shared_mathlib")
    if sharing and (
        sharing.get("shared") or sharing.get("private") or sharing.get("preparing") or sharing.get("blocked")
    ):
        print()
        print(
            f"Mathlib builds (latest per worker): {sharing.get('shared', 0)} shared, {sharing.get('private', 0)} private, {sharing.get('preparing', 0)} preparing, {sharing.get('blocked', 0)} blocked; {sharing.get('cache_hits', 0)} cache hits; {sharing.get('preparation_seconds', 0):.1f}s preparation"
        )
        for reason in sharing.get("reasons", []):
            print("  " + reason)
    recovery = s.get("work_recovery")
    if recovery and any(recovery.get(key) for key in RECOVERY_COUNTS):
        print()
        print(
            f"Local work recovery: {recovery.get('records', 0)} saved, "
            f"{recovery.get('retained', 0)} checkouts moved aside, "
            f"{recovery.get('incomplete', 0)} incomplete, {recovery.get('corrupt', 0)} corrupt"
        )
        for reason in recovery.get("reasons", []):
            print("  " + reason)
    if ci_rows:
        table("CI history", headers, ci_rows, widths)
    print()
    count = s["running"] if s["running"] is not None else "unknown"
    current = f"Currently: {count} {'worker' if count == 1 else 'workers'} working; {s['open_prs']} open authored PRs"
    if s["unknown_prs"]:
        current += f"; {s['unknown_prs']} PR states pending"
    if s.get("unknown_landings"):
        current += f"; {s['unknown_landings']} merge timestamps pending"
    print(current)
    if s.get("interrupted_unfinished"):
        print(f"Interrupted without finish receipts: {s['interrupted_unfinished']} (outside finished counts)")
    observed = s.get("pr_observed_at")
    print(
        "PR evidence: "
        + (f"oldest observation {max(0, int(time.time() - observed))}s ago" if observed else "not yet available")
    )
    for error in value.get("errors", []):
        print("Activity coverage: " + error)


def combine(values):
    """Combine distinct machine summaries; pool costs/LOC before dividing."""
    known = [v["summary"] for v in values if v.get("available")]
    if not known:
        return dict(available=False, reason="No prepared activity available")
    errors = [e for v in values for e in v.get("errors", [])]
    if len(known) != len(values):
        errors.append("Some hosts have no prepared activity")
    accounting_versions = {v.get("costs", {}).get("accounting_version", 1) for v in known}
    compatible_costs = len(accounting_versions) == 1
    if not compatible_costs:
        errors.append("Hosts use different cost accounting versions; refresh their native activity collectors")
    s = dict(
        prepared_at=min(v["prepared_at"] for v in known),
        history_since=min((v["history_since"] for v in known if v.get("history_since")), default=None),
        round_types=list(dict.fromkeys(k for v in known for k in v.get("round_types", []))),
        rounds={w: {} for w in WINDOWS},
        pr_windows={w: {} for w in WINDOWS},
        costs=dict(windows={}, accounting_version=min(v.get("costs", {}).get("accounting_version", 1) for v in known)),
        ci_windows=None,
        running=sum(v["running"] for v in known) if all(v["running"] is not None for v in known) else None,
        interrupted_unfinished=sum(v.get("interrupted_unfinished", 0) for v in known),
        open_prs=sum(v["open_prs"] for v in known),
        unknown_prs=sum(v["unknown_prs"] for v in known),
        unknown_landings=sum(v["unknown_landings"] for v in known),
        pr_observed_at=min((v["pr_observed_at"] for v in known if v.get("pr_observed_at")), default=None),
    )
    for v in known:
        for w, types in v["rounds"].items():
            for kind, counts in types.items():
                row = s["rounds"][w].setdefault(kind, dict(finished=0, failed=0, unknown=0))
                for key in row:
                    row[key] += counts.get(key, 0)
    for w in WINDOWS:
        for key in ("authored", "landed", "reviews", "fixes"):
            s["pr_windows"][w][key] = sum(v["pr_windows"][w].get(key, 0) for v in known)
        # Unique assisted PR counts are per-host; review events are additive.
        s["pr_windows"][w]["assisted"] = known[0]["pr_windows"][w].get("assisted") if len(known) == 1 else None
    for w in COST_WINDOWS:
        rows = [v.get("costs", {}).get("windows", {}).get(w, {}) for v in known]
        row = {
            k: sum(r.get(k, 0) for r in rows)
            for k in (
                "landed_loc_added",
                "open_loc_added",
                "missing_loc_prs",
                "known_loc_prs",
                "landed_prs",
                "open_prs",
            )
        }
        for key in ("spent_usd", "active_accrued_usd", "pending_usd", "estimated_usd"):
            row[key] = (
                str(sum((Decimal(r[key]) for r in rows), Decimal(0)))
                if compatible_costs and all(r.get(key) is not None for r in rows) and len(known) == len(values)
                else None
            )
        row["measured_pr_weight"] = float(sum((measured_pr_weight(r) for r in rows), Decimal(0)))
        row.update(loc_estimate(row))
        s["costs"]["windows"][w] = row
    if any(v.get("costs", {}).get("spending") for v in known):
        s["costs"]["spending"] = {}
        for w in WINDOWS:
            rows = [v.get("costs", {}).get("spending", {}).get(w, {}) for v in known]
            complete = (
                compatible_costs and len(known) == len(values) and all(r.get("spent_usd") is not None for r in rows)
            )
            spent = str(sum((Decimal(r["spent_usd"]) for r in rows), Decimal(0))) if complete else None
            # Parallel machines share wall-clock time; neither sum durations nor
            # average their rates. A newly joined host contributes only its costs.
            covered = max((r.get("covered_seconds", 0) for r in rows), default=0)
            worker_seconds = (
                sum(r["worker_seconds"] for r in rows)
                if len(known) == len(values) and all(r.get("worker_seconds") is not None for r in rows)
                else None
            )
            s["costs"]["spending"][w] = dict(
                spent_usd=spent,
                covered_seconds=covered,
                worker_seconds=worker_seconds,
                usd_per_hour=hourly_rate(spent, covered),
                usd_per_worker_hour=hourly_rate(spent, worker_seconds),
            )
    s["costs"]["quality"] = {
        k: sum(v.get("costs", {}).get("quality", {}).get(k, 0) for v in known)
        for k in ("native", "estimated", "incomplete")
    }
    sharing = [v.get("shared_mathlib", {}) for v in known]
    if any(sharing):
        s["shared_mathlib"] = {
            key: sum(row.get(key, 0) for row in sharing)
            for key in ("shared", "private", "preparing", "blocked", "cache_hits", "preparation_seconds")
        }
        s["shared_mathlib"]["reasons"] = sorted({reason for row in sharing for reason in row.get("reasons", [])})
    recovery = [v.get("work_recovery", {}) for v in known]
    if any(recovery):
        s["work_recovery"] = {key: sum(row.get(key, 0) for row in recovery) for key in RECOVERY_COUNTS}
        s["work_recovery"]["reasons"] = sorted({reason for row in recovery for reason in row.get("reasons", [])})
    funds = [v.get("costs", {}).get("funding") for v in known]
    if all(funds) and len(known) == len(values):
        s["costs"]["funding"] = {
            k: str(sum((Decimal(f[k]) for f in funds), Decimal(0)))
            for k in ("balance", "pending", "available", "grant_rate")
        }
    if all(v.get("ci_windows") for v in known):
        s["ci_windows"] = {
            w: {k: sum(v["ci_windows"][w][k] for v in known) for k in known[0]["ci_windows"][w]} for w in WINDOWS
        }
    return dict(available=True, summary=s, errors=errors)

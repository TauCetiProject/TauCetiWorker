"""Portable presentation and aggregation of prepared activity (stdlib only).

Fleet vendors this module from the pinned worker; accounting and presentation
policy live here so other consumers can use the same summaries.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal

WINDOWS = ("3h", "24h", "7d", "total")
COST_WINDOWS = ("total", "7d")


def amount(value, *, rate=False):
    if value is None:
        return "—"
    n = Decimal(value)
    if rate and 0 < n < Decimal("0.0001"):
        return "<$0.0001"
    return f"${n:.4f}" if rate else f"${n:.2f}"


def number(value):
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".") if "." in str(value) else str(value)


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
    cost_rows = []
    if costs.get("windows"):
        windows = costs["windows"]
        for key, label in (
            ("spent_usd", "API cost: completed sessions"),
            ("pending_usd", "API cost: active estimates"),
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
                    number(windows[w].get("weighted_loc", 0)) + (" + ?" if windows[w].get("missing_loc_prs") else "")
                    for w in COST_WINDOWS
                ],
            )
        )
        cost_rows.append(
            ("Estimated $ / weighted LOC", [amount(windows[w].get("usd_per_loc"), rate=True) for w in COST_WINDOWS])
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
    label_width = max(len("Finished rounds"), *(len(label) for label, _ in [*rounds, *prs, *ci_rows, *cost_rows]))
    headers = ("3h", "24h", "7d", "Recorded total")
    widths = [
        max(len(h), *(len(str(values[i])) for _, values in [*rounds, *prs, *ci_rows])) for i, h in enumerate(headers)
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
    if cost_rows:
        table("Costs / initiated PR output", ("All time", "7 days"), cost_rows)
    funding = costs.get("funding")
    if funding:
        print()
        print(
            f"Budget: {amount(funding['available'])} available; {amount(funding['pending'])} pending; {amount(funding['grant_rate'])}/hour replenishment"
        )
    if ci_rows:
        table("CI history", headers, ci_rows, widths)
    print()
    print(
        f"Currently: {s['running'] if s['running'] is not None else 'unknown'} rounds running; {s['open_prs']} known open authored PRs; {s['unknown_prs']} PR states unknown; {s['unknown_landings']} closed/merged PRs lack landing evidence"
    )
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
    s = dict(
        prepared_at=min(v["prepared_at"] for v in known),
        history_since=min((v["history_since"] for v in known if v.get("history_since")), default=None),
        round_types=list(dict.fromkeys(k for v in known for k in v.get("round_types", []))),
        rounds={w: {} for w in WINDOWS},
        pr_windows={w: {} for w in WINDOWS},
        costs=dict(windows={}),
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
        for key in ("spent_usd", "pending_usd", "estimated_usd"):
            row[key] = (
                str(sum((Decimal(r[key]) for r in rows), Decimal(0)))
                if all(r.get(key) is not None for r in rows) and len(known) == len(values)
                else None
            )
        weighted = Decimal(row["landed_loc_added"]) + Decimal(row["open_loc_added"]) / 2
        row["weighted_loc"] = str(weighted)
        row["usd_per_loc"] = (
            str(Decimal(row["estimated_usd"]) / weighted)
            if row["estimated_usd"] is not None and weighted > 0 and not row["missing_loc_prs"]
            else None
        )
        s["costs"]["windows"][w] = row
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

"""Read-only rolling statistics for the current controller ledger, without provider calls."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = "tauceti-queue.statistics/v1"
WINDOWS = (("1h", 3600), ("3h", 10800), ("24h", 86400), ("7d", 604800))


def percentile(values, fraction):
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lo = int(index)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def snapshot(database, repository, branch, *, now=None):
    """One consistent SQLite snapshot. Run durations include setup/reporting/publication."""
    now = time.time() if now is None else now
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as c, c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        if c.execute("SELECT 1 FROM settings WHERE key='source-ci:binding'").fetchone():
            raise ValueError("qualified source statistics need an explicit public policy")
        batches = c.execute(
            "SELECT id,landed_at FROM batches WHERE repository=? AND state='landed'", (repository,)
        ).fetchall()
        members = c.execute(
            "SELECT m.batch_id,m.number,i.state,i.landed_at FROM members m JOIN batches b ON b.id=m.batch_id "
            "JOIN inputs i ON i.id=m.input_id WHERE b.repository=? AND m.removed=0 AND m.withdrawn=0",
            (repository,),
        ).fetchall()
        attempts = c.execute(
            "SELECT a.* FROM ci_attempts a JOIN batches b ON b.id=a.batch_id "
            "WHERE b.repository=? AND a.dispatch_state NOT IN ('prepared','rejected')",
            (repository,),
        ).fetchall()
        first = c.execute("SELECT min(created_at) FROM batches WHERE repository=?", (repository,)).fetchone()[0]
    windows = []
    for label, seconds in WINDOWS:
        lower = now - seconds
        landed = {b["id"] for b in batches if b["landed_at"] is not None and lower < b["landed_at"] <= now}
        # Input landing receipts are positive evidence. A withdrawn/removed member never counts.
        prs = {
            m["number"]
            for m in members
            if m["batch_id"] in landed
            and m["state"] == "landed"
            and m["landed_at"] is not None
            and lower < m["landed_at"] <= now
        }
        selected = [
            a
            for a in attempts
            if a["outcome"] != "pending"
            and (ended := a["run_completed_at"] or a["last_progress_at"] or a["dispatched_at"]) is not None
            and lower < ended <= now
        ]
        durations = sorted(
            a["run_completed_at"] - a["run_started_at"]
            for a in selected
            if a["run_started_at"] is not None
            and a["run_completed_at"] is not None
            and 0 <= a["run_started_at"] <= a["run_completed_at"] <= now
        )
        windows.append(
            dict(
                window=label,
                landed_batches=len(landed),
                landed_prs=len(prs),
                ci_completed=len(selected),
                ci_passed=sum(a["outcome"] == "pass" for a in selected),
                ci_source_failed=sum(a["outcome"] == "source-failure" for a in selected),
                ci_infrastructure=sum(a["outcome"] == "infrastructure" for a in selected),
                ci_superseded=sum(a["outcome"] == "superseded" for a in selected),
                run_duration_samples=len(durations),
                run_p50_seconds=percentile(durations, 0.5),
                run_p95_seconds=percentile(durations, 0.95),
            )
        )
    last = max(
        (b for b in batches if b["landed_at"] is not None and b["landed_at"] <= now),
        key=lambda b: b["landed_at"],
        default=None,
    )
    return dict(
        schema=SCHEMA,
        repository=repository,
        canonical_branch=branch,
        observed_at=now,
        coverage="controller_ledger",
        since=first,
        windows=windows,
        last_landing=None
        if last is None
        else dict(
            batch_id=last["id"],
            at=last["landed_at"],
            prs=len({m["number"] for m in members if m["batch_id"] == last["id"] and m["state"] == "landed"}),
        ),
        duration_definition="CI workflow run duration, including setup, reporting and publication; not build-only runtime",
    )

"""Read-only rolling statistics for the current controller ledger, without provider calls."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = "tauceti-queue.statistics/v1"
LANDINGS_SCHEMA = "tauceti-queue.landings/v1"
LANDINGS_MAX_DAYS = 35
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


def _forge(identity):
    """`github` for a primary-source input (no identity), else the identity's forge kind.

    A malformed stored identity is the ledger's fault, not the caller's, so it raises
    RuntimeError (a server error), never ValueError (which the server answers as a bad request).
    """
    if not identity:
        return "github"
    try:
        parsed = json.loads(identity)
    except ValueError as error:
        raise RuntimeError("input source identity is not JSON") from error
    forge = parsed.get("forge") if isinstance(parsed, dict) else None
    kind = forge.get("kind") if isinstance(forge, dict) else None
    if not isinstance(kind, str) or not kind:
        raise RuntimeError("input source identity names no forge")
    return kind


def landings(database, repository, *, days, now=None):
    """Each pull request landed in the last `days` days, with when the queue first approved it.

    Approval is the queue admitting a head of the pull request after an all-green review, and a
    pull request admitted more than once (a new head, a withdrawal) keeps its first admission, so
    opening-to-approval plus approval-to-land is its whole opening-to-land time. Landing uses the
    same positive evidence as `snapshot`: the input's landing receipt, the first if there are two.
    Opening times are the forges' and are not held here.
    """
    if type(days) is not int or not 1 <= days <= LANDINGS_MAX_DAYS:
        raise ValueError(f"days must be an integer from 1 to {LANDINGS_MAX_DAYS}")
    now = time.time() if now is None else now
    lower = now - days * 86400
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as c, c:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        if c.execute("SELECT 1 FROM settings WHERE key='source-ci:binding'").fetchone():
            raise ValueError("qualified source statistics need an explicit public policy")
        # The earliest receipt across the whole ledger, then the window: a pull request with a
        # second receipt inside the window landed before it, and does not belong to it.
        rows = c.execute(
            "SELECT l.number, l.source_identity, l.landed, f.first FROM "
            "(SELECT number, source_identity, min(landed_at) AS landed FROM inputs "
            "  WHERE repository=? AND state='landed' AND landed_at IS NOT NULL "
            "  GROUP BY number, source_identity) l "
            "JOIN (SELECT number, source_identity, min(approved_at) AS first FROM inputs "
            "  WHERE repository=? GROUP BY number, source_identity) f "
            "  ON f.number=l.number AND f.source_identity=l.source_identity "
            "WHERE l.landed > ? AND l.landed <= ?",
            (repository, repository, lower, now),
        ).fetchall()
        first_batch = c.execute("SELECT min(created_at) FROM batches WHERE repository=?", (repository,)).fetchone()[0]
    # Coverage starts where the ledger does, not before: a window reaching past the queue's first
    # batch would otherwise read as days on which nothing landed.
    since = lower if first_batch is None else max(lower, first_batch)
    entries = sorted(
        ([_forge(identity), number, round(first, 3), round(landed, 3)] for number, identity, landed, first in rows),
        key=lambda entry: (entry[3], entry[0], entry[1]),
    )
    return dict(
        schema=LANDINGS_SCHEMA,
        repository=repository,
        observed_at=now,
        coverage="controller_ledger",
        since=since,
        days=days,
        fields=["forge", "number", "first_approved_at", "landed_at"],
        definition=(
            "pull requests whose queue input recorded a landing in (since, observed_at]; "
            "first_approved_at is the earliest queue admission of any head of the pull request, "
            "after an all-green review; times are unix seconds"
        ),
        landings=entries,
    )

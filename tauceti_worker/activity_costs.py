"""Prepare pooled conversation costs from the authoritative local budget ledger."""

import json
from decimal import Decimal

from .activity_summary import hourly_rate
from .budget import Budget, money

COST_WINDOWS = {"total": None, "7d": 7 * 86400}
SPENDING_WINDOWS = {"3h": 10800, "24h": 86400, "7d": 7 * 86400, "total": None}


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS conversation_costs(
        id TEXT PRIMARY KEY,worker TEXT,scope TEXT,started REAL,settled REAL,cost TEXT);
      CREATE INDEX IF NOT EXISTS cost_scope ON conversation_costs(scope,settled);
      CREATE INDEX IF NOT EXISTS cost_worker ON conversation_costs(worker,settled);
    """)


def prepare(db, now, scope_for_worker):
    """Background only. Reuse Budget's validation/reconciliation, never mutate it."""
    budget = Budget(clock=lambda: now)
    if not budget.ledger.exists():
        if db.execute("SELECT 1 FROM meta WHERE key='activity_funding'").fetchone():
            raise ValueError("Budget ledger unavailable")
        return
    with budget.locked():
        raw = budget.ledger.read_bytes()
    records = budget.records(raw)
    state = budget.replay(records)
    budget.accrue(state)
    settled_at = {}
    for record in records:
        for event in record["events"]:
            if event["type"] == "settle":
                settled_at.setdefault(event["id"], record["timestamp"])
    rows = []
    for identity, session in state["sessions"].items():
        settled = session["status"] == "settled"
        value = session["cost"] if settled else session.get("reserve", session["estimate"])
        if money(value) < 0:
            raise ValueError("Negative conversation cost")
        rows.append(
            (
                identity,
                session["worker"],
                scope_for_worker(session["worker"]),
                session["started"],
                settled_at[identity] if settled else None,
                value,
            )
        )
    # This is a derived cache. A ledger repair/replacement cannot leave old sessions behind.
    db.execute("DELETE FROM conversation_costs")
    db.executemany("INSERT INTO conversation_costs VALUES (?,?,?,?,?,?)", rows)
    pending = sum(
        (money(s.get("reserve", s["estimate"])) for s in state["sessions"].values() if s["status"] != "settled"),
        Decimal(0),
    )
    funding = dict(
        balance=str(state["balance"]),
        pending=str(pending),
        available=str(state["balance"] - pending),
        grant_rate=str(state["rate"]),
        prepared_at=now,
    )
    db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_funding',?)", (json.dumps(funding),))
    db.execute("DELETE FROM meta WHERE key='activity_cost_error'")


def summarize(db, now, cutoff, column=None, identity=None):
    where, args = (f"AND {column}=?", [identity]) if column else ("", [])
    funding_row = db.execute("SELECT value FROM meta WHERE key='activity_funding'").fetchone()
    funding = json.loads(funding_row[0]) if funding_row else None
    failure = db.execute("SELECT value FROM meta WHERE key='activity_cost_error'").fetchone()
    windows = {}
    for key, seconds in COST_WINDOWS.items():
        since = max(cutoff or 0, now - seconds) if seconds is not None else cutoff or 0
        costs = db.execute(
            f"SELECT cost FROM conversation_costs WHERE settled>=? AND settled<=? {where}", [since, now, *args]
        )
        spent = sum((money(row[0]) for row in costs), Decimal(0))
        active = db.execute(
            f"SELECT cost FROM conversation_costs WHERE settled IS NULL AND started>=? AND started<=? {where}",
            [since, now, *args],
        )
        pending = sum((money(row[0]) for row in active), Decimal(0))
        available = funding and not failure
        windows[key] = dict(
            spent_usd=str(spent) if available else None,
            pending_usd=str(pending) if available else None,
            estimated_usd=str(spent + pending) if available else None,
        )
    recorded_since = cutoff
    if recorded_since is None:
        recorded_since = db.execute(f"SELECT MIN(started) FROM conversation_costs WHERE 1=1 {where}", args).fetchone()[
            0
        ]
    spending = {}
    for key, seconds in SPENDING_WINDOWS.items():
        since = (
            max(recorded_since, now - seconds) if seconds is not None and recorded_since is not None else recorded_since
        )
        covered = max(0, now - since) if since is not None else 0
        costs = db.execute(
            f"SELECT cost FROM conversation_costs WHERE settled>=? AND settled<=? {where}",
            [since if since is not None else now, now, *args],
        )
        spent = str(sum((money(row[0]) for row in costs), Decimal(0))) if funding and not failure else None
        # Existing native round intervals record work, rather than the lifetime
        # of wrappers that may be waiting for capacity, budget or GitHub.
        begin = since if since is not None else now
        rounds = db.execute(
            f"SELECT started,finished FROM rounds WHERE started<=? AND (finished>=? OR finished IS NULL) AND COALESCE(outcome,'') NOT IN ('waiting','interrupted-unfinished') {where}",
            [now, begin, *args],
        )
        worker_seconds = sum(
            max(0, min(finished if finished is not None else now, now) - max(started, begin))
            for started, finished in rounds
        )
        spending[key] = dict(
            spent_usd=spent,
            covered_seconds=covered,
            worker_seconds=worker_seconds,
            usd_per_hour=hourly_rate(spent, covered),
        )
    return dict(
        windows=windows, spending=spending, funding=funding, prepared_at=funding.get("prepared_at") if funding else None
    )

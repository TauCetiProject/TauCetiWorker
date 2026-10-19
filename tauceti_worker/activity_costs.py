"""Prepare pooled conversation costs from the authoritative local budget ledger."""

import json
from decimal import Decimal

from .activity_summary import hourly_rate
from .budget import Budget, money
from .usage_accounting import read_usage

COST_WINDOWS = {"total": None, "7d": 7 * 86400}
SPENDING_WINDOWS = {"3h": 10800, "24h": 86400, "7d": 7 * 86400, "total": None}


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS conversation_costs(
        id TEXT PRIMARY KEY,worker TEXT,scope TEXT,started REAL,settled REAL,cost TEXT);
      CREATE TABLE IF NOT EXISTS cost_accruals(
        id TEXT,worker TEXT,scope TEXT,started REAL,finished REAL,cost TEXT,quality TEXT,active INTEGER);
      CREATE INDEX IF NOT EXISTS accrual_scope ON cost_accruals(scope,started);
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
    rows, accruals, seen = [], [], set()
    quality = dict(native=0, estimated=0, incomplete=0)
    for identity, session in sorted(state["sessions"].items(), key=lambda pair: pair[1]["started"]):
        settled = session["status"] == "settled"
        value = session.get("cost", "0")
        saved = (
            read_usage(budget.directory / "usage" / (identity + ".json"))
            if len(identity) == 32 and all(c in "0123456789abcdef" for c in identity)
            else None
        )
        items = []
        if saved:
            for item in saved["messages"].values():
                key = (item["session"], item["message_id"])
                if key not in seen:
                    items.append(item)
                    seen.add(key)
        priced = [item for item in items if item["cost"] is not None]
        total = sum((money(item["cost"]) for item in priced), Decimal(0))
        source = session.get("cost_provenance", "native-reported")
        native = settled and source == "native-reported"
        if not settled:
            value = str(max(money(value), total, money(session.get("lower_bound_cost", "0"))))
        scope = scope_for_worker(session["worker"])
        quality["native" if native else "estimated"] += 1
        if not settled and (
            not saved or saved["inflight"] or any(not item["complete"] or item["cost"] is None for item in items)
        ):
            quality["incomplete"] += 1
        if total > 0:
            # A native final total corrects all response costs proportionally,
            # retaining their timestamps rather than creating a settlement spike.
            allocated = Decimal(0)
            for index, item in enumerate(priced):
                part = (
                    money(value) - allocated if index == len(priced) - 1 else money(value) * money(item["cost"]) / total
                )
                allocated += part
                accruals.append(
                    (
                        identity,
                        session["worker"],
                        scope,
                        item["at"],
                        item["at"],
                        str(part),
                        "native-allocated" if native else "token-priced",
                        not settled,
                    )
                )
        elif money(value) > 0:
            finish = settled_at.get(identity, saved["observed_at"] if saved else now)
            accruals.append(
                (
                    identity,
                    session["worker"],
                    scope,
                    session["started"],
                    max(session["started"], finish),
                    value,
                    "interval-estimate",
                    not settled,
                )
            )
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
    db.execute("DELETE FROM cost_accruals")
    db.executemany("INSERT INTO cost_accruals VALUES (?,?,?,?,?,?,?,?)", accruals)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_cost_quality',?)", (json.dumps(quality),))
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

    def accrued(since, active=None):
        query = f"SELECT started,finished,cost FROM cost_accruals WHERE finished>=? AND started<=? {where}"
        values = [since, now, *args]
        if active is not None:
            query += " AND active=?"
            values.append(active)
        total = Decimal(0)
        for start, finish, cost in db.execute(query, values):
            if start == finish:
                if since <= start <= now:
                    total += money(cost)
            else:
                overlap = max(0, min(finish, now) - max(start, since))
                total += money(cost) * money(overlap) / money(finish - start)
        return total

    windows = {}
    for key, seconds in COST_WINDOWS.items():
        since = max(cutoff or 0, now - seconds) if seconds is not None else cutoff or 0
        available = funding and not failure
        completed, active = accrued(since, 0), accrued(since, 1)
        windows[key] = dict(
            spent_usd=str(completed) if available else None,
            active_accrued_usd=str(active) if available else None,
            estimated_usd=str(completed + active) if available else None,
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
        spent = str(accrued(since if since is not None else now)) if funding and not failure else None
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
            usd_per_worker_hour=hourly_rate(spent, worker_seconds),
        )
    return dict(
        accounting_version=2,
        windows=windows,
        spending=spending,
        funding=funding,
        quality=json.loads(quality[0])
        if (quality := db.execute("SELECT value FROM meta WHERE key='activity_cost_quality'").fetchone())
        else {},
        prepared_at=funding.get("prepared_at") if funding else None,
    )

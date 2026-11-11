"""Prepare pooled conversation costs from the authoritative local budget ledger."""

import json
import time
from decimal import Decimal

from . import activity_rollup as rollup
from .activity_summary import COST_WINDOWS as COST_WINDOW_NAMES
from .activity_summary import WINDOW_SECONDS, hourly_rate
from .budget import money
from .usage_accounting import session_usage, usage_path

COST_WINDOWS = {key: WINDOW_SECONDS[key] for key in COST_WINDOW_NAMES}
SPENDING_WINDOWS = WINDOW_SECONDS


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS conversation_costs(
        id TEXT PRIMARY KEY,worker TEXT,scope TEXT,started REAL,settled REAL,cost TEXT);
      CREATE TABLE IF NOT EXISTS prepared_usage(
        id TEXT PRIMARY KEY,signature TEXT,payload TEXT);
      CREATE TABLE IF NOT EXISTS conversation_quality(
        id TEXT PRIMARY KEY,worker TEXT,scope TEXT,native INTEGER,estimated INTEGER,incomplete INTEGER);
      CREATE TABLE IF NOT EXISTS conversation_accounting(
        id TEXT PRIMARY KEY,worker TEXT,scope TEXT,live INTEGER,settled_estimates INTEGER,
        recovered_estimates INTEGER,unresolved_stopped INTEGER);
      CREATE TABLE IF NOT EXISTS cost_accruals(
        id TEXT,worker TEXT,scope TEXT,started REAL,finished REAL,cost TEXT,quality TEXT,active INTEGER);
      DROP INDEX IF EXISTS accrual_scope_covering;
      DROP INDEX IF EXISTS accrual_scope;
      DROP INDEX IF EXISTS accrual_worker_covering;
      DROP INDEX IF EXISTS accrual_worker;
      DROP INDEX IF EXISTS cost_scope;
      DROP INDEX IF EXISTS cost_worker;
      DROP INDEX IF EXISTS quality_worker;
      DROP INDEX IF EXISTS accounting_worker;
      DROP INDEX IF EXISTS accounting_estimates;
      CREATE INDEX IF NOT EXISTS accrual_invocation ON cost_accruals(id);
      CREATE INDEX IF NOT EXISTS cost_first ON conversation_costs(started);
      CREATE INDEX IF NOT EXISTS cost_worker_first ON conversation_costs(worker,started);
      CREATE INDEX IF NOT EXISTS cost_scope_first ON conversation_costs(scope,started);
      CREATE INDEX IF NOT EXISTS accounting_live ON conversation_accounting(id)
        WHERE live=1 OR unresolved_stopped=1;
      CREATE TABLE IF NOT EXISTS cost_sessions(id TEXT PRIMARY KEY,worker TEXT,scope TEXT,started REAL,ordinal INTEGER,data TEXT,moving INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS cost_session_order ON cost_sessions(started,ordinal);
      DROP INDEX IF EXISTS cost_session_worker;
      CREATE TABLE IF NOT EXISTS cost_messages(provider TEXT,message TEXT,invocation TEXT,started REAL,ordinal INTEGER,payload TEXT,
        PRIMARY KEY(provider,message,invocation));
      CREATE INDEX IF NOT EXISTS cost_message_owner ON cost_messages(provider,message,started,ordinal,invocation);
      CREATE INDEX IF NOT EXISTS cost_message_invocation ON cost_messages(invocation);
      DROP TABLE IF EXISTS cost_worker_scopes;
      CREATE TABLE IF NOT EXISTS cost_calibration(cohort TEXT,invocation TEXT,started REAL,ordinal INTEGER,payload TEXT,
        PRIMARY KEY(cohort,invocation));
      CREATE INDEX IF NOT EXISTS cost_calibration_recent ON cost_calibration(cohort,started DESC,ordinal DESC);
      CREATE INDEX IF NOT EXISTS cost_calibration_invocation ON cost_calibration(invocation);
      CREATE TABLE IF NOT EXISTS cost_watch(id TEXT PRIMARY KEY,deadline REAL);
      CREATE INDEX IF NOT EXISTS cost_watch_deadline ON cost_watch(deadline);
      CREATE TABLE IF NOT EXISTS cost_failed(id TEXT PRIMARY KEY,next_at REAL,attempts INTEGER,error TEXT);
      CREATE INDEX IF NOT EXISTS cost_failed_next ON cost_failed(next_at);
    """)
    rollup.install(db)
    for table, source in (
        ("cost_accruals", "cost"),
        ("conversation_accounting", "accounting"),
        ("conversation_quality", "quality"),
    ):
        rollup.watch(db, table, source, ["id"])


def usage_signature(budget, session, metrics):
    path = usage_path(budget.directory, session["id"], session.get("bridge"))
    paths = {path, budget.directory / "usage" / (session["id"] + ".json")}
    fingerprints = []
    for candidate in sorted(paths):
        for file in (candidate, candidate.with_suffix(".jsonl")):
            metrics["usage_metadata_checks"] += 1
            try:
                stat = file.lstat()
                fingerprints.append((str(file), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_uid, stat.st_mode))
            except OSError as error:
                fingerprints.append((str(file), None, error.errno))
    return json.dumps(fingerprints)


def cached_usage(db, budget, session, metrics, *, signature=None):
    """Stat active checkpoints; closed native evidence changes through ledger events."""
    saved = db.execute("SELECT signature,payload FROM prepared_usage WHERE id=?", (session["id"],)).fetchone()
    if (
        saved
        and session["status"] == "settled"
        and session.get("cost_provenance", "native-reported") == "native-reported"
    ):
        value = json.loads(saved[1])
        if value and value["closed"]:
            return value
    signature = usage_signature(budget, session, metrics) if signature is None else signature
    if saved and saved[0] == signature:
        return json.loads(saved[1])
    started = time.monotonic()
    value = session_usage(budget.directory, session)
    metrics["usage_decodes"] += 1
    metrics["usage_decode_seconds"] += time.monotonic() - started
    if value:
        value = {
            **value,
            "messages": {
                key: {name: item[name] for name in ("session", "message_id", "at", "cost", "complete")}
                for key, item in value["messages"].items()
            },
        }
    db.execute("INSERT OR REPLACE INTO prepared_usage VALUES (?,?,?)", (session["id"], signature, json.dumps(value)))
    return value


def prepare(db, now, scope_for_worker):
    from .activity_cost_prepare import prepare as incremental_prepare

    # The caller may retain other activity facts after a cost error. Preserve
    # the entire old cost generation, including ownership and replay cursor.
    if not db.in_transaction:
        db.execute("BEGIN")
    db.execute("SAVEPOINT prepare_costs")
    try:
        result = incremental_prepare(db, now, scope_for_worker)
    except BaseException:
        db.execute("ROLLBACK TO prepare_costs")
        db.execute("RELEASE prepare_costs")
        raise
    db.execute("RELEASE prepare_costs")
    return result


def summarize(db, now, cutoff, column=None, identity=None):
    from .activity_rollup_sources import flush

    flush(db, now)
    where, args = (f"AND {column}=?", [identity]) if column else ("", [])
    funding_row = db.execute("SELECT value FROM meta WHERE key='activity_funding'").fetchone()
    funding = json.loads(funding_row[0]) if funding_row else None
    failure = db.execute("SELECT value FROM meta WHERE key='activity_cost_error'").fetchone()
    available = funding and not failure
    upper, totals = rollup.snapshot(db, now, SPENDING_WINDOWS, cutoff, column, identity)

    def value(values, key):
        amount = values.get(key, Decimal(0))
        return Decimal(0) if abs(amount) < Decimal("1e-45") else +amount

    windows = {}
    for key in COST_WINDOWS:
        row = totals[key]
        completed, active, stopped = (value(row, name) for name in ("completed", "active", "stopped"))
        windows[key] = dict(
            spent_usd=str(completed) if available else None,
            active_accrued_usd=str(active) if available else None,
            unresolved_stopped_usd=str(stopped) if available else None,
            settled_estimates_usd=str(value(row, "settled_estimates")) if available else None,
            recovered_estimates_usd=str(value(row, "recovered_estimates")) if available else None,
            estimated_usd=str(completed + active + stopped) if available else None,
        )
    recorded_since = cutoff
    if recorded_since is None:
        recorded_since = db.execute(f"SELECT MIN(started) FROM conversation_costs WHERE 1=1 {where}", args).fetchone()[
            0
        ]
    _, spending_totals = rollup.snapshot(db, now, SPENDING_WINDOWS, recorded_since, column, identity)
    spending = {}
    for key, seconds in SPENDING_WINDOWS.items():
        since = (
            max(recorded_since, now - seconds) if seconds is not None and recorded_since is not None else recorded_since
        )
        covered = max(0, now - since) if since is not None else 0
        row = spending_totals[key]
        spent = (
            str(sum((value(row, name) for name in ("completed", "active", "stopped")), Decimal(0)))
            if available
            else None
        )
        worker_seconds = float(value(row, "worker_seconds"))
        spending[key] = dict(
            spent_usd=spent,
            covered_seconds=covered,
            worker_seconds=worker_seconds,
            usd_per_hour=hourly_rate(spent, covered),
            usd_per_worker_hour=hourly_rate(spent, worker_seconds),
        )
    baseline = {}
    for row in db.execute(
        "SELECT c.id,c.worker,c.scope,c.started,c.cost,p.payload FROM conversation_accounting a INDEXED BY accounting_live "
        "CROSS JOIN conversation_costs c ON a.id=c.id LEFT JOIN prepared_usage p ON p.id=c.id "
        f"WHERE (a.live=1 OR a.unresolved_stopped=1) {('AND c.' + column + '=?') if column else ''}",
        args,
    ):
        saved = json.loads(row[5]) if row[5] else None
        baseline[row[0]] = dict(
            worker=row[1],
            scope=row[2],
            started=row[3],
            cost=row[4],
            observed_at=saved.get("observed_at") if saved else row[3],
        )
        if saved and saved.get("journal_format") and saved.get("journal_identity"):
            observed = sum(
                (money(m["cost"]) for m in saved["messages"].values() if m.get("cost") is not None), Decimal(0)
            )
            baseline[row[0]]["summary"] = {
                **{
                    key: saved[key]
                    for key in (
                        "id",
                        "model",
                        "sequence",
                        "observed_at",
                        "journal_format",
                        "journal_identity",
                        "journal_offset",
                        "journal_start_bytes",
                        "prices_sha256",
                    )
                },
                "cost": str(observed),
                "usage_seen": observed > 0,
            }
        baseline[row[0]]["windows"] = {key: "0" for key in SPENDING_WINDOWS}
        for start, finish, cost in db.execute("SELECT started,finished,cost FROM cost_accruals WHERE id=?", (row[0],)):
            if start is None or finish is None:
                continue
            for key, seconds in SPENDING_WINDOWS.items():
                since = (
                    max(recorded_since, now - seconds)
                    if seconds is not None and recorded_since is not None
                    else now - seconds
                    if seconds is not None
                    else recorded_since or 0
                )
                part = (
                    (money(cost) if since <= start <= now else Decimal(0))
                    if start == finish
                    else (money(cost) * money(max(0, min(finish, now) - max(start, since))) / money(finish - start))
                )
                baseline[row[0]]["windows"][key] = str(money(baseline[row[0]]["windows"][key]) + part)
    return dict(
        accounting_version=3,
        windows=windows,
        spending=spending,
        funding=funding,
        quality={key: int(upper.get("quality:" + key, 0)) for key in ("native", "estimated", "incomplete")},
        accounting={
            key: int(upper.get("accounting:" + key, 0))
            for key in ("live", "settled_estimates", "recovered_estimates", "unresolved_stopped")
        },
        prepared_at=funding.get("prepared_at") if funding else None,
        live_baseline={
            **{key: funding.get(key) for key in ("generation", "ledger_sequence", "ledger_identity")},
            "sessions": baseline,
        }
        if funding and funding.get("generation")
        else None,
        live_rates=funding.get("live_rates", {}) if funding else {},
        preparation_metrics=funding.get("preparation_metrics", {}) if funding else {},
    )

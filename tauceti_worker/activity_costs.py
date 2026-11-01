"""Prepare pooled conversation costs from the authoritative local budget ledger."""

import json
import sys
import time
import uuid
from decimal import Decimal

from .activity_summary import COST_WINDOWS as COST_WINDOW_NAMES
from .activity_summary import WINDOW_SECONDS, hourly_rate
from .budget import Budget, money
from .budget_recovery import interval, live_identity
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
      CREATE INDEX IF NOT EXISTS accrual_scope_covering ON cost_accruals(scope,started,id,finished,cost,active);
      DROP INDEX IF EXISTS accrual_scope;
      CREATE INDEX IF NOT EXISTS accrual_worker_covering ON cost_accruals(worker,started,id,finished,cost,active);
      DROP INDEX IF EXISTS accrual_worker;
      CREATE INDEX IF NOT EXISTS cost_scope ON conversation_costs(scope,settled);
      CREATE INDEX IF NOT EXISTS cost_worker ON conversation_costs(worker,settled);
      CREATE INDEX IF NOT EXISTS quality_worker ON conversation_quality(worker,native,estimated,incomplete);
      CREATE INDEX IF NOT EXISTS accounting_worker ON conversation_accounting(worker,live,settled_estimates,recovered_estimates,unresolved_stopped);
      -- Match the estimate-marker predicate in summarize().
      CREATE INDEX IF NOT EXISTS accounting_estimates ON conversation_accounting(id,settled_estimates,recovered_estimates)
        WHERE id IS NOT NULL AND (settled_estimates=1 OR recovered_estimates=1);
    """)


def cached_usage(db, budget, session, metrics):
    """Stat small checkpoints; immutable old usage is reduced and cached in SQLite."""
    saved = db.execute("SELECT signature,payload FROM prepared_usage WHERE id=?", (session["id"],)).fetchone()
    if (
        saved
        and session["status"] == "settled"
        and session.get("cost_provenance", "native-reported") == "native-reported"
    ):
        value = json.loads(saved[1])
        if value and value["closed"]:
            return value  # Final native evidence is immutable; late cost corrections are ledger events.
    path = usage_path(budget.directory, session["id"], session.get("bridge"))
    paths = {path, budget.directory / "usage" / (session["id"] + ".json")}
    fingerprints = []
    for candidate in sorted(paths):
        for file in (candidate, candidate.with_suffix(".jsonl")):
            metrics["usage_metadata_checks"] += 1
            try:
                stat = file.lstat()
                fingerprints.append((str(file), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_uid, stat.st_mode))
            except FileNotFoundError:
                fingerprints.append((str(file), None))
    signature = json.dumps(fingerprints)
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


def _write_prepared(db, table, rows):
    """Reconcile fully recomputed rows without rewriting unchanged SQLite pages."""
    fields = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    # Accruals have multiple rows per invocation. Keep their recomputed order,
    # including duplicate rows, exactly as the previous delete-and-insert did.
    if table == "cost_accruals":
        key = "rowid"
        # Separate rowid ranges avoid a full scan for an unchanged population.
        db.execute(f"DELETE FROM {table} WHERE rowid<1")
        db.execute(f"DELETE FROM {table} WHERE rowid>?", (len(rows),))
        columns = [key, *fields]
        changed = []
        # Stream stored rows in position order, rather than retaining a second
        # complete copy or probing the table once for every computed row.
        stored = db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
        previous = next(stored, None)
        for index, row in enumerate(rows, 1):
            if previous is None or previous[0] != index:
                changed.append(index)
            else:
                if tuple(previous)[1:] != tuple(row):
                    changed.append(index)
                previous = next(stored, None)
        stored.close()
        values = ((index, *rows[index - 1]) for index in changed)
    else:
        key = "id"
        # Index references to computed rows, not copies of every stored value.
        # As with the UPSERT, the last desired row wins for a repeated identity.
        latest = {row[0]: row for row in rows}
        obsolete = set()
        for row in db.execute(f"SELECT * FROM {table}"):
            if row[0] not in latest:
                obsolete.add(row[0])
            elif row[0] is not None and tuple(row) == tuple(latest[row[0]]):
                latest[row[0]] = None
        db.executemany(f"DELETE FROM {table} WHERE id IS ?", ((identity,) for identity in obsolete))
        columns = fields
        values = (row for row in rows if row[0] is None or latest[row[0]] is row)
    names = ",".join(fields)
    excluded = ",".join(f"excluded.{field}" for field in fields)
    updates = ",".join(f"{field}=excluded.{field}" for field in fields)
    db.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates} WHERE ({names}) IS NOT ({excluded})",
        values,
    )


def prepare(db, now, scope_for_worker):
    """Background only. Reuse Budget's validation/reconciliation, never mutate it."""
    started, cpu = time.monotonic(), time.process_time()
    metrics = dict(usage_metadata_checks=0, usage_decodes=0, usage_decode_seconds=0)
    budget = Budget(clock=lambda: now)
    if not budget.ledger.exists():
        if db.execute("SELECT 1 FROM meta WHERE key='activity_funding'").fetchone():
            raise ValueError("Budget ledger unavailable")
        return
    from .activity_ledger import load

    state, settled_at, replay_metrics = load(db, budget)
    info = budget.ledger.stat()
    metrics.update(replay_metrics, ledger_replay_seconds=time.monotonic() - started)
    if replay_metrics["rebuilt"]:
        db.execute("DELETE FROM prepared_usage")
    phase = time.monotonic()
    budget.accrue(state)
    rows, accruals, quality_rows, accounting_rows, seen = [], [], [], [], set()
    scopes, calibration = {}, []
    for identity, session in sorted(state["sessions"].items(), key=lambda pair: pair[1]["started"]):
        settled = session["status"] == "settled"
        value = session.get("cost", "0")
        saved = (
            cached_usage(db, budget, session, metrics)
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
        if (
            native
            and saved
            and saved.get("closed")
            and not saved.get("inflight")
            and not session.get("resume")
            and session.get("outcome") not in {"launch-failed", "reconciled"}
            and money(value) > 0
            and saved.get("journal_format")
            and saved.get("journal_offset", 0) > saved.get("journal_start_bytes", 0)
            and saved.get("messages")
            and all(m["complete"] and m["cost"] is not None for m in saved["messages"].values())
        ):
            calibration.append(
                dict(
                    model=session["model"],
                    phase=session["phase"],
                    cost=value,
                    journal_format=saved["journal_format"],
                    prices_sha256=saved.get("prices_sha256"),
                    bytes=saved["journal_offset"] - saved["journal_start_bytes"],
                )
            )
        if not settled:
            value = str(max(money(value), total, money(session.get("lower_bound_cost", "0"))))
        worker = session["worker"]
        if worker not in scopes:
            scopes[worker] = scope_for_worker(worker)
        scope = scopes[worker]
        incomplete = not settled and (
            not saved
            or not saved["closed"]
            or saved["inflight"]
            or any(not item["complete"] or item["cost"] is None for item in items)
        )
        quality_rows.append((identity, session["worker"], scope, native, not native, incomplete))
        live = not settled and live_identity(session, now, budget.directory)["live"]
        accounting_rows.append(
            (
                identity,
                session["worker"],
                scope,
                live,
                settled and not native,
                settled and bool(session.get("recovery_audit")) and not native,
                not settled and not live,
            )
        )
        active_kind = 0 if settled else 1 if live else 2
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
                        active_kind,
                    )
                )
        elif money(value) > 0:
            recorded = interval(session, now)
            finish = (
                float(recorded["finish"])
                if recorded["finish"] is not None
                else session["started"]
                if settled
                else saved["observed_at"]
                if saved
                else now
            )
            accruals.append(
                (
                    identity,
                    session["worker"],
                    scope,
                    session["started"],
                    max(session["started"], finish),
                    value,
                    "interval-estimate",
                    active_kind,
                )
            )
        if money(value) < 0:
            raise ValueError("Negative conversation cost")
        rows.append(
            (
                identity,
                session["worker"],
                scope,
                session["started"],
                settled_at[identity] if settled else None,
                value,
            )
        )
    metrics["usage_and_allocation_seconds"] = time.monotonic() - phase
    phase = time.monotonic()
    # This is a derived cache. A ledger repair/replacement cannot leave old sessions behind.
    for table, prepared in (
        ("conversation_costs", rows),
        ("cost_accruals", accruals),
        ("conversation_accounting", accounting_rows),
        ("conversation_quality", quality_rows),
    ):
        _write_prepared(db, table, prepared)
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
        ledger_sequence=state["seq"],
        ledger_identity=[info.st_dev, info.st_ino],
        generation=uuid.uuid4().hex,
    )
    from .activity_live import calibrate

    funding["live_rates"] = calibrate(calibration)
    metrics.update(
        table_write_seconds=time.monotonic() - phase,
        seconds=time.monotonic() - started,
        cpu_seconds=time.process_time() - cpu,
        ledger_bytes=info.st_size,
        invocations=len(rows),
    )
    try:
        import resource

        metrics["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )
    except ImportError:
        pass
    funding["preparation_metrics"] = metrics
    db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_funding',?)", (json.dumps(funding),))
    db.execute("DELETE FROM meta WHERE key='activity_cost_error'")


def summarize(db, now, cutoff, column=None, identity=None):
    where, args = (f"AND {column}=?", [identity]) if column else ("", [])
    funding_row = db.execute("SELECT value FROM meta WHERE key='activity_funding'").fetchone()
    funding = json.loads(funding_row[0]) if funding_row else None
    failure = db.execute("SELECT value FROM meta WHERE key='activity_cost_error'").fetchone()
    # Every window summarizes the same immutable prepared rows. Read them once
    # per summary instead of repeatedly walking SQLite and its page cache.
    # Preserve the previous rowid/table or (identity,started,rowid) index order,
    # including timestamp ties, so query-plan changes cannot reorder Decimal sums.
    ordering = "started,rowid" if column else "rowid"
    accruals = db.execute(
        f"SELECT id,started,finished,cost,active FROM cost_accruals WHERE started<=? AND finished IS NOT NULL {where} ORDER BY {ordering}",
        [now, *args],
    ).fetchall()
    estimates = {"settled_estimates": set(), "recovered_estimates": set()}
    # Keep this predicate aligned with accounting_estimates in install().
    for invocation, settled, recovered in db.execute(
        "SELECT id,settled_estimates,recovered_estimates FROM conversation_accounting WHERE id IS NOT NULL AND (settled_estimates=1 OR recovered_estimates=1)"
    ):
        if settled == 1:
            estimates["settled_estimates"].add(invocation)
        if recovered == 1:
            estimates["recovered_estimates"].add(invocation)

    def accrued(since, active=None, accounting_kind=None):
        total = Decimal(0)
        for invocation, start, finish, cost, kind in accruals:
            if active is not None and kind != active:
                continue
            if accounting_kind and invocation not in estimates[accounting_kind]:
                continue
            # Preserve Decimal string precision by excluding older rows entirely.
            if finish < since:
                continue
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
        completed, active, stopped = accrued(since, 0), accrued(since, 1), accrued(since, 2)
        windows[key] = dict(
            spent_usd=str(completed) if available else None,
            active_accrued_usd=str(active) if available else None,
            unresolved_stopped_usd=str(stopped) if available else None,
            settled_estimates_usd=str(accrued(since, 0, "settled_estimates")) if available else None,
            recovered_estimates_usd=str(accrued(since, 0, "recovered_estimates")) if available else None,
            estimated_usd=str(completed + active + stopped) if available else None,
        )
    recorded_since = cutoff
    if recorded_since is None:
        recorded_since = db.execute(f"SELECT MIN(started) FROM conversation_costs WHERE 1=1 {where}", args).fetchone()[
            0
        ]
    spending = {}
    begin = recorded_since if recorded_since is not None else now
    rounds = db.execute(
        f"SELECT started,finished FROM rounds WHERE started<=? AND (finished>=? OR finished IS NULL) AND COALESCE(outcome,'') NOT IN ('waiting','interrupted-unfinished') {where}",
        [now, begin, *args],
    ).fetchall()
    for key, seconds in SPENDING_WINDOWS.items():
        since = (
            max(recorded_since, now - seconds) if seconds is not None and recorded_since is not None else recorded_since
        )
        covered = max(0, now - since) if since is not None else 0
        spent = str(accrued(since if since is not None else now)) if funding and not failure else None
        # Existing native round intervals record work, rather than the lifetime
        # of wrappers that may be waiting for capacity, budget or GitHub.
        begin = since if since is not None else now
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
    baseline = {}
    for row in db.execute(
        f"SELECT c.id,c.worker,c.scope,c.started,c.cost,p.payload FROM conversation_costs c "
        "JOIN conversation_accounting a ON a.id=c.id LEFT JOIN prepared_usage p ON p.id=c.id "
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
            cost = sum((money(m["cost"]) for m in saved["messages"].values() if m.get("cost") is not None), Decimal(0))
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
                "cost": str(cost),
                "usage_seen": cost > 0,
            }
    # Downward native corrections must remove spend where the prepared report
    # actually allocated it, not create a negative cost in the latest window.
    for row in baseline.values():
        row["windows"] = {key: "0" for key in SPENDING_WINDOWS}
    for invocation, start, finish, cost, _kind in accruals:
        if invocation not in baseline:
            continue
        for key, seconds in SPENDING_WINDOWS.items():
            since = (
                max(recorded_since, now - seconds)
                if seconds is not None and recorded_since is not None
                else now - seconds
                if seconds is not None
                else recorded_since or 0
            )
            if start == finish:
                part = money(cost) if since <= start <= now else Decimal(0)
            else:
                part = money(cost) * money(max(0, min(finish, now) - max(start, since))) / money(finish - start)
            baseline[invocation]["windows"][key] = str(money(baseline[invocation]["windows"][key]) + part)
    return dict(
        accounting_version=3,
        windows=windows,
        spending=spending,
        funding=funding,
        quality=dict(
            zip(
                ("native", "estimated", "incomplete"),
                db.execute(
                    f"SELECT COALESCE(SUM(native),0),COALESCE(SUM(estimated),0),COALESCE(SUM(incomplete),0) FROM conversation_quality WHERE 1=1 {where}",
                    args,
                ).fetchone(),
                strict=True,
            )
        ),
        accounting=dict(
            zip(
                ("live", "settled_estimates", "recovered_estimates", "unresolved_stopped"),
                db.execute(
                    f"SELECT COALESCE(SUM(live),0),COALESCE(SUM(settled_estimates),0),COALESCE(SUM(recovered_estimates),0),COALESCE(SUM(unresolved_stopped),0) FROM conversation_accounting WHERE 1=1 {where}",
                    args,
                ).fetchone(),
                strict=True,
            )
        ),
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

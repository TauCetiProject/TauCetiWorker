"""Changed-fact adapters for the activity report's bounded moving totals."""

import json
from decimal import Decimal, localcontext

from . import activity_rollup as r

MEANINGFUL = {"review", "fix", "fix-ci", "rebase", "bump", "lint-repair", "branch_push"}
FIXES = {"fix", "fix-ci", "rebase", "bump", "lint-repair"}


def install(db):
    r.install(db)
    for table, source, key, fields in (
        ("rounds", "round", ["id"], None),
        ("prs", "pr", ["repo", "number"], ["worker", "scope", "created", "state", "landed", "head"]),
        ("pr_loc", "pr", ["repo", "number"], ["head", "added"]),
        ("pr_contributions", "contribution", ["repo", "number"], None),
        ("ci_activity", "ci", ["id"], None),
    ):
        # Different tables may invalidate the same source, but each table must
        # seed existing keys once when upgrading an old reporting database.
        marker = source + ":" + table
        r.watch(db, table, marker, key, fields=fields)
    for table in ("rounds", "prs", "pr_contributions", "ci_activity"):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        fields = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for field in (
            ("started", "COALESCE(started,finished)")
            if table == "rounds"
            else ("created", "observed", "checked")
            if table == "prs"
            else ("at",)
        ):
            suffix = "first" if field.startswith("COALESCE") else field
            for column in (None, "worker", "scope"):
                if column and column not in fields:
                    continue
                name = "report_" + table + "_" + (column or "all") + "_" + suffix
                columns = (column + "," if column else "") + field
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    db.executescript("""
      CREATE INDEX IF NOT EXISTS report_live_rounds ON rounds(worker,scope) WHERE finished IS NULL AND outcome IS NULL;
      CREATE INDEX IF NOT EXISTS report_contribution_pr ON pr_contributions(repo,number,at,id);
      CREATE INDEX IF NOT EXISTS report_unresolved_pr ON prs(observed,created) WHERE landed IS NULL AND state IS NOT 'CLOSED';
      CREATE INDEX IF NOT EXISTS report_unresolved_worker_pr ON prs(worker,observed,created) WHERE landed IS NULL AND state IS NOT 'CLOSED';
      CREATE INDEX IF NOT EXISTS report_unresolved_scope_pr ON prs(scope,observed,created) WHERE landed IS NULL AND state IS NOT 'CLOSED';
      CREATE INDEX IF NOT EXISTS report_missing_pr ON prs(created) WHERE landed IS NULL AND state IS NOT 'CLOSED' AND observed IS NULL;
      CREATE INDEX IF NOT EXISTS report_missing_worker_pr ON prs(worker,created) WHERE landed IS NULL AND state IS NOT 'CLOSED' AND observed IS NULL;
      CREATE INDEX IF NOT EXISTS report_missing_scope_pr ON prs(scope,created) WHERE landed IS NULL AND state IS NOT 'CLOSED' AND observed IS NULL;
      CREATE INDEX IF NOT EXISTS report_interrupted_worker ON rounds(worker,started DESC,id DESC) WHERE outcome='interrupted-unfinished';
      CREATE INDEX IF NOT EXISTS report_interrupted_scope ON rounds(scope,started DESC,id DESC) WHERE outcome='interrupted-unfinished';
      CREATE TABLE IF NOT EXISTS report_pr_refresh(repo TEXT,number INTEGER,worker TEXT,next_at REAL,PRIMARY KEY(repo,number));
      CREATE INDEX IF NOT EXISTS report_pr_next ON report_pr_refresh(next_at) WHERE next_at IS NOT NULL;
      CREATE TRIGGER IF NOT EXISTS report_pr_track AFTER INSERT ON prs BEGIN
        INSERT INTO report_pr_refresh SELECT NEW.repo,NEW.number,NEW.worker,0
          WHERE NOT EXISTS(SELECT 1 FROM report_pr_refresh WHERE repo=NEW.repo AND number=NEW.number);
        UPDATE report_pr_refresh SET worker=NEW.worker,next_at=0 WHERE repo=NEW.repo AND number=NEW.number;
      END;
      CREATE TRIGGER IF NOT EXISTS report_pr_recheck AFTER UPDATE ON prs
        WHEN NEW.head IS NOT OLD.head OR NEW.state IS NOT OLD.state OR NEW.landed IS NOT OLD.landed OR NEW.worker IS NOT OLD.worker BEGIN
        UPDATE report_pr_refresh SET worker=NEW.worker,next_at=0 WHERE repo=NEW.repo AND number=NEW.number;
      END;
      CREATE TRIGGER IF NOT EXISTS report_pr_reset AFTER UPDATE OF checked ON prs
        WHEN NEW.checked<OLD.checked OR (NEW.checked IS NULL AND OLD.checked IS NOT NULL) BEGIN
        UPDATE report_pr_refresh SET next_at=0 WHERE repo=NEW.repo AND number=NEW.number;
      END;
      CREATE TRIGGER IF NOT EXISTS report_observation_reset AFTER UPDATE OF checked ON pr_observations
        WHEN NEW.checked<OLD.checked OR (NEW.checked IS NULL AND OLD.checked IS NOT NULL) BEGIN
        UPDATE report_pr_refresh SET next_at=0 WHERE repo=NEW.repo AND number=NEW.number;
      END;
      CREATE TRIGGER IF NOT EXISTS report_contribution_track AFTER INSERT ON pr_contributions BEGIN
        INSERT INTO report_pr_refresh SELECT NEW.repo,NEW.number,NEW.worker,0
          WHERE NOT EXISTS(SELECT 1 FROM report_pr_refresh WHERE repo=NEW.repo AND number=NEW.number);
        UPDATE report_pr_refresh SET next_at=0 WHERE repo=NEW.repo AND number=NEW.number;
      END;
    """)
    if db.execute("INSERT OR IGNORE INTO report_sources VALUES('pr-refresh-registry')").rowcount:
        db.execute("INSERT OR IGNORE INTO report_pr_refresh SELECT repo,number,worker,0 FROM prs")
        db.execute(
            "INSERT OR IGNORE INTO report_pr_refresh SELECT repo,number,worker,0 FROM pr_contributions GROUP BY repo,number HAVING rowid=MAX(rowid)"
        )


def invalidate(db, source, key):
    db.execute("INSERT OR IGNORE INTO report_dirty VALUES(?,?)", (source, key))


def cost(db, key, now):
    identity = json.loads(key)[0]
    flags = db.execute(
        "SELECT settled_estimates,recovered_estimates FROM conversation_accounting WHERE id=?", (identity,)
    ).fetchone()
    rows = []
    for row in db.execute("SELECT * FROM cost_accruals WHERE id IS ? ORDER BY rowid", (identity,)):
        invocation, worker, scope, start, finish, amount, quality, active = row
        if start is None or finish is None:
            continue
        metrics = {("completed", "active", "stopped")[active]: Decimal(amount)}
        if identity is not None and active == 0 and flags:
            if flags[0] == 1:
                metrics["settled_estimates"] = Decimal(amount)
            if flags[1] == 1:
                metrics["recovered_estimates"] = Decimal(amount)
        if start == finish:
            values = [r.point(start, metrics)]
        else:
            with localcontext() as ctx:
                ctx.prec = 80
                duration = Decimal(str(finish)) - Decimal(str(start))
                values = r.interval(start, finish, {k: v / duration for k, v in metrics.items()})
        rows.extend(r.events(r.subjects(worker, scope), values))
    return rows


def counters(db, source, key):
    identity = json.loads(key)[0]
    table = "conversation_" + source
    rows = []
    fields = [row[1] for row in db.execute(f"PRAGMA table_info({table})")][3:]
    for row in db.execute(f"SELECT * FROM {table} WHERE id IS ?", (identity,)):
        rows.extend(
            r.events(
                r.subjects(row[1], row[2]),
                [r.point(0, {source + ":" + field: value for field, value in zip(fields, row[3:], strict=True)})],
            )
        )
    if source == "accounting":
        invalidate(db, "cost", key)
    return rows


def round_events(db, key, now):
    row = db.execute("SELECT * FROM rounds WHERE id IS ?", (json.loads(key)[0],)).fetchone()
    if not row:
        return []
    row = dict(row)
    values = [r.point(0, {"round_records": 1})]
    if row["finished"] is not None:
        kind = row["kind"]
        metrics = {"round:" + json.dumps([kind, "finished"]): 1}
        for field, outcomes in (
            ("failed", {"failed", "timeout", "interrupted"}),
            ("unknown", {"unknown", "unconfirmed"}),
        ):
            if row["outcome"] in outcomes:
                metrics["round:" + json.dumps([kind, field])] = 1
        if kind == "review" and row["outcome"] == "completed":
            metrics["reviews"] = 1
        values.append(r.point(row["finished"], metrics))
    if row["started"] is not None and row["outcome"] not in {"waiting", "interrupted-unfinished"}:
        values.extend(r.interval(row["started"], row["finished"], {"worker_seconds": 1}))
    if row["outcome"] == "interrupted-unfinished":
        values.append(r.point(0, {"interrupted_total": 1}))
        if row["started"] is not None:
            values.append(r.point(row["started"], {"interrupted": 1}))
    db.execute("INSERT OR IGNORE INTO report_members VALUES(?,?)", (row["scope"], row["worker"]))
    return r.events(r.subjects(row["worker"], row["scope"]), values)


def pr_events(db, key, now):
    repo, number = json.loads(key)
    row = db.execute("SELECT * FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
    invalidate(db, "contribution:pr_contributions", key)
    if not row:
        return []
    p = dict(row)
    loc = db.execute("SELECT head,added FROM pr_loc WHERE repo=? AND number=?", (repo, number)).fetchone()
    measured = bool(loc and loc[0] == p["head"])
    landed = p["landed"] is not None and p["landed"] <= now
    opened = p["state"] == "OPEN" and p["landed"] is None
    if p["landed"] is not None and p["landed"] > now:
        db.execute("INSERT OR REPLACE INTO report_due VALUES('pr:prs',?,?)", (key, p["landed"]))
    cohort = dict(
        authored=1,
        landed_loc_added=loc[1] if landed and measured else 0,
        open_loc_added=loc[1] if opened and measured else 0,
        landed_prs=int(landed),
        open_prs=int(opened),
        missing_loc_prs=int(((landed or opened) and not measured) or (p["state"] is None and p["landed"] is None)),
        known_loc_prs=int((landed or opened) and measured),
        measured_pr_weight=(1 if landed else 0.5 if opened else 0) if measured else 0,
    )
    values = [
        r.point(p["created"], cohort),
        r.point(
            0,
            dict(
                open_stock=int(opened),
                unknown_prs=int(p["state"] is None),
                unknown_landings=int(p["state"] == "MERGED" and p["landed"] is None),
            ),
        ),
    ]
    if p["landed"] is not None:
        values.append(r.point(p["landed"], {"landed": 1}))
    return r.events(r.subjects(p["worker"], p["scope"]), values)


def contribution_events(db, key, now):
    repo, number = json.loads(key)
    owner = db.execute("SELECT worker,scope FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
    owned = set(r.subjects(*owner)) if owner else set()
    by_subject = {}
    for row in db.execute(
        "SELECT worker,scope,at,activity FROM pr_contributions WHERE repo=? AND number=? ORDER BY at,id", (repo, number)
    ):
        worker, scope, at, activity = row
        if at > now:
            db.execute(
                "INSERT INTO report_due VALUES('contribution:pr_contributions',?,?) ON CONFLICT(source,key) DO UPDATE SET at=min(at,excluded.at)",
                (key, at),
            )
            continue
        for subject in r.subjects(worker, scope):
            values, latest = by_subject.setdefault(subject, ([], None))
            if subject in owned:
                continue
            if activity in FIXES:
                values.append(r.point(at, {"fixes": 1}))
            if activity in MEANINGFUL:
                by_subject[subject] = values, max(latest or at, at)
    result = []
    for subject, (values, latest) in by_subject.items():
        values.append(r.point(0, {"contribution_records": 1}))
        if latest is not None:
            values.append(r.point(latest, {"assisted": 1}))
        result.extend(r.events([subject], values))
    return result


def ci_events(db, key, now):
    row = db.execute("SELECT * FROM ci_activity WHERE id IS ?", (json.loads(key)[0],)).fetchone()
    if not row:
        return []
    identity, scope, at, conclusion, wait, run = row
    return r.events(
        [r.subject(), r.subject("scope", scope)],
        [
            r.point(
                at,
                dict(
                    ci_attempts=1,
                    ci_successes=int(conclusion == "success"),
                    ci_failures=int(conclusion == "failure"),
                    ci_wait_seconds=wait or 0,
                    ci_run_seconds=run or 0,
                ),
            )
        ],
    )


def flush(db, now):
    for source, key in db.execute("SELECT source,key FROM report_due WHERE at<=?", (now,)).fetchall():
        invalidate(db, source, key)
        db.execute("DELETE FROM report_due WHERE source=? AND key=?", (source, key))
    processed = 0
    while True:
        rows = db.execute("SELECT source,key FROM report_dirty ORDER BY source,key LIMIT 256").fetchall()
        if not rows:
            break
        for source, key in rows:
            db.execute("DELETE FROM report_dirty WHERE source=? AND key=?", (source, key))
            if source in {"quality", "accounting"}:
                desired = counters(db, source, key)
            else:
                handler = {
                    "cost": cost,
                    "round": round_events,
                    "pr": pr_events,
                    "contribution": contribution_events,
                    "ci": ci_events,
                }[source.split(":")[0]]
                desired = handler(db, key, now)
            r.replace(db, source.split(":")[0], key, desired)
            processed += 1
    return processed

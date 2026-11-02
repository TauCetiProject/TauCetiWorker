"""Transactional reporting deltas and moving prefix totals, independent of history size.

Each fact supplies point payments or interval slope changes. A handful of cursors
follow the report's time boundaries. Advancing them reads only crossed events;
late corrections adjust the stored prefixes directly. Decimal arithmetic stays
above the public money precision so subtracting large prefixes preserves cents.
"""

import json
from decimal import Decimal, localcontext

ZERO = Decimal(0)
SCHEMA_VERSION = 1


def subject(column=None, identity=None):
    return json.dumps([column, identity], separators=(",", ":"))


def subjects(worker, scope):
    return (subject(), subject("worker", worker), subject("scope", scope))


def install(db):
    db.execute("CREATE TABLE IF NOT EXISTS report_schema(id INTEGER PRIMARY KEY,version INTEGER)")
    previous = db.execute("SELECT version FROM report_schema WHERE id=1").fetchone()
    if previous and previous[0] != SCHEMA_VERSION:
        # Only disposable aggregates are rebuilt on an explicit format change;
        # the ingested facts, financial ledger and admission cache are retained.
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'report_%'"
        ).fetchall():
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for table in (
            "report_sources",
            "report_dirty",
            "report_events",
            "report_frontiers",
            "report_values",
            "report_subjects",
            "report_members",
            "report_due",
            "report_pr_refresh",
        ):
            db.execute(f"DROP TABLE IF EXISTS {table}")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS report_sources(name TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS report_dirty(source TEXT,key TEXT,PRIMARY KEY(source,key));
      CREATE TABLE IF NOT EXISTS report_events(
        source TEXT,key TEXT,ordinal INTEGER,subject TEXT,at REAL,step TEXT,slope TEXT,
        PRIMARY KEY(source,key,ordinal));
      CREATE INDEX IF NOT EXISTS report_event_time ON report_events(at);
      CREATE TABLE IF NOT EXISTS report_frontiers(name TEXT PRIMARY KEY,at REAL,inclusive INTEGER);
      CREATE TABLE IF NOT EXISTS report_values(
        frontier TEXT,subject TEXT,at TEXT,value TEXT,slope TEXT,PRIMARY KEY(frontier,subject));
      CREATE TABLE IF NOT EXISTS report_subjects(subject TEXT PRIMARY KEY,refs INTEGER NOT NULL);
      CREATE TRIGGER IF NOT EXISTS report_subject_add AFTER INSERT ON report_events BEGIN
        INSERT INTO report_subjects VALUES(NEW.subject,1)
          ON CONFLICT(subject) DO UPDATE SET refs=refs+1;
      END;
      CREATE TRIGGER IF NOT EXISTS report_subject_remove AFTER DELETE ON report_events BEGIN
        UPDATE report_subjects SET refs=refs-1 WHERE subject=OLD.subject;
      END;
      CREATE TABLE IF NOT EXISTS report_members(scope TEXT,worker TEXT,PRIMARY KEY(scope,worker));
      CREATE TABLE IF NOT EXISTS report_due(source TEXT,key TEXT,at REAL,PRIMARY KEY(source,key));
      CREATE INDEX IF NOT EXISTS report_due_time ON report_due(at);
    """)
    db.execute("INSERT OR REPLACE INTO report_schema VALUES(1,?)", (SCHEMA_VERSION,))


def watch(db, table, source, columns, *, fields=None):
    """Seed a new source once. Its triggers share the facts' transaction thereafter."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return
    fields = fields or [row[1] for row in db.execute(f"PRAGMA table_info({table})")]

    def expression(prefix):
        return "json_array(" + ",".join(prefix + c for c in columns) + ")"

    old, new = expression("OLD."), expression("NEW.")
    changed = " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in fields)
    db.executescript(f"""
      CREATE TRIGGER IF NOT EXISTS report_{table}_insert AFTER INSERT ON {table} BEGIN
        INSERT INTO report_dirty SELECT '{source}',{new}
          WHERE NOT EXISTS(SELECT 1 FROM report_dirty WHERE source='{source}' AND key={new});
      END;
      CREATE TRIGGER IF NOT EXISTS report_{table}_delete AFTER DELETE ON {table} BEGIN
        INSERT INTO report_dirty SELECT '{source}',{old}
          WHERE NOT EXISTS(SELECT 1 FROM report_dirty WHERE source='{source}' AND key={old});
      END;
      CREATE TRIGGER IF NOT EXISTS report_{table}_update AFTER UPDATE ON {table} WHEN {changed} BEGIN
        INSERT INTO report_dirty SELECT '{source}',{old}
          WHERE NOT EXISTS(SELECT 1 FROM report_dirty WHERE source='{source}' AND key={old});
        INSERT INTO report_dirty SELECT '{source}',{new}
          WHERE NOT EXISTS(SELECT 1 FROM report_dirty WHERE source='{source}' AND key={new});
      END;
    """)
    if db.execute("INSERT OR IGNORE INTO report_sources VALUES(?)", (source,)).rowcount:
        db.execute(f"INSERT OR IGNORE INTO report_dirty SELECT ?,{expression('')} FROM {table}", (source,))


def decode(raw):
    return {k: Decimal(v) for k, v in json.loads(raw).items()}


def encode(values):
    return json.dumps({k: str(v) for k, v in values.items() if v}, sort_keys=True, separators=(",", ":"))


def add(target, values, factor=Decimal(1)):
    for key, value in values.items():
        target[key] = target.get(key, ZERO) + value * factor


def state(db, frontier, owner):
    row = db.execute(
        "SELECT at,value,slope FROM report_values WHERE frontier=? AND subject=?", (frontier, owner)
    ).fetchone()
    return (Decimal(row[0]), decode(row[1]), decode(row[2])) if row else (ZERO, {}, {})


def save(db, frontier, owner, at, value, slope):
    db.execute(
        "INSERT OR REPLACE INTO report_values VALUES(?,?,?,?,?)",
        (frontier, owner, str(at), encode(value), encode(slope)),
    )


def point(at, metrics):
    return (at, {k: Decimal(str(v)) for k, v in metrics.items() if v}, {})


def interval(start, finish, metrics):
    if finish is not None and finish <= start:
        return []
    rate = {k: Decimal(str(v)) for k, v in metrics.items() if v}
    return [(start, {}, rate)] + ([(finish, {}, {k: -v for k, v in rate.items()})] if finish is not None else [])


def events(owners, rows):
    return [(owner, at, encode(step), encode(slope)) for owner in owners for at, step, slope in rows]


def replace(db, source, key, desired):
    old = [
        tuple(row)
        for row in db.execute(
            "SELECT subject,at,step,slope FROM report_events WHERE source=? AND key=? ORDER BY ordinal", (source, key)
        )
    ]
    if old == desired:
        return False
    with localcontext() as ctx:
        ctx.prec = 80
        frontiers = list(db.execute("SELECT name,at,inclusive FROM report_frontiers"))
        for rows, sign in ((old, Decimal(-1)), (desired, Decimal(1))):
            for owner, at, step, slope in rows:
                step, slope = decode(step), decode(slope)
                for name, position, inclusive in frontiers:
                    if at > position or (at == position and not inclusive):
                        continue
                    position = Decimal(str(position))
                    previous, value, derivative = state(db, name, owner)
                    add(value, derivative, position - previous)
                    add(value, step, sign)
                    add(value, slope, sign * (position - Decimal(str(at))))
                    add(derivative, slope, sign)
                    save(db, name, owner, position, value, derivative)
        db.execute("DELETE FROM report_events WHERE source=? AND key=?", (source, key))
        db.executemany(
            "INSERT INTO report_events VALUES(?,?,?,?,?,?,?)",
            ((source, key, ordinal, *row) for ordinal, row in enumerate(desired)),
        )
    return True


def advance(db, name, at, *, inclusive=False):
    previous = db.execute("SELECT at,inclusive FROM report_frontiers WHERE name=?", (name,)).fetchone()
    if previous and previous[0] == at and bool(previous[1]) == inclusive:
        return 0
    # Only creation or a backwards clock requires a prefix rebuild. Normal
    # advances use an indexed interval, regardless of retained history size.
    rebuild = not previous or previous[0] > at or bool(previous[1]) != inclusive
    if rebuild:
        db.execute("DELETE FROM report_values WHERE frontier=?", (name,))
    upper = "<=" if inclusive else "<"
    lower = ">" if inclusive else ">="
    clause = "" if rebuild else f"AND at{lower}?"
    args = [at] if rebuild else [at, previous[0]]
    count, states = 0, {}
    with localcontext() as ctx:
        ctx.prec = 80
        for owner, when, step, slope in db.execute(
            f"SELECT subject,at,step,slope FROM report_events WHERE at{upper}? {clause} ORDER BY at", args
        ):
            count += 1
            when = Decimal(str(when))
            if owner not in states:
                states[owner] = state(db, name, owner)
            position, value, derivative = states[owner]
            add(value, derivative, when - position)
            add(value, decode(step))
            add(derivative, decode(slope))
            states[owner] = when, value, derivative
        for owner, (position, value, derivative) in states.items():
            save(db, name, owner, position, value, derivative)
    db.execute("INSERT OR REPLACE INTO report_frontiers VALUES(?,?,?)", (name, at, inclusive))
    return count


def prefix(db, name, owner):
    at = db.execute("SELECT at FROM report_frontiers WHERE name=?", (name,)).fetchone()[0]
    with localcontext() as ctx:
        ctx.prec = 80
        position, value, slope = state(db, name, owner)
        add(value, slope, Decimal(str(at)) - position)
    return value


def snapshot(db, now, windows, cutoff=None, column=None, identity=None):
    owner = subject(column, identity)
    advance(db, "now", now, inclusive=True)
    upper = prefix(db, "now", owner)
    result = {}
    for name, seconds in windows.items():
        since = max(cutoff or 0, now - seconds) if seconds is not None else cutoff or 0
        if seconds is not None:
            advance(db, "lower:" + name, now - seconds)
        lower = "cutoff:" + str(cutoff or 0) if since == (cutoff or 0) else "lower:" + name
        if since == (cutoff or 0):
            advance(db, lower, since)
        before = prefix(db, lower, owner)
        with localcontext() as ctx:
            ctx.prec = 80
            value = dict(upper)
            add(value, before, Decimal(-1))
        result[name] = value
    return upper, result

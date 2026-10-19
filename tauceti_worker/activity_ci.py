"""Incremental, read-only import of already-recorded local CI receipts."""

import contextlib
import json
import os
import sqlite3
from pathlib import Path


def prepare(db, now, scope):
    path = Path(os.environ.get("TAUCETI_ACTIVITY_CI_DB", Path.home() / ".local/state/tauceti-fleet/events.sqlite"))
    if not path.is_file() or not scope:
        return
    prior = db.execute("SELECT value FROM meta WHERE key='activity_ci_cursor'").fetchone()
    cursor = json.loads(prior[0]) if prior else {}
    inode = path.stat().st_ino
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        high = source.execute("SELECT MAX(rowid) FROM events").fetchone()[0] or 0
        offset = cursor.get("offset", 0) if cursor.get("inode") == inode and cursor.get("path") == str(path) else 0
        if offset > high:
            offset = 0
        cutoff = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
        if offset == 0:
            db.execute("DELETE FROM ci_activity WHERE scope=?", (scope,))
            first = source.execute(
                "SELECT MIN(rowid) FROM events WHERE kind='ci_result' AND at>=?", (cutoff[0] if cutoff else 0,)
            ).fetchone()[0]
            offset = first - 1 if first is not None else high
        for seq, identity, at, kind, raw in source.execute(
            "SELECT rowid,id,at,kind,data FROM events WHERE rowid>? ORDER BY rowid LIMIT 2000", (offset,)
        ):
            if kind == "ci_result" and (not cutoff or at >= cutoff[0]):
                event = json.loads(raw)
                db.execute(
                    "INSERT OR IGNORE INTO ci_activity VALUES (?,?,?,?,?,?)",
                    (identity, scope, at, event.get("conclusion"), event.get("wait_seconds"), event.get("run_seconds")),
                )
            offset = seq
        db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('activity_ci_cursor',?)",
            (json.dumps(dict(path=str(path), inode=inode, offset=offset, prepared_at=now, complete=offset >= high)),),
        )


def summarize(db, now, windows, column=None, identity=None):
    cursor = db.execute("SELECT value FROM meta WHERE key='activity_ci_cursor'").fetchone()
    if column == "worker" or not cursor or not json.loads(cursor[0]).get("complete", False):
        return None
    condition, args = ("AND scope=?", [identity]) if column else ("", [])
    result = {}
    for name, seconds in windows.items():
        since = now - seconds if seconds is not None else 0
        rows = list(db.execute(f"SELECT * FROM ci_activity WHERE at>=? AND at<=? {condition}", [since, now, *args]))
        result[name] = dict(
            attempts=len(rows),
            successes=sum(r["conclusion"] == "success" for r in rows),
            failures=sum(r["conclusion"] == "failure" for r in rows),
            wait_seconds=sum(r["wait_seconds"] or 0 for r in rows),
            run_seconds=sum(r["run_seconds"] or 0 for r in rows),
        )
    return result

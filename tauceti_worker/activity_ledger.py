"""Activity-owned incremental ledger replay; never opens the admission projection."""

import contextlib
import json
import uuid
from pathlib import Path

from .budget import Budget
from .budget_projection import Projection


class ReportingBudget(Budget):
    def replay(self, records=None, *, _state=None, _samples=True):
        if records is None:
            records = self.records()
        state = super().replay([], _state=_state, _samples=False)
        state.setdefault("_activity_invocations", 0)
        for record in records:
            state["_activity_invocations"] += sum(
                event["type"] == "admit" and event["id"] not in state["sessions"] for event in record["events"]
            )
            # Preserve the first settlement transaction time, independently of
            # the recorded interval endpoint and later native corrections.
            first = {
                e["id"]: state["sessions"].get(e["id"], {}).get("_activity_settled_at", record["timestamp"])
                for e in record["events"]
                if e["type"] == "settle"
            }
            state = super().replay([record], _state=state, _samples=False)
            for key, at in first.items():
                state["sessions"][key]["_activity_settled_at"] = at
        return state


class ReportingProjection(Projection):
    def before_rebuild(self, db):
        # This must commit atomically with replacing projected sessions. A crash
        # immediately after replay must still invalidate the activity consumer.
        db.execute("UPDATE report_epoch SET value=? WHERE id=1", (uuid.uuid4().hex,))

    def connect(self):
        db = super().connect()
        db.executescript("""
          CREATE TABLE IF NOT EXISTS report_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT);
          CREATE TABLE IF NOT EXISTS report_epoch(id INTEGER PRIMARY KEY,value TEXT);
          CREATE TRIGGER IF NOT EXISTS report_session_insert AFTER INSERT ON sessions BEGIN
            INSERT INTO report_changes(id) VALUES(NEW.id);
          END;
          CREATE TRIGGER IF NOT EXISTS report_session_update AFTER UPDATE ON sessions
            WHEN OLD.data IS NOT NEW.data BEGIN INSERT INTO report_changes(id) VALUES(NEW.id); END;
        """)
        if db.execute("INSERT OR IGNORE INTO report_epoch VALUES(1,?)", (uuid.uuid4().hex,)).rowcount:
            db.execute("INSERT INTO report_changes(id) SELECT id FROM sessions")
        saved = db.execute("SELECT data FROM meta WHERE id=1").fetchone()
        if saved:
            metadata = json.loads(saved[0])
            if "_activity_invocations" not in metadata["state"]:
                metadata["state"]["_activity_invocations"] = db.execute("SELECT count(*) FROM sessions").fetchone()[0]
                db.execute("UPDATE meta SET data=? WHERE id=1", (json.dumps(metadata),))
        return db


@contextlib.contextmanager
def load(db, budget):
    filename = db.execute("PRAGMA database_list").fetchone()[2]
    if not filename:
        records = budget.records()
        state = budget.replay(records)
        settled = {}
        for record in records:
            for event in record["events"]:
                if event["type"] == "settle":
                    settled.setdefault(event["id"], record["timestamp"])
        for key, at in settled.items():
            state["sessions"][key]["_activity_settled_at"] = at
        yield (
            state,
            set(state["sessions"]),
            dict(replayed_bytes=budget.ledger.stat().st_size, rebuilt=True, cursor=None),
        )
        return
    directory = Path(filename).parent / "cost-accounting"
    directory.mkdir(parents=True, exist_ok=True)
    reporting = ReportingBudget(directory, clock=budget.clock)
    reporting.ledger = budget.ledger
    projection = ReportingProjection(reporting, complete_prefix=True)
    try:
        state = projection.load()
        store = projection.db
        epoch = store.execute("SELECT value FROM report_epoch WHERE id=1").fetchone()[0]
        old = db.execute("SELECT value FROM meta WHERE key='activity_cost_cursor'").fetchone()
        old = json.loads(old[0]) if old else {}
        rebuilt = old.get("epoch") != epoch
        acknowledged = old.get("seq", 0) if not rebuilt else 0
        # Acknowledge only the previous, committed activity transaction. A crash
        # after projection replay cannot lose notifications for unprepared costs.
        store.execute("DELETE FROM report_changes WHERE seq<=?", (acknowledged,))
        changed = {
            row[0] for row in store.execute("SELECT DISTINCT id FROM report_changes WHERE seq>?", (acknowledged,))
        }
        if rebuilt:
            changed = {row[0] for row in store.execute("SELECT id FROM sessions")}
        high = store.execute("SELECT MAX(seq) FROM report_changes").fetchone()[0] or acknowledged
        yield (
            state,
            changed,
            dict(
                replayed_bytes=projection.replayed_bytes,
                rebuilt=rebuilt,
                cursor=dict(epoch=epoch, seq=high),
            ),
        )
    finally:
        projection.close()

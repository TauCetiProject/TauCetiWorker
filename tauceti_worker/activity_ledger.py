"""Activity-owned incremental ledger replay; never opens the admission projection."""

from pathlib import Path

from .budget import Budget
from .budget_projection import Projection


class ReportingBudget(Budget):
    def replay(self, records=None, *, _state=None, _samples=True):
        if records is None:
            records = self.records()
        state = super().replay([], _state=_state, _samples=False)
        for record in records:
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
        return state, settled, dict(replayed_bytes=budget.ledger.stat().st_size, rebuilt=True)
    directory = Path(filename).parent / "cost-accounting"
    directory.mkdir(parents=True, exist_ok=True)
    reporting = ReportingBudget(directory, clock=budget.clock)
    reporting.ledger = budget.ledger
    projection = Projection(reporting, complete_prefix=True)
    try:
        state = projection.load()
        rows = reporting.sessions(state)
        state["sessions"] = {s["id"]: s for s in rows}
        return (
            state,
            {s["id"]: s["_activity_settled_at"] for s in rows if "_activity_settled_at" in s},
            dict(replayed_bytes=projection.replayed_bytes, rebuilt=projection.rebuilt),
        )
    finally:
        projection.close()

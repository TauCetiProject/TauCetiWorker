"""Disposable, indexed projection of the authoritative budget JSONL ledger."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def projected_read(function):
    @functools.wraps(function)
    def read(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except (sqlite3.DatabaseError, ValueError, TypeError):
            self.owner.discard()
            self.fallback = self.owner.budget.replay()["sessions"]
            return function(self, *args, **kwargs)

    return read


class Sessions(Mapping):
    def __init__(self, db, owner=None):
        self.db = db
        self.owner = owner
        self.changed = {}
        self.fallback = None

    @projected_read
    def __getitem__(self, key):
        if self.fallback is not None:
            return self.fallback[key]
        if key not in self.changed:
            row = self.db.execute("SELECT data FROM sessions WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            self.changed[key] = json.loads(row[0])
        return self.changed[key]

    def __setitem__(self, key, value):
        self.changed[key] = value

    @projected_read
    def __iter__(self):
        if self.fallback is not None:
            return iter(self.fallback)
        return iter([row[0] for row in self.db.execute("SELECT id FROM sessions ORDER BY ordinal")])

    @projected_read
    def __len__(self):
        if self.fallback is not None:
            return len(self.fallback)
        return self.db.execute("SELECT count(*) FROM sessions").fetchone()[0]

    @projected_read
    def select(
        self, *, active=False, model=None, phase=None, settled=False, positive=False, estimated=False, limit=None
    ):
        if self.fallback is not None:
            return self.owner.budget.sessions(
                {"sessions": self.fallback},
                active=active,
                model=model,
                phase=phase,
                settled=settled,
                positive=positive,
                estimated=estimated,
                limit=limit,
            )
        clauses, args = [], []
        if estimated:
            clauses.append(
                "json_extract(data,'$.cost_provenance') IN ('token-priced','historical-estimate','prelaunch-estimate')"
            )
        if active:
            clauses.append("status != 'settled'")
        if settled:
            clauses.append("status = 'settled'")
        if model is not None:
            clauses.append("model=?")
            args.append(model)
        if phase is not None:
            clauses.append("phase=?")
            args.append(phase)
        if positive:
            clauses.append("positive_cost=1")
        query = "SELECT data FROM sessions" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        query += " ORDER BY ordinal" + (" DESC LIMIT ?" if limit is not None else "")
        if limit is not None:
            args.append(limit)
        rows = [json.loads(row[0]) for row in self.db.execute(query, args)]
        return rows[::-1] if limit is not None else rows

    @projected_read
    def related(self, providers, *, include=None):
        if self.fallback is not None:
            return self.owner.budget.related({"sessions": self.fallback}, providers, include)
        providers = list(providers)
        placeholders = ",".join("?" for _ in providers)
        # Resumes without a receipt also matter for liveness/evidence boundaries.
        sql = f"SELECT data FROM sessions WHERE id=? OR resume IN ({placeholders}) OR id IN (SELECT session FROM providers WHERE provider IN ({placeholders})) ORDER BY ordinal"
        return [json.loads(r[0]) for r in self.db.execute(sql, [include, *providers, *providers])]

    def save(self, ordinal):
        for key, value in self.changed.items():
            if not self.db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone():
                ordinal = self.db.execute("SELECT coalesce(max(ordinal),0)+1 FROM sessions").fetchone()[0]

            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, model=excluded.model, phase=excluded.phase, resume=excluded.resume, cost=excluded.cost, positive_cost=excluded.positive_cost, data=excluded.data",
                (
                    key,
                    ordinal,
                    value["status"],
                    value["model"],
                    value["phase"],
                    value.get("resume"),
                    value.get("cost", "0"),
                    int(Decimal(value.get("cost", "0")) > 0),
                    json.dumps(value, separators=(",", ":")),
                ),
            )
            self.db.execute("DELETE FROM providers WHERE session=?", (key,))
            self.db.executemany("INSERT INTO providers VALUES (?,?)", [(p, key) for p in value.get("totals", {})])
        self.changed.clear()


class Projection:
    def __init__(self, budget):
        self.budget = budget
        self.path = budget.directory / "projection.sqlite"
        self.db = None

    def connect(self):
        if self.db is None:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, ordinal INTEGER, status TEXT, model TEXT, phase TEXT, resume TEXT, cost TEXT, positive_cost INTEGER, data TEXT);
                CREATE INDEX IF NOT EXISTS session_order ON sessions(ordinal);
                CREATE INDEX IF NOT EXISTS active ON sessions(ordinal) WHERE status != 'settled';
                CREATE INDEX IF NOT EXISTS costs ON sessions(model,phase,status,positive_cost,ordinal);
                CREATE INDEX IF NOT EXISTS recent ON sessions(status,ordinal);
                CREATE INDEX IF NOT EXISTS model_costs ON sessions(model,status,positive_cost,ordinal);
                CREATE INDEX IF NOT EXISTS resumes ON sessions(resume,ordinal);
                CREATE TABLE IF NOT EXISTS providers (provider TEXT, session TEXT, PRIMARY KEY(provider,session));
                CREATE INDEX IF NOT EXISTS provider_session ON providers(session);
            """)
        return self.db

    def load(self):
        try:
            return self._load()
        except (sqlite3.DatabaseError, ValueError, InvalidOperation, KeyError, TypeError):
            # This file holds no unique financial evidence. Rebuild only it.
            if self.db is not None:
                self.db.close()
                self.db = None
            self.path.unlink(missing_ok=True)
            self.path.with_name(self.path.name + "-journal").unlink(missing_ok=True)
            return self._load()

    def _load(self):
        db = self.connect()
        with contextlib.ExitStack() as stack, db:
            try:
                stream = stack.enter_context(self.budget.ledger.open("rb"))
            except FileNotFoundError:
                stream = None
            db.execute("BEGIN")
            saved = db.execute("SELECT data FROM meta WHERE id=1").fetchone()
            saved = json.loads(saved[0]) if saved else None
            info = os.fstat(stream.fileno()) if stream else None
            identity = [info.st_dev, info.st_ino] if info else None
            size = info.st_size if info else 0
            valid = saved and saved["identity"] == identity and saved["offset"] <= size
            if valid and stream:
                offset = saved["offset"]
                stream.seek(max(0, offset - 256))
                valid = hashlib.sha256(stream.read(min(offset, 256))).hexdigest() == saved["anchor"]
                # An in-place rewrite without append invalidates the projection.
                if size == offset and info.st_mtime_ns != saved["mtime_ns"]:
                    valid = False
            if not valid:
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM providers")
                saved = {"offset": 0, "state": {}}
            scalar = saved["state"]
            state = self.budget.replay([], _state=None) if not scalar else dict(scalar)
            for key in ("balance", "rate", "spent", "watermark"):
                state[key] = Decimal(state[key])
                if not state[key].is_finite():
                    raise ValueError("invalid cached amount")
            state["sessions"] = Sessions(db, self)
            state["samples"] = {}
            offset = saved["offset"]
            try:
                if stream:
                    stream.seek(offset)
                    raw = stream.read()
                else:
                    raw = b""
                records = self.budget.records(raw, start=state["seq"])
                for record in records:
                    self.budget.replay([record], _state=state, _samples=False)
                    state["sessions"].save(record["seq"])
                if raw or not valid:
                    offset += len(raw)
                    if stream:
                        stream.seek(max(0, offset - 256))
                        anchor = hashlib.sha256(stream.read(min(offset, 256))).hexdigest()
                    else:
                        anchor = hashlib.sha256(b"").hexdigest()
                    metadata = {k: v for k, v in state.items() if k not in ("sessions", "samples")}
                    for key in ("balance", "rate", "spent", "watermark"):
                        metadata[key] = str(metadata[key])
                    db.execute(
                        "INSERT OR REPLACE INTO meta VALUES (1,?)",
                        (
                            json.dumps(
                                dict(
                                    identity=identity,
                                    offset=offset,
                                    anchor=anchor,
                                    mtime_ns=info.st_mtime_ns if info else None,
                                    state=metadata,
                                )
                            ),
                        ),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
            state["sessions"] = Sessions(db, self)
            return state

    def discard(self):
        self.close()
        self.path.unlink(missing_ok=True)
        self.path.with_name(self.path.name + "-journal").unlink(missing_ok=True)

    def __del__(self):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

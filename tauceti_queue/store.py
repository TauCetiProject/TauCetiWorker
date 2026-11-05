"""The queue's records in one SQLite database.

The plan names one database for the service; a single-process service (controller loop plus HTTP
server sharing one `Controller`) does not need a database server, so this is stdlib `sqlite3` in
WAL mode with every mutation inside a transaction. The SQL is plain (no SQLite-only types, `?`
placeholders, JSON kept in TEXT columns) and all of it lives here, so a PostgreSQL backend is a
second implementation of this class rather than a rewrite of the controller.

Tables:

- `settings`: runtime settings and small pieces of controller state (`key`, JSON `value`, actor, time).
- `inputs`: approved inputs, one row per accepted PR revision, with the waiting/reserved/inserted/
  landed/cancelled state, priority, urgency and the retained ref of the accepted head.
- `batches`: identity, version, base, tip, sealed flag, state (`open`, `head`, `landed`, `removed`)
  and the expected parent recorded for promotion.
- `members`: per batch and position, the member's commit and provenance (`clean` or `edited`),
  the task that edited it and the explanation collected from its message.
- `claims`: batch identity and version, operation, worker identity, token, expiry, release.
- `tasks`: repair and rebase-conflict tasks with their diagnostics and state.
- `ci_attempts`: every candidate dispatch with the run it was bound to, placement, phases, outcome.
- `outbox`: pending side effects (promotion, comments, closes, labels, ref retention) with an
  idempotency key and attempt history.
- `journal`: an append-only log of what the controller did, for status and tests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    actor TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS inputs (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    coauthors TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    reviewer TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    urgent INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    approved_at REAL NOT NULL,
    retained_ref TEXT NOT NULL DEFAULT '',
    batch_id INTEGER,
    passes_waited INTEGER NOT NULL DEFAULT 0,
    probes INTEGER NOT NULL DEFAULT 0,
    last_probe TEXT NOT NULL DEFAULT '',
    last_probe_tip TEXT NOT NULL DEFAULT '',
    last_probe_base TEXT NOT NULL DEFAULT '',
    probe_conflicts TEXT NOT NULL DEFAULT '{}',
    landed_at REAL,
    landed_sha TEXT
);
CREATE INDEX IF NOT EXISTS inputs_state ON inputs (repository, state);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    base TEXT NOT NULL,
    tip TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    sealed_at REAL,
    landed_at REAL,
    landed_how TEXT NOT NULL DEFAULT '',
    expected_parent TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    first_dispatch_at REAL
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    input_id INTEGER NOT NULL,
    source_pr TEXT NOT NULL,
    number INTEGER NOT NULL,
    accepted_head TEXT NOT NULL,
    position INTEGER NOT NULL,
    commit_sha TEXT,
    provenance TEXT NOT NULL DEFAULT 'clean',
    task_id INTEGER,
    explanation TEXT NOT NULL DEFAULT '',
    edited_shas TEXT NOT NULL DEFAULT '[]',
    added_at REAL NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0,
    removed_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS members_batch ON members (batch_id, removed);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    batch_version INTEGER NOT NULL,
    operation TEXT NOT NULL,
    worker TEXT NOT NULL,
    github_login TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    starting_tip TEXT NOT NULL,
    task_id INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    renewed_at REAL,
    released_at REAL,
    release_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS claims_batch ON claims (batch_id, released_at);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    repository TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    batch_version INTEGER NOT NULL,
    base TEXT NOT NULL,
    tip TEXT NOT NULL,
    onto TEXT NOT NULL,
    diagnostics TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT NOT NULL DEFAULT '',
    ci_attempt_id INTEGER,
    claims INTEGER NOT NULL DEFAULT 0,
    submissions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks (state, batch_id);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    claim_id INTEGER,
    tip TEXT NOT NULL,
    retained_ref TEXT NOT NULL DEFAULT '',
    accepted INTEGER NOT NULL,
    reason TEXT NOT NULL,
    worker_notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_attempts (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    batch_version INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    dispatch_id TEXT NOT NULL DEFAULT '',
    dispatched_at REAL NOT NULL,
    run_id TEXT,
    run_attempt INTEGER,
    run_status TEXT NOT NULL DEFAULT '',
    run_conclusion TEXT,
    run_created_at REAL,
    run_started_at REAL,
    run_completed_at REAL,
    runner_name TEXT,
    runner_labels TEXT NOT NULL DEFAULT '[]',
    host TEXT,
    phases TEXT NOT NULL DEFAULT '[]',
    html_url TEXT NOT NULL DEFAULT '',
    last_progress_at REAL,
    last_observed_at REAL,
    outcome TEXT NOT NULL DEFAULT 'pending',
    outcome_detail TEXT NOT NULL DEFAULT '',
    diagnostics TEXT NOT NULL DEFAULT '[]',
    infrastructure_retry INTEGER NOT NULL DEFAULT 0,
    needs_observation INTEGER NOT NULL DEFAULT 1,
    verdicts TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ci_batch ON ci_attempts (batch_id, batch_version);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    history TEXT NOT NULL DEFAULT '[]',
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL,
    result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox (state, next_attempt_at);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    batch_id INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


class Store:
    """A small row-oriented API over the schema above; the controller owns the meaning of the rows."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(SCHEMA)
        self._add_missing_columns()

    # Columns added to a table after databases with the table already existed; `CREATE TABLE IF NOT
    # EXISTS` leaves those databases alone, so each is added here when absent.
    ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("ci_attempts", "verdicts", "TEXT NOT NULL DEFAULT '{}'"),
        ("inputs", "last_probe_base", "TEXT NOT NULL DEFAULT ''"),
        ("inputs", "probe_conflicts", "TEXT NOT NULL DEFAULT '{}'"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, declaration in self.ADDED_COLUMNS:
            present = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # --- transactions ------------------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """`BEGIN IMMEDIATE` ... `COMMIT`; nested uses join the outer transaction. Rolls back on error."""
        with self._lock:
            if self._depth == 0:
                self._connection.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._connection.execute("COMMIT")

    # --- generic rows ------------------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, params)

    def insert(self, table: str, **row: Any) -> int:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.transaction():
            cursor = self.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))
            return int(cursor.lastrowid or 0)

    def update(self, table: str, where: Mapping[str, Any], **values: Any) -> int:
        if not values:
            return 0
        sets = ", ".join(f"{column} = ?" for column in values)
        clause, params = self._where(where)
        with self.transaction():
            cursor = self.execute(f"UPDATE {table} SET {sets}{clause}", [*values.values(), *params])
            return cursor.rowcount

    def rows(
        self,
        table: str,
        where: Mapping[str, Any] | None = None,
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clause, params = self._where(where or {})
        sql = f"SELECT * FROM {table}{clause}"
        if order:
            sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        # The connection is shared by the controller loop and the HTTP threads (feeds read the store
        # without the controller lock): the fetch must happen under the same lock as the execute, or a
        # concurrent statement resets the cursor mid-read (seen as `IndexError` on `/status` live).
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    def row(self, table: str, where: Mapping[str, Any], order: str | None = None) -> dict[str, Any] | None:
        found = self.rows(table, where, order=order, limit=1)
        return found[0] if found else None

    def count(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        clause, params = self._where(where or {})
        with self._lock:
            return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}{clause}", params).fetchone()[0])

    @staticmethod
    def _where(where: Mapping[str, Any]) -> tuple[str, list[Any]]:
        if not where:
            return "", []
        parts: list[str] = []
        params: list[Any] = []
        for column, value in where.items():
            if value is None:
                parts.append(f"{column} IS NULL")
            elif isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    parts.append("0 = 1")
                else:
                    parts.append(f"{column} IN ({', '.join('?' for _ in values)})")
                    params.extend(values)
            else:
                parts.append(f"{column} = ?")
                params.append(value)
        return " WHERE " + " AND ".join(parts), params

    # --- settings ----------------------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.row("settings", {"key": key})
        return default if row is None else loads(row["value"])

    def set_setting(self, key: str, value: Any, actor: str = "", at: float | None = None) -> None:
        with self.transaction():
            self.execute(
                "INSERT INTO settings (key, value, actor, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, actor = excluded.actor, "
                "updated_at = excluded.updated_at",
                (key, dumps(value), actor, at),
            )

    def settings(self) -> dict[str, dict[str, Any]]:
        return {
            row["key"]: {"value": loads(row["value"]), "actor": row["actor"], "updated_at": row["updated_at"]}
            for row in self.rows("settings", order="key")
        }

    # --- journal -----------------------------------------------------------------------------------

    def journal(self, at: float, kind: str, batch_id: int | None = None, **detail: Any) -> None:
        self.insert("journal", at=at, kind=kind, batch_id=batch_id, detail=dumps(detail))

    def journal_entries(self, kind: str | None = None, batch_id: int | None = None) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if kind is not None:
            where["kind"] = kind
        if batch_id is not None:
            where["batch_id"] = batch_id
        entries = self.rows("journal", where, order="id")
        for entry in entries:
            entry["detail"] = loads(entry["detail"], {})
        return entries

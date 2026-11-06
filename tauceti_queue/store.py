"""The queue's records in one SQLite database.

The plan names one database for the service; a single-process service (controller loop plus HTTP
server sharing one `Controller`) does not need a database server, so this is stdlib `sqlite3` in
WAL mode with every mutation inside a transaction. The SQL is plain (no SQLite-only types, `?`
placeholders, JSON kept in TEXT columns) and all of it lives here, so a PostgreSQL backend is a
second implementation of this class rather than a rewrite of the controller.

Tables:

- `settings`: runtime settings and small pieces of controller state (`key`, JSON `value`, actor, time).
- `inputs`: approved inputs, one row per accepted PR revision, with the waiting/reserved/inserted/
  landed/cancelled state, priority, urgency, the retained ref of the accepted head, the `no_repair`
  opt-out and, for a `problem-anticipated` withdrawal, what it waits for (`anticipated`).
- `batches`: identity, version, base, tip, sealed flag, state (`open`, `head`, `landed`, `removed`),
  the expected parent recorded for promotion and, for an open batch, the version a `prepare` task
  last built (and repaired) on its own base and its pre-rebase onto the head's tip
  (`pre_rebase_state`, `_tip`, `_onto`, `_source`, `_edits`, `_at`, and `_prepared`, the pre-rebased
  tip a `prepare` task last built and repaired on the head's tip).
- `members`: per batch and position, the member's commit and provenance (`clean` or `edited`),
  the task that edited it, the explanation collected from its message, the `no_repair` opt-out and
  whether it was `withdrawn` (an emptied `no_repair` member whose commit stays until the head lands).
- `claims`: batch identity and version, operation, worker identity, token, expiry, release.
- `tasks`: repair, rebase-conflict, pre-rebase and prepare tasks with their diagnostics and state; a
  `prepare` task of a batch's pre-rebased chain carries the batch tip that chain covers
  (`pre_rebase_source`), and none for a preparation on the batch's own base.
- `batch_tips`: every tip a batch has had, by version and time, so that a CI dispatch can name the
  previous tips whose published build outputs Lake can reuse after the batch is replayed.
- `ci_attempts`: every candidate dispatch with the run it was bound to, placement, phases, outcome,
  and the candidate revisions for published outputs it sent (`repair_outputs_revs`). An attempt
  whose live run the controller cancelled because a newer dispatch, a landing, a removal or a
  rebuild superseded it records when (`cancelled_at`), the attempt that superseded it
  (`cancelled_by`, empty otherwise), why (`cancel_reason`) and GitHub's answer (`cancel_result`).
- `outbox`: pending side effects (promotion, comments, closes, labels, ref retention) with an
  idempotency key and attempt history.
- `journal`: an append-only log of what the controller did, for status and tests.
- `identity_cache`: answers GitHub gave about workers and operators, kept across restarts so a
  restart during a quota outage does not lock them out: per `namespace` (`bearer`, a token's
  SHA-256 fingerprint to its JSON `[login, id]`; `admin` and `server-admin`, a login to its
  repository-admin answer), the JSON `value` and the time GitHub last confirmed it (`verified_at`).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
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
    landed_sha TEXT,
    no_repair INTEGER NOT NULL DEFAULT 0,
    anticipated TEXT NOT NULL DEFAULT '{}',
    declaration_names TEXT NOT NULL DEFAULT '{}',
    declaration_fetch_attempts INTEGER NOT NULL DEFAULT 0,
    declaration_retry_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS inputs_state ON inputs (repository, state);
CREATE TABLE IF NOT EXISTS admission_work (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL DEFAULT '',
    number INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1,
    evaluated_generation INTEGER NOT NULL DEFAULT 0,
    observed_version TEXT NOT NULL DEFAULT '',
    evaluated_version TEXT NOT NULL DEFAULT '',
    authorization_key TEXT NOT NULL DEFAULT '',
    cancelled_authorization TEXT NOT NULL DEFAULT '',
    cancelled_head_sha TEXT NOT NULL DEFAULT '',
    cancelled_at REAL NOT NULL DEFAULT 0,
    urgent INTEGER NOT NULL DEFAULT 0,
    first_pending_at REAL NOT NULL DEFAULT 0,
    attempted_at REAL NOT NULL DEFAULT 0,
    queue_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    retry_resource TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '{}',
    disposition TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS admission_pr ON admission_work (repository, number);
CREATE INDEX IF NOT EXISTS admission_due ON admission_work (repository, next_attempt_at, attempted_at);
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
    first_dispatch_at REAL,
    prepared_version INTEGER,
    pre_rebase_state TEXT NOT NULL DEFAULT '',
    pre_rebase_tip TEXT,
    pre_rebase_onto TEXT,
    pre_rebase_source TEXT,
    pre_rebase_edits TEXT NOT NULL DEFAULT '{}',
    pre_rebase_at REAL,
    pre_rebase_prepared TEXT
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
    removed_reason TEXT NOT NULL DEFAULT '',
    no_repair INTEGER NOT NULL DEFAULT 0,
    withdrawn INTEGER NOT NULL DEFAULT 0
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
    submissions INTEGER NOT NULL DEFAULT 0,
    pre_rebase_source TEXT
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
CREATE TABLE IF NOT EXISTS batch_tips (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    tip TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS batch_tips_version ON batch_tips (batch_id, version);
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
    run_updated_at REAL,
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
    verdicts TEXT NOT NULL DEFAULT '{}',
    repair_outputs_revs TEXT NOT NULL DEFAULT '[]',
    cancelled_at REAL,
    cancelled_by INTEGER,
    cancel_reason TEXT NOT NULL DEFAULT '',
    cancel_result TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS identity_cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    verified_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    batch_id INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


# The `CREATE TABLE` statements of `SCHEMA` alone.
TABLES = "".join(statement + ";\n" for statement in SCHEMA.split(";") if statement.strip().startswith("CREATE TABLE"))


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def declared_columns() -> dict[str, list[tuple[str, str]]]:
    """Each table `SCHEMA` creates, with its columns' names and `ADD COLUMN` declarations in order."""
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(SCHEMA)
        tables = [
            name
            for (name,) in scratch.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result: dict[str, list[tuple[str, str]]] = {}
        for table in tables:
            columns = []
            for _cid, name, type_, notnull, default, _pk in scratch.execute(f"PRAGMA table_info({table})"):
                declaration = type_
                if notnull:
                    declaration += " NOT NULL"
                if default is not None:
                    declaration += f" DEFAULT {default}"
                columns.append((name, declaration))
            result[table] = columns
        return result
    finally:
        scratch.close()


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
        # Tables first, then the columns declared since a table was created, then the indexes (which
        # may name those columns).
        self._connection.executescript(TABLES)
        self._add_missing_columns()
        self._connection.executescript(SCHEMA)

    def _add_missing_columns(self) -> None:
        """Add to each table every column `SCHEMA` declares that the database's table lacks.

        `CREATE TABLE IF NOT EXISTS` leaves a table created at an earlier schema alone, so the columns
        declared since are read from `SCHEMA` itself (by creating it in a scratch database) and added
        here. A declared column that `ALTER TABLE ... ADD COLUMN` cannot add (one that is `NOT NULL`
        without a default, say) fails loudly at open rather than at the first read of the column.
        """
        for table, columns in declared_columns().items():
            present = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns:
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

    def query(self, sql: str, params=()) -> list[dict[str, Any]]:
        """Fetch a complete read under the shared connection lock."""
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

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

    def setting_record(self, key: str) -> dict[str, Any] | None:
        """The stored `value`, `actor` and `updated_at` of `key`; None when the store has no value."""
        row = self.row("settings", {"key": key})
        if row is None:
            return None
        return {"value": loads(row["value"]), "actor": row["actor"], "updated_at": row["updated_at"]}

    def settings(self) -> dict[str, dict[str, Any]]:
        return {
            row["key"]: {"value": loads(row["value"]), "actor": row["actor"], "updated_at": row["updated_at"]}
            for row in self.rows("settings", order="key")
        }

    # --- identity cache ---------------------------------------------------------------------------

    def cached_identities(self, namespace: str) -> list[dict[str, Any]]:
        """The `identity_cache` rows of `namespace` as `{key, value, verified_at}`, value decoded."""
        return [
            {"key": row["key"], "value": loads(row["value"]), "verified_at": row["verified_at"]}
            for row in self.rows("identity_cache", {"namespace": namespace}, order="key")
        ]

    def cache_identity(self, namespace: str, key: str, value: Any, verified_at: float) -> None:
        with self.transaction():
            self.execute(
                "INSERT INTO identity_cache (namespace, key, value, verified_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value, "
                "verified_at = excluded.verified_at",
                (namespace, key, dumps(value), verified_at),
            )

    def forget_identity(self, namespace: str, key: str) -> int:
        with self.transaction():
            return self.execute("DELETE FROM identity_cache WHERE namespace = ? AND key = ?", (namespace, key)).rowcount

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


def read_settings(path: str) -> dict[str, dict[str, Any]] | None:
    """The `settings` rows of the database at `path`, read without writing; None when there is no
    database or no `settings` table yet. For read-only commands such as `check-config`."""
    try:
        connection = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT key, value, actor, updated_at FROM settings ORDER BY key").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return {
        row["key"]: {"value": loads(row["value"]), "actor": row["actor"], "updated_at": row["updated_at"]}
        for row in rows
    }

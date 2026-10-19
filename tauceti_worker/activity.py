"""Local activity facts, incremental preparation, and strictly observation-only PR reads.

Readers only load published JSON. The preparer owns SQLite and never imports the
GitHub facade: a failed/missing shared-server observation cannot cause fallback.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import activity_ci, activity_costs, activity_summary
from .constants import WORK_TASKS
from .runtime_status import atomic_json, read_json

WINDOWS = {"3h": 10800, "24h": 86400, "7d": 604800, "total": None}
ROUND_TYPES = (*WORK_TASKS, "no-work")
_round = None


def root():
    return (
        Path(os.environ.get("TAUCETI_ACTIVITY_DIR", Path.home() / ".local/state/tauceti/activity"))
        .expanduser()
        .absolute()
    )


def pin_root():
    os.environ["TAUCETI_ACTIVITY_DIR"] = str(root())


def scope_path(kind, identity):
    return root() / kind / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def emit(kind, *, key=None, **fields):
    """One immutable receipt; concurrent workers never contend on a shared lock."""
    try:
        identity = key or uuid.uuid4().hex
        event = dict(
            version=1,
            id=identity,
            at=time.time(),
            kind=kind,
            worker=os.environ.get("TAUCETI_WORKER_ID", "default"),
            group=os.environ.get("TAUCETI_FLEET_MACHINE_ID", "local"),
        )
        event.update(fields)
        root().mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root() / "pending" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        atomic_json(path, event)
        path.chmod(0o600)
        return True
    except Exception:
        # Activity storage must not interrupt work or overwrite its failure reason.
        return False


def process_identity(pid):
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class Round:
    def __init__(self, worker):
        global _round
        self.id = uuid.uuid4().hex
        self.worker = worker
        self.done = False
        self.kind = "no-work"
        self.rc = 0
        self.started_at = time.time()
        self.deadline = float(os.environ.get("TAUCETI_ROUND_DEADLINE", "inf"))
        _round = self
        emit(
            "round_started",
            key=self.id + ":start",
            round_id=self.id,
            worker=worker,
            pid=os.getpid(),
            process_identity=process_identity(os.getpid()),
            hostname=socket.gethostname(),
        )

    def select(self, kind):
        self.kind = kind
        emit("round_selected", key=self.id + ":select", round_id=self.id, worker=self.worker, round_type=kind)

    def finish(self, exc=None):
        global _round
        if self.done:
            return
        self.done = True
        code = getattr(exc, "code", None) if exc is not None else self.rc
        if exc is not None and code is None:
            # NoProgress is intentionally separate from execution failures.
            outcome = "no-progress" if type(exc).__name__ == "NoProgress" else "failed"
        else:
            outcome = (
                "timeout"
                if code in (124, 137) or (code == 143 and time.time() >= self.deadline)
                else "interrupted"
                if code in (130, 143)
                else "no-progress"
                if code == 75
                else "failed"
                if code
                else "completed"
            )
        if type(exc).__name__ in ("Die", "NoProgress"):
            # Match cli_main's conversion of transport/quota failures to a wait.
            # This reads local instrumentation only, never GitHub.
            from .github_transport import blocking_event_since

            with contextlib.suppress(Exception):
                if blocking_event_since(self.started_at):
                    code = 77
        if code in (76, 77) or type(exc).__name__ in ("AdmissionUnavailable", "GitHubUnavailable"):
            # Waiting for permission to work is not a finished work round.
            emit("round_waited", key=self.id + ":wait", round_id=self.id, worker=self.worker)
            if _round is self:
                _round = None
            return
        emit(
            "round_finished",
            key=self.id + ":finish",
            round_id=self.id,
            worker=self.worker,
            round_type=self.kind,
            outcome=outcome,
        )
        if _round is self:
            _round = None


def select(kind):
    if _round is not None:
        _round.select(kind)


def current_round_id():
    return _round.id if _round is not None else None


def _connect():
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root() / "activity.sqlite", timeout=1)
    os.chmod(root() / "activity.sqlite", 0o600)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,worker TEXT,scope TEXT,kind TEXT,
        started REAL,finished REAL,outcome TEXT,pid INTEGER,identity TEXT,hostname TEXT);
      CREATE INDEX IF NOT EXISTS round_finish ON rounds(finished,kind);
      CREATE INDEX IF NOT EXISTS round_worker ON rounds(worker,finished);
      CREATE INDEX IF NOT EXISTS round_scope ON rounds(scope,finished);
      CREATE TABLE IF NOT EXISTS prs(repo TEXT,number INTEGER,worker TEXT,scope TEXT,created REAL,
        state TEXT,landed REAL,observed REAL,checked REAL,head TEXT,source TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER);
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TRIGGER IF NOT EXISTS activity_round_history_cutoff BEFORE INSERT ON rounds
        WHEN NEW.id LIKE 'resource:%' AND EXISTS(SELECT 1 FROM meta WHERE key='history_cutoff')
        BEGIN SELECT RAISE(IGNORE); END;
      CREATE TRIGGER IF NOT EXISTS activity_pr_history_cutoff BEFORE INSERT ON prs
        WHEN NEW.created < (SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff')
        BEGIN SELECT RAISE(IGNORE); END;
      CREATE TRIGGER IF NOT EXISTS activity_pr_history_cutoff_update AFTER UPDATE OF created ON prs
        WHEN NEW.created < (SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff')
        BEGIN DELETE FROM prs WHERE repo=NEW.repo AND number=NEW.number; END;
    """)
    activity_costs.install(db)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS pr_contributions(id TEXT PRIMARY KEY,repo TEXT,number INTEGER,
        worker TEXT,scope TEXT,at REAL,activity TEXT);
      CREATE INDEX IF NOT EXISTS contribution_scope ON pr_contributions(scope,at);
      CREATE INDEX IF NOT EXISTS contribution_worker ON pr_contributions(worker,at);
      CREATE TABLE IF NOT EXISTS pr_loc(repo TEXT,number INTEGER,head TEXT,added INTEGER,deleted INTEGER,
        observed REAL,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS pr_observations(repo TEXT,number INTEGER,state TEXT,created REAL,
        landed REAL,head TEXT,observed REAL,checked REAL,source TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS ci_activity(id TEXT PRIMARY KEY,scope TEXT,at REAL,
        conclusion TEXT,wait_seconds REAL,run_seconds REAL);
      CREATE INDEX IF NOT EXISTS ci_activity_scope ON ci_activity(scope,at);
    """)
    if db.execute("INSERT OR IGNORE INTO meta VALUES ('contribution_schema','1')").rowcount:
        # Earlier collectors advanced these journals without storing contribution
        # facts. Replay once; stable receipt IDs keep both streams idempotent.
        db.execute("DELETE FROM cursors WHERE path NOT LIKE '%/resources.jsonl'")
    return db


def _ingest(db, e):
    if e.get("version") != 1 or not isinstance(e.get("at"), (int, float)):
        raise ValueError("invalid activity receipt")
    if e.get("round_id", "").startswith("resource:"):
        return
    cutoff = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    if cutoff and e["at"] < cutoff[0]:
        return
    kind, worker, scope = e["kind"], e.get("worker", "unknown"), e.get("group", e.get("machine_id", "local"))
    if kind == "pr_activity":
        db.execute(
            "INSERT OR IGNORE INTO pr_contributions VALUES (?,?,?,?,?,?,?)",
            (e["id"], e["repository"], e["pr"], worker, scope, e["at"], e.get("activity", "work")),
        )
    if not db.execute("INSERT OR IGNORE INTO events VALUES (?)", (e["id"],)).rowcount:
        return
    if kind.startswith("round_"):
        rid = e["round_id"]
        db.execute("INSERT OR IGNORE INTO rounds(id,worker,scope,kind) VALUES (?,?,?,'unknown')", (rid, worker, scope))
        if kind == "round_started":
            db.execute(
                "UPDATE rounds SET started=?,pid=?,identity=?,hostname=? WHERE id=?",
                (e["at"], e.get("pid"), e.get("process_identity"), e.get("hostname"), rid),
            )
        elif kind == "round_selected":
            db.execute("UPDATE rounds SET kind=? WHERE id=?", (e["round_type"], rid))
        elif kind == "round_finished":
            db.execute(
                "UPDATE rounds SET kind=?,finished=?,outcome=? WHERE id=?",
                (e["round_type"], e["at"], e["outcome"], rid),
            )
        elif kind == "round_waited":
            db.execute("UPDATE rounds SET outcome='waiting' WHERE id=?", (rid,))
    elif kind == "pr_created":
        db.execute(
            "INSERT OR IGNORE INTO prs(repo,number,worker,scope,created) VALUES (?,?,?,?,?)",
            (e["repository"], e["pr"], worker, scope, e["at"]),
        )

    elif kind == "ci_result":
        db.execute(
            "INSERT OR IGNORE INTO ci_activity VALUES (?,?,?,?,?,?)",
            (e["id"], scope, e["at"], e.get("conclusion"), e.get("wait_seconds"), e.get("run_seconds")),
        )


def _consume(db, errors):
    for path in sorted((root() / "pending").glob("*.json")):
        try:
            with db:
                _ingest(db, json.loads(path.read_text()))
            path.unlink()
        except (OSError, ValueError, KeyError, sqlite3.Error):
            errors.append("Invalid or unreadable activity receipt: " + path.name)
    # A SIGKILL cannot emit its own finish. Keep these separate from finished
    # rounds: the time of discovery is not a trustworthy completion timestamp.
    for row in db.execute("SELECT * FROM rounds WHERE finished IS NULL AND outcome IS NULL AND pid IS NOT NULL"):
        if row["hostname"] != socket.gethostname():
            continue
        alive = True
        try:
            os.kill(row["pid"], 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            pass
        if row["identity"] and process_identity(row["pid"]) not in (None, row["identity"]):
            alive = False
        if not alive:
            db.execute("UPDATE rounds SET outcome='interrupted-unfinished' WHERE id=?", (row["id"],))


def backfill_file(db, path, convert):
    """Explicit, incremental import of structured history, never transcripts."""
    if not path.exists():
        return
    stat = path.stat()
    old = db.execute("SELECT inode,offset FROM cursors WHERE path=?", (str(path),)).fetchone()
    offset = old[1] if old and old[0] == stat.st_ino and old[1] <= stat.st_size else 0
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            before = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(before)
                break
            for event in convert(json.loads(line)):
                _ingest(db, event)
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (str(path), stat.st_ino, stream.tell()))


def backfill(db, history_roots=()):
    # Resource logs predate typed activity recording and are intentionally ignored.
    # Keep history_roots accepted for callers using the former backfill interface.
    paths = {Path(row[0]) for row in db.execute("SELECT path FROM cursors") if Path(row[0]).name != "resources.jsonl"}
    paths.add(
        Path(os.environ.get("TAUCETI_FLEET_EVENTS", Path.home() / ".local/state/tauceti-fleet/worker-events.jsonl"))
    )
    for path in sorted(paths):
        backfill_file(db, path, lambda e: [e] if e.get("kind") in ("pr_created", "pr_activity", "ci_result") else [])


def _clean_history(db):
    # Pin the start once, using the first native lifecycle receipt. Preserve event
    # deduplication tombstones so replaying a journal cannot restore discarded facts.
    db.execute("""
      INSERT OR IGNORE INTO meta(key,value)
      SELECT 'history_cutoff', MIN(COALESCE(started,finished)) FROM rounds
      WHERE id NOT LIKE 'resource:%' HAVING MIN(COALESCE(started,finished)) IS NOT NULL
    """)
    db.execute("DELETE FROM rounds WHERE id LIKE 'resource:%'")
    cutoff = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    if cutoff:
        db.execute("DELETE FROM rounds WHERE COALESCE(started,finished) < ?", (cutoff[0],))
        db.execute("DELETE FROM prs WHERE created < ?", (cutoff[0],))
        db.execute("DELETE FROM pr_contributions WHERE at < ?", (cutoff[0],))
        db.execute("DELETE FROM ci_activity WHERE at < ?", (cutoff[0],))
    db.execute("DELETE FROM cursors WHERE path LIKE '%/resources.jsonl'")


def _epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def observe(url, token_file, repository, numbers):
    """Only this endpoint is allowed. No shared query facade, retry, or redirect."""
    from urllib.parse import urlsplit

    u = urlsplit(url)
    if u.hostname in ("api.github.com", "github.com", "www.github.com"):
        raise ValueError("Activity reporting requires the observation server, never GitHub")
    if (
        (u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")))
        or u.username
        or u.password
        or u.query
        or u.fragment
    ):
        raise ValueError("invalid observation server URL")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    token = Path(token_file).expanduser().read_text().strip()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/activity",
        data=json.dumps(dict(repository=repository, numbers=numbers)).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
        },
    )
    try:
        response = urllib.request.build_opener(NoRedirect).open(req, timeout=5)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    with response:
        raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise ValueError("observation response too large")
        result = json.loads(raw)
    if result.get("version") != 1 or result.get("repository") != repository or result.get("coverage") != "observed":
        raise ValueError("invalid observation response")
    return result


def _refresh_prs(db, config, now, errors):
    from .worker_manager import load_worker_specs

    specs = {s.id: s for s in load_worker_specs(config)} if config else {}
    tracked = {
        (r["repo"], r["number"]): r["worker"] for r in db.execute("SELECT repo,number,worker FROM pr_contributions")
    }
    owned = {(r["repo"], r["number"]): dict(r) for r in db.execute("SELECT * FROM prs")}
    tracked.update({key: p["worker"] for key, p in owned.items()})
    for repo, number in tracked:
        db.execute("INSERT OR IGNORE INTO pr_observations(repo,number) VALUES (?,?)", (repo, number))
    batches = {}
    for h in db.execute("SELECT * FROM pr_observations"):
        key = (h["repo"], h["number"])
        if key not in tracked:
            continue
        p = owned.get(key, h)
        checked = max(h["checked"] or 0, p["checked"] or 0)
        loc = db.execute("SELECT head FROM pr_loc WHERE repo=? AND number=?", key).fetchone()
        if checked > now - 300 or (p["landed"] is not None and loc and loc[0] == p["head"]):
            continue
        spec = specs.get(tracked[key])
        url = getattr(spec, "github_state_url", None) or os.environ.get("TAUCETI_GITHUB_STATE_URL")
        token = getattr(spec, "github_state_token_file", None) or os.environ.get("TAUCETI_GITHUB_STATE_TOKEN_FILE")
        if url and token:
            batches.setdefault((url, str(token), h["repo"]), []).append(h["number"])
    prior = db.execute("SELECT value FROM meta WHERE key='observation_errors'").fetchone()
    observation_errors = json.loads(prior[0]) if prior else {}
    deadline = time.monotonic() + 10
    for (url, token, repo), numbers in batches.items():
        observation_errors.pop(repo, None)
        for offset in range(0, len(numbers), 100):
            if time.monotonic() >= deadline:
                errors.append("Remaining shared PR observations deferred to the next preparation pass")
                break
            batch = numbers[offset : offset + 100]
            try:
                value = observe(url, token, repo, batch)
                if value.get("landing_observations", {}).get("error"):
                    observation_errors[repo] = "Stored Bors landing observations unavailable; retaining last-known data"
                seen = [p["number"] for p in value["prs"]] + value["missing"]
                if len(seen) != len(set(seen)) or set(seen) != set(batch):
                    raise ValueError("incomplete observation response")
                for p in value["prs"]:
                    if p["number"] not in batch or p.get("state") not in ("OPEN", "CLOSED", "MERGED"):
                        raise ValueError("invalid PR observation")
                    fields = (
                        p["state"],
                        _epoch(p.get("landed_at")),
                        p["observed_at"] / 1000,
                        now,
                        p.get("head_sha"),
                        p.get("landing_source"),
                        _epoch(p.get("created_at")),
                        repo,
                        p["number"],
                    )
                    db.execute(
                        "UPDATE prs SET state=?,landed=COALESCE(?,landed),observed=?,checked=?,head=?,source=COALESCE(?,source),created=COALESCE(?,created) WHERE repo=? AND number=?",
                        fields,
                    )
                    db.execute(
                        "UPDATE pr_observations SET state=?,landed=COALESCE(?,landed),observed=?,checked=?,head=?,source=COALESCE(?,source),created=COALESCE(?,created) WHERE repo=? AND number=?",
                        fields,
                    )
                    loc = p.get("lean_loc")
                    if loc is not None:
                        if loc.get("head_sha") != p.get("head_sha") or not all(
                            type(loc.get(k)) is int and loc[k] >= 0 for k in ("added", "deleted")
                        ):
                            raise ValueError("invalid LOC observation")
                        db.execute(
                            "INSERT OR REPLACE INTO pr_loc VALUES (?,?,?,?,?,?)",
                            (repo, p["number"], loc["head_sha"], loc["added"], loc["deleted"], p["observed_at"] / 1000),
                        )
                    else:
                        db.execute("DELETE FROM pr_loc WHERE repo=? AND number=?", (repo, p["number"]))
            except Exception:
                observation_errors[repo] = (
                    "Shared PR observations unavailable for " + repo + "; retaining last-known data"
                )
            # Missing resources and failures are observed no more frequently than successful reads.
            db.executemany("UPDATE prs SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch])
            db.executemany(
                "UPDATE pr_observations SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch]
            )
    db.execute("INSERT OR REPLACE INTO meta VALUES ('observation_errors',?)", (json.dumps(observation_errors),))
    errors.extend(observation_errors.values())


def _bors(db, errors):
    path = Path(
        os.environ.get("TAUCETI_ACTIVITY_BORS_DB", Path.home() / ".local/state/tauceti-fleet/tauceti-ci.sqlite")
    )
    if not path.is_file():
        return
    try:
        pending = [
            tuple(row)
            for row in db.execute(
                "SELECT repo,number,head FROM prs WHERE landed IS NULL AND head IS NOT NULL ORDER BY repo,number"
            )
        ]
        stamp = json.dumps([str(path), path.stat().st_mtime_ns, pending])
        old = db.execute("SELECT value FROM meta WHERE key='bors_input'").fetchone()
        if not pending or (old and old[0] == stamp):
            return
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            for repo, members, at in source.execute(
                "SELECT b.repo,b.members_json,l.landed_at FROM merge_builds b JOIN main_landings l ON l.repo=b.repo AND l.head_sha=b.head_sha WHERE b.landed=1"
            ):
                repo = repo if "/" in repo else "TauCetiProject/" + repo
                for member in json.loads(members or "[]"):
                    if member.get("head_sha"):
                        db.execute(
                            "UPDATE prs SET landed=?,source='bors-main-evidence' WHERE repo=? AND number=? AND head=? AND landed IS NULL",
                            (_epoch(at), repo, member.get("pr"), member["head_sha"]),
                        )
        db.execute("INSERT OR REPLACE INTO meta VALUES ('bors_input',?)", (stamp,))
    except (OSError, ValueError, sqlite3.Error):
        errors.append("Existing Bors landing evidence is unreadable")


def _worker_scope(db, worker):
    row = db.execute("SELECT scope FROM rounds WHERE worker=? ORDER BY started DESC LIMIT 1", (worker,)).fetchone()
    if row:
        return row[0]
    machine = os.environ.get("TAUCETI_FLEET_MACHINE_ID") or read_json(
        Path.home() / ".config/tauceti-fleet/host.json"
    ).get("machine_id")
    return machine if machine and worker.startswith("fleet-" + machine[:8] + "-") else "local"


def _summary(db, now, column=None, identity=None):
    where, args = (f"WHERE {column}=?", [identity]) if column else ("", [])
    rounds = {}
    for label, seconds in WINDOWS.items():
        condition = "finished<=?" if seconds is None else "finished>=? AND finished<=?"
        times = [now] if seconds is None else [now - seconds, now]
        rows = db.execute(
            f"SELECT kind,COUNT(*) n,SUM(outcome IN ('failed','timeout','interrupted')) failures,SUM(outcome='unknown') unknown FROM rounds {where} {'AND' if where else 'WHERE'} {condition} GROUP BY kind",
            args + times,
        )
        rounds[label] = {r["kind"]: dict(finished=r["n"], failed=r["failures"], unknown=r["unknown"]) for r in rows}
    prs = [dict(r) for r in db.execute(f"SELECT * FROM prs {where}", args)]
    first_round = db.execute(f"SELECT MIN(COALESCE(started,finished)) FROM rounds {where}", args).fetchone()[0]
    first_pr = min((p["created"] for p in prs), default=None)
    since = min((v for v in (first_round, first_pr) if v is not None), default=None)
    pr_windows = {
        label: dict(
            authored=sum((seconds is None or p["created"] >= now - seconds) and p["created"] <= now for p in prs),
            landed=sum(
                p["landed"] is not None and (seconds is None or p["landed"] >= now - seconds) and p["landed"] <= now
                for p in prs
            ),
        )
        for label, seconds in WINDOWS.items()
    }
    native_since = db.execute(f"SELECT MIN(started) FROM rounds {where}", args).fetchone()[0]
    cutoff_row = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    cutoff = cutoff_row[0] if cutoff_row else since
    costs = activity_costs.summarize(db, now, cutoff, column, identity)
    locs = {(r["repo"], r["number"]): dict(r) for r in db.execute("SELECT * FROM pr_loc")}
    for window, seconds in activity_costs.COST_WINDOWS.items():
        begin = max(cutoff or 0, now - seconds) if seconds is not None else cutoff or 0
        cohort = [p for p in prs if begin <= p["created"] <= now]
        landed = [p for p in cohort if p["landed"] is not None and p["landed"] <= now]
        opened = [p for p in cohort if p["state"] == "OPEN" and p["landed"] is None]

        def measured(p):
            loc = locs.get((p["repo"], p["number"]))
            return loc if loc and loc["head"] == p["head"] else None

        landed_loc = sum((measured(p) or {}).get("added", 0) for p in landed)
        open_loc = sum((measured(p) or {}).get("added", 0) for p in opened)
        missing = sum(measured(p) is None for p in [*landed, *opened]) + sum(
            p["state"] is None and p["landed"] is None for p in cohort
        )
        row = costs["windows"][window]
        row.update(
            landed_loc_added=landed_loc,
            open_loc_added=open_loc,
            landed_prs=len(landed),
            open_prs=len(opened),
            missing_loc_prs=missing,
            known_loc_prs=len(landed) + len(opened) - sum(measured(p) is None for p in [*landed, *opened]),
            measured_pr_weight=sum(measured(p) is not None for p in landed)
            + sum(measured(p) is not None for p in opened) / 2,
        )
        row.update(activity_summary.loc_estimate(row))
    contributions = [dict(r) for r in db.execute(f"SELECT * FROM pr_contributions {where}", args)]
    owned_keys = {(p["repo"], p["number"]) for p in prs}
    for window, seconds in WINDOWS.items():
        begin = float("-inf") if seconds is None else now - seconds
        others = [c for c in contributions if begin <= c["at"] <= now and (c["repo"], c["number"]) not in owned_keys]
        meaningful = [
            c
            for c in others
            if c["activity"] in ("review", "fix", "fix-ci", "rebase", "bump", "lint-repair", "branch_push")
        ]
        pr_windows[window].update(
            assisted=len({(c["repo"], c["number"]) for c in meaningful}),
            reviews=db.execute(
                f"SELECT COUNT(*) FROM rounds {where} {'AND' if where else 'WHERE'} kind='review' AND outcome='completed' AND finished>=? AND finished<=?",
                [*args, begin, now],
            ).fetchone()[0],
            fixes=sum(c["activity"] in ("fix", "fix-ci", "rebase", "bump", "lint-repair") for c in others),
        )
    return dict(
        prepared_at=now,
        source_id=identity if column else "local",
        history_since=since,
        rounds=rounds,
        round_types=list(ROUND_TYPES),
        pr_windows=pr_windows,
        costs=costs,
        ci_windows=activity_ci.summarize(db, now, WINDOWS, column, identity),
        running=(
            db.execute(
                f"SELECT COUNT(DISTINCT worker) FROM rounds {where} {'AND' if where else 'WHERE'} finished IS NULL AND outcome IS NULL",
                args,
            ).fetchone()[0]
            if native_since is not None
            else None
        ),
        interrupted_unfinished=db.execute(
            f"SELECT COUNT(*) FROM rounds {where} {'AND' if where else 'WHERE'} outcome='interrupted-unfinished'", args
        ).fetchone()[0],
        open_prs=sum(p["state"] == "OPEN" and p["landed"] is None for p in prs),
        unknown_prs=sum(p["state"] is None for p in prs),
        unknown_landings=sum(p["state"] == "MERGED" and p["landed"] is None for p in prs),
        pr_observed_at=min((p["observed"] for p in prs if p["observed"]), default=None),
        pr_checked_at=min((p["checked"] for p in prs if p["checked"]), default=None),
    )


def prepare(config=None, *, import_history=False, history_roots=(), now=None):
    """Explicit/background preparation. The CLI's normal read never calls this."""
    import fcntl

    now = time.time() if now is None else now
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root() / "prepare.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # Another manager/timer is already preparing this cache.
        with contextlib.closing(_connect()) as db:
            errors = []
            with db:
                # Follow registered PR journals by offset; resource logs are excluded.
                if import_history or db.execute("SELECT 1 FROM cursors LIMIT 1").fetchone():
                    try:
                        backfill(db, history_roots)
                    except (OSError, ValueError, KeyError):
                        errors.append("PR creation receipt is unreadable")
                _consume(db, errors)
                _clean_history(db)
                machine = os.environ.get("TAUCETI_FLEET_MACHINE_ID") or read_json(
                    Path.home() / ".config/tauceti-fleet/host.json"
                ).get("machine_id")
                try:
                    activity_ci.prepare(db, now, machine)
                except (OSError, ValueError, sqlite3.Error):
                    errors.append("Local CI history preparation failed; retaining last-known data")
                try:
                    activity_costs.prepare(db, now, lambda worker: _worker_scope(db, worker))
                except (OSError, ValueError, KeyError) as error:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_cost_error',?)", (type(error).__name__,))
                    errors.append("Conversation cost preparation failed; retaining last-known budget data")
                _refresh_prs(db, config, now, errors)
                _bors(db, errors)
            from . import shared_mathlib

            sharing = shared_mathlib.summary()
            value = dict(version=1, prepared_at=now, errors=errors, all=_summary(db, now), workers={}, groups={})
            value["all"]["shared_mathlib"] = sharing
            for column, key in (("worker", "workers"), ("scope", "groups")):
                ids = db.execute(
                    f"SELECT {column} FROM rounds UNION SELECT {column} FROM prs UNION SELECT {column} FROM pr_contributions UNION SELECT {column} FROM conversation_costs"
                    + (" UNION SELECT scope FROM ci_activity" if column == "scope" else "")
                ).fetchall()
                for row in ids:
                    value[key][row[0]] = _summary(db, now, column, row[0])
                    value[key][row[0]]["shared_mathlib"] = shared_mathlib.summary(
                        **{"scope" if key == "groups" else "worker": row[0]}
                    )
                    atomic_json(
                        scope_path(key, row[0]), dict(version=1, prepared_at=now, all=value[key][row[0]], errors=errors)
                    )
            # Detail is separate; status readers never transfer a growing PR inventory.
            atomic_json(
                root() / "prs.json",
                dict(version=1, prepared_at=now, prs=[dict(r) for r in db.execute("SELECT * FROM prs")]),
            )
            atomic_json(root() / "summary.json", {k: v for k, v in value.items() if k not in ("workers", "groups")})
            return value


def cached(*, worker=None, group=None):
    path = (
        scope_path("workers", worker) if worker else scope_path("groups", group) if group else root() / "summary.json"
    )
    value = read_json(path)
    if not value:
        return dict(available=False, reason="Activity not prepared; run tauceti activity refresh")
    summary = value.get("all")
    errors = list(value.get("errors", []))
    failure = read_json(root() / "preparation-error.json")
    if failure.get("at", 0) > value.get("prepared_at", 0):
        errors.append("Background preparation failed; retaining last-known summary (" + failure["error"] + ")")
    return dict(
        available=summary is not None, version=1, summary=summary, errors=errors, prepared_at=value.get("prepared_at")
    )


def display(value):
    activity_summary.display(value, ROUND_TYPES)


def start_collector(config):
    """Only the existing manager owns this thread; no additional service needed."""
    pin_root()
    stopping = threading.Event()

    def run():
        while not stopping.is_set():
            try:
                prepare(config)
            except Exception as error:
                with contextlib.suppress(Exception):
                    atomic_json(root() / "preparation-error.json", dict(at=time.time(), error=type(error).__name__))
            stopping.wait(60)

    thread = threading.Thread(target=run, name="activity-preparation", daemon=True)
    thread.start()
    return stopping


def command(args):
    if args.config is None:
        from .worker_manager import default_workers_config

        path = default_workers_config()
        args.config = path if path.exists() else None
    if args.action == "install-timer":
        install_timer(args.config)
    if args.action == "refresh":
        prepare(args.config, import_history=args.backfill, history_roots=args.history_root)
    if args.inventory:
        value = read_json(root() / "prs.json")
        if args.worker or args.group:
            value["prs"] = [
                p
                for p in value.get("prs", [])
                if (not args.worker or p["worker"] == args.worker) and (not args.group or p["scope"] == args.group)
            ]
    else:
        value = cached(worker=args.worker, group=args.group)
    if args.json or args.inventory:
        print(json.dumps(value, indent=2))
    else:
        display(value)
    return 0


def install_timer(config=None):
    """Optional preparation for installations without a running worker manager."""
    import subprocess

    from .paths import self_argv, self_env

    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    argv = self_argv("activity", "refresh") + (["--config", str(config)] if config else [])

    def unit(value):
        return json.dumps(str(value).replace("%", "%%"))

    environment = self_env()
    extra = "".join(
        "Environment=" + unit(key + "=" + os.environ[key]) + "\n"
        for key in ("TAUCETI_GITHUB_STATE_URL", "TAUCETI_GITHUB_STATE_TOKEN_FILE", "TAUCETI_ACTIVITY_BORS_DB")
        if os.environ.get(key)
    )
    (directory / "tauceti-activity.service").write_text(
        "[Unit]\nDescription=Prepare cached TauCeti activity (no upstream GitHub reads)\n"
        "[Service]\nType=oneshot\nNice=10\nExecStart="
        + " ".join(map(unit, argv))
        + "\n"
        + "Environment="
        + unit("TAUCETI_ACTIVITY_DIR=" + str(root()))
        + "\n"
        + "Environment="
        + unit("PYTHONPATH=" + environment.get("PYTHONPATH", ""))
        + "\n"
        + extra
    )
    (directory / "tauceti-activity.timer").write_text(
        "[Unit]\nDescription=Prepare TauCeti activity every minute\n[Timer]\nOnBootSec=30\nOnUnitInactiveSec=60\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "tauceti-activity.timer"],
    ):
        subprocess.run(argv, check=True, timeout=15, capture_output=True)
    print("Installed tauceti-activity.timer; no GitHub API polling is performed.")

"""Local activity facts, incremental preparation, and strictly observation-only PR reads.

Readers only load published JSON. The preparer owns SQLite and never imports the
GitHub facade: a failed/missing shared-server observation cannot cause fallback.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import activity_attribution, activity_ci, activity_costs, activity_git, activity_summary
from .constants import WORK_TASKS
from .runtime_status import atomic_json, read_json

WINDOWS = activity_summary.WINDOW_SECONDS
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
        self.observation = None
        os.environ["TAUCETI_ACTIVITY_ROUND_ID"] = self.id
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
        self.observation = None
        emit(
            "round_selected",
            key=self.id + ":select",
            round_id=self.id,
            worker=self.worker,
            round_type=kind,
            review_archive=str(activity_attribution.archive_root()) if kind == "review" else None,
            review_outbox=str(activity_attribution.archive_outbox(self.worker)) if kind == "review" else None,
            **{k: v for k, v in activity_attribution.invocation_context(kind).items() if k != "round_id"},
        )

    def finish(self, exc=None):
        global _round
        if self.done:
            return
        self.done = True
        if os.environ.get("TAUCETI_ACTIVITY_ROUND_ID") == self.id:
            os.environ.pop("TAUCETI_ACTIVITY_ROUND_ID", None)
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
        if code in (76, 77, 79, 80, 81) or type(exc).__name__ in (
            "ClaimCollision",
            "AdmissionUnavailable",
            "DocumentationUnavailable",
            "ProgressPreparationUnavailable",
            "GitHubUnavailable",
            "SharedStateUnavailable",
        ):
            # Waiting for permission to work is not a finished work round.
            failure = getattr(exc, "failure", None) or {}
            dependency = {k: failure[k] for k in ("dependency", "reason") if failure.get(k)}
            emit("round_waited", key=self.id + ":wait", round_id=self.id, worker=self.worker, **dependency)
            if _round is self:
                _round = None
            return
        emit(
            "round_finished",
            key=self.id + ":finish",
            round_id=self.id,
            worker=self.worker,
            round_type=self.kind,
            outcome=self.observation if outcome == "completed" and self.observation else outcome,
        )
        if _round is self:
            _round = None


def select(kind):
    if _round is not None:
        _round.select(kind)


def observation(outcome):
    if _round is not None:
        _round.observation = outcome


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
      CREATE TABLE IF NOT EXISTS round_publications(round_id TEXT,worker TEXT,scope TEXT,publication TEXT,
        PRIMARY KEY(round_id,worker,scope,publication));
      CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,worker TEXT,scope TEXT,kind TEXT,
        started REAL,finished REAL,outcome TEXT,pid INTEGER,identity TEXT,hostname TEXT);
      DROP INDEX IF EXISTS round_finish_covering;
      DROP INDEX IF EXISTS round_finish;
      DROP INDEX IF EXISTS round_worker_covering;
      DROP INDEX IF EXISTS round_worker;
      DROP INDEX IF EXISTS round_scope_covering;
      DROP INDEX IF EXISTS round_scope;
      CREATE INDEX IF NOT EXISTS round_interrupted ON rounds(outcome,started DESC,id DESC,worker,scope)
        WHERE outcome='interrupted-unfinished';
      CREATE TABLE IF NOT EXISTS prs(repo TEXT,number INTEGER,worker TEXT,scope TEXT,created REAL,
        state TEXT,landed REAL,observed REAL,checked REAL,head TEXT,source TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER);
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TRIGGER IF NOT EXISTS activity_round_history_cutoff BEFORE INSERT ON rounds
        WHEN NEW.id LIKE 'resource:%' AND EXISTS(SELECT 1 FROM meta WHERE key='history_cutoff')
        BEGIN SELECT RAISE(IGNORE); END;
      DROP TRIGGER IF EXISTS activity_pr_history_cutoff;
      DROP TRIGGER IF EXISTS activity_pr_history_cutoff_update;
    """)
    activity_costs.install(db)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS pr_contributions(id TEXT PRIMARY KEY,repo TEXT,number INTEGER,
        worker TEXT,scope TEXT,at REAL,activity TEXT);
      DROP INDEX IF EXISTS contribution_scope_covering;
      DROP INDEX IF EXISTS contribution_scope;
      DROP INDEX IF EXISTS contribution_worker_covering;
      DROP INDEX IF EXISTS contribution_worker;
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
    if db.execute("INSERT OR IGNORE INTO meta VALUES ('coordination_journal_schema','1')").rowcount:
        # Older preparers advanced these journals while filtering review starts out.
        # Replay once; stable event IDs keep already-counted activity idempotent.
        db.execute("DELETE FROM cursors WHERE path NOT LIKE '%/resources.jsonl'")
    if db.execute("INSERT OR IGNORE INTO meta VALUES ('landing_window_schema','1')").rowcount:
        # Replay retained creation journals to recover identities discarded by
        # the former creation cutoff, without losing registered journal paths.
        db.execute("UPDATE cursors SET offset=0 WHERE path NOT LIKE '%/resources.jsonl'")
    from .activity_rollup_sources import install as install_rollups

    install_rollups(db)
    activity_attribution.install(db)
    return db


def _reconcile_publication(db, e, worker, scope):
    """Keep positive publication evidence across receipt ordering and restarts.

    Only matching, normally finished rounds can be reconciled. A push or closure
    proves a repair outcome; creation proves authorship. Replies alone cannot
    prove a repair that required a push. Never invent a completion timestamp or
    turn an execution failure into a successful round.
    """
    rid = e.get("round_id")
    if not rid:
        return
    publication = (
        "creation"
        if e["kind"] == "pr_created"
        else "push"
        if e["kind"] == "pr_activity" and e.get("activity") == "branch_push"
        else "closure"
        if e["kind"] == "pr_activity" and e.get("publication") == "closure"
        else None
    )
    if publication:
        db.execute("INSERT OR IGNORE INTO round_publications VALUES (?,?,?,?)", (rid, worker, scope, publication))
    if publication or e["kind"].startswith("round_"):
        db.execute(
            """UPDATE rounds SET outcome='completed'
               WHERE id=? AND worker=? AND scope=? AND finished IS NOT NULL
                 AND outcome IN ('unknown','unconfirmed')
                 AND EXISTS(SELECT 1 FROM round_publications p
                   WHERE p.round_id=rounds.id AND p.worker=rounds.worker AND p.scope=rounds.scope
                     AND ((p.publication='creation' AND rounds.kind IN ('roadmap','bump'))
                       OR (p.publication IN ('push','closure') AND rounds.kind IN
                         ('fix','fix-ci','rebase','bump','lint-repair','overlap-fix'))))""",
            (rid, worker, scope),
        )


def _ingest(db, e):
    if e.get("version") != 1 or not isinstance(e.get("at"), (int, float)):
        raise ValueError("invalid activity receipt")
    if e.get("round_id", "").startswith("resource:"):
        return
    cutoff = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    if cutoff and e["at"] < cutoff[0] and e.get("kind") != "pr_created":
        return
    kind, worker, scope = e["kind"], e.get("worker", "unknown"), e.get("group", e.get("machine_id", "local"))
    activity_attribution.ingest(db, e)
    if kind == "pr_created":
        # Replays must restore PRs removed by older collectors even when the
        # event's deduplication tombstone already exists.
        db.execute(
            "INSERT OR IGNORE INTO prs(repo,number,worker,scope,created) VALUES (?,?,?,?,?)",
            (e["repository"], e["pr"], worker, scope, e["at"]),
        )
    if kind == "pr_activity":
        db.execute(
            "INSERT OR IGNORE INTO pr_contributions VALUES (?,?,?,?,?,?,?)",
            (e["id"], e["repository"], e["pr"], worker, scope, e["at"], e.get("activity", "work")),
        )
    if not db.execute("INSERT OR IGNORE INTO events VALUES (?)", (e["id"],)).rowcount:
        _reconcile_publication(db, e, worker, scope)
        return
    if kind == "review_coordination":
        row = db.execute("SELECT value FROM meta WHERE key='local_coordination'").fetchone()
        buckets = json.loads(row[0]) if row else {}
        hour = str(int(e["at"] // 3600) * 3600)
        bucket = buckets.setdefault(hour, dict(started=0, git_held=0))
        bucket["started"] += 1
        bucket["git_held"] += bool(e.get("git_held"))
        buckets = {k: v for k, v in buckets.items() if float(k) >= e["at"] - 7 * 86400 - 3600}
        db.execute("INSERT OR REPLACE INTO meta VALUES ('local_coordination',?)", (json.dumps(buckets),))
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
    elif kind == "ci_result":
        db.execute(
            "INSERT OR IGNORE INTO ci_activity VALUES (?,?,?,?,?,?)",
            (e["id"], scope, e["at"], e.get("conclusion"), e.get("wait_seconds"), e.get("run_seconds")),
        )

    _reconcile_publication(db, e, worker, scope)


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
        backfill_file(
            db,
            path,
            lambda e: [e] if e.get("kind") in ("pr_created", "pr_activity", "ci_result", "review_coordination") else [],
        )


def _clean_history(db):
    # Pin the start once, using the first native lifecycle receipt. Preserve event
    # deduplication tombstones so replaying a journal cannot restore discarded facts.
    if not db.execute("SELECT 1 FROM meta WHERE key='history_cutoff'").fetchone():
        db.execute("""
          INSERT OR IGNORE INTO meta(key,value)
          SELECT 'history_cutoff', MIN(COALESCE(started,finished)) FROM rounds
          WHERE id NOT LIKE 'resource:%' HAVING MIN(COALESCE(started,finished)) IS NOT NULL
        """)
    db.execute("DELETE FROM rounds WHERE id>='resource:' AND id<'resource;'")
    cutoff = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    if cutoff:
        db.execute("DELETE FROM rounds WHERE COALESCE(started,finished) < ?", (cutoff[0],))
        # Older PRs may still land inside a reporting window. Keep their identity
        # and refresh observations; only authorship counts use the history cutoff.
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
    # Indexed due work replaces enumerating every PR and contribution receipt.
    due = db.execute(
        "SELECT repo,number,worker FROM report_pr_refresh WHERE next_at<=? ORDER BY next_at LIMIT 1000", (now,)
    ).fetchall()
    batches = {}
    for repo, number, worker in due:
        key = (repo, number)
        db.execute("INSERT OR IGNORE INTO pr_observations(repo,number) VALUES (?,?)", key)
        p = db.execute("SELECT * FROM prs WHERE repo=? AND number=?", key).fetchone()
        h = db.execute("SELECT * FROM pr_observations WHERE repo=? AND number=?", key).fetchone()
        p = p or h
        checked = max(h["checked"] or 0, p["checked"] or 0)
        loc = db.execute("SELECT head FROM pr_loc WHERE repo=? AND number=?", key).fetchone()
        counts = db.execute(
            "SELECT head,count,checked,observed FROM pr_review_counts WHERE repo=? AND number=?", key
        ).fetchone()
        landed_loc = p["landed"] is not None and loc and loc[0] == p["head"]
        settled = (
            landed_loc
            and counts
            and counts[0] == p["head"]
            and counts[1] is not None
            and counts[3] is not None
            and counts[2] >= p["landed"]
        )
        expired = bool(landed_loc and counts and counts[2] >= p["landed"] and now > p["landed"] + 7 * 86400)
        db.execute(
            "UPDATE report_pr_refresh SET next_at=? WHERE repo=? AND number=?",
            (
                None
                if settled or expired
                else max(now, checked) + (3600 if landed_loc and counts and counts[2] >= p["landed"] else 300),
                *key,
            ),
        )
        if settled or expired or checked > now - 300:
            continue
        spec = specs.get(worker)
        url = getattr(spec, "github_state_url", None) or os.environ.get("TAUCETI_GITHUB_STATE_URL")
        token = getattr(spec, "github_state_token_file", None) or os.environ.get("TAUCETI_GITHUB_STATE_TOKEN_FILE")
        if url and token:
            batches.setdefault((url, str(token), repo), []).append(number)
    prior = db.execute("SELECT value FROM meta WHERE key='observation_errors'").fetchone()
    observation_errors = json.loads(prior[0]) if prior else {}
    # A removed/settled repository will never have another observation attempt
    # to clear its old error. Retain failures only while refresh work remains.
    observation_errors = {
        repo: error
        for repo, error in observation_errors.items()
        if db.execute(
            "SELECT 1 FROM report_pr_refresh WHERE repo=? AND next_at IS NOT NULL LIMIT 1", (repo,)
        ).fetchone()
    }
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
                if value.get("review_coordination"):
                    db.execute(
                        "INSERT OR REPLACE INTO meta VALUES (?,?)",
                        ("coordination:" + repo, json.dumps(value["review_coordination"])),
                    )
                if value.get("landing_observations", {}).get("error"):
                    observation_errors[repo] = "Stored Bors landing observations unavailable; retaining last-known data"
                seen = [p["number"] for p in value["prs"]] + value["missing"]
                if len(seen) != len(set(seen)) or set(seen) != set(batch):
                    raise ValueError("incomplete observation response")
                if any(
                    p["number"] not in batch or p.get("state") not in ("OPEN", "CLOSED", "MERGED") for p in value["prs"]
                ):
                    raise ValueError("invalid PR observation")
                stored_locs = {}
                for p in value["prs"]:
                    loc = db.execute(
                        "SELECT head,added,deleted FROM pr_loc WHERE repo=? AND number=?", (repo, p["number"])
                    ).fetchone()
                    if loc and loc[0] == p.get("head_sha"):
                        stored_locs[p["number"]] = dict(head_sha=loc[0], added=loc[1], deleted=loc[2])
                missing_loc = [p for p in value["prs"] if p.get("lean_loc") is None and p["number"] not in stored_locs]
                git_locs = activity_git.measure(root(), repo, missing_loc)
                if any(p.get("head_sha") and p.get("base_sha") and p["number"] not in git_locs for p in missing_loc):
                    message = "Some exact PR LOC comparisons are unavailable; normal refresh will retry"
                    if message not in errors:
                        errors.append(message)
                for p in value["prs"]:
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
                    counter = p.get("review_passes")
                    count = counter.get("count") if isinstance(counter, dict) else None
                    stamp = counter.get("observed_at") if isinstance(counter, dict) else None
                    valid = (
                        type(count) is int
                        and 0 <= count <= 2**53 - 1
                        and counter.get("basis") == "scoreboard-round-counters"
                        and type(stamp) in (int, float)
                        and math.isfinite(stamp)
                        and stamp >= 0
                    )
                    counter_values = (
                        p.get("head_sha"),
                        count if valid else None,
                        stamp / 1000 if valid else None,
                        now,
                        "scoreboard-round-counters" if valid else "unavailable",
                        repo,
                        p["number"],
                    )
                    db.execute(
                        "UPDATE pr_review_counts SET head=?,count=?,observed=?,checked=?,basis=? WHERE repo=? AND number=?",
                        counter_values,
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO pr_review_counts(head,count,observed,checked,basis,repo,number) VALUES (?,?,?,?,?,?,?)",
                        counter_values,
                    )
                    loc = p.get("lean_loc") or git_locs.get(p["number"]) or stored_locs.get(p["number"])
                    if loc is not None:
                        if loc.get("head_sha") != p.get("head_sha") or not all(
                            type(loc.get(k)) is int and loc[k] >= 0 for k in ("added", "deleted")
                        ):
                            raise ValueError("invalid LOC observation")
                        db.execute(
                            "UPDATE pr_loc SET head=?,added=?,deleted=?,observed=? WHERE repo=? AND number=?",
                            (loc["head_sha"], loc["added"], loc["deleted"], p["observed_at"] / 1000, repo, p["number"]),
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO pr_loc VALUES (?,?,?,?,?,?)",
                            (repo, p["number"], loc["head_sha"], loc["added"], loc["deleted"], p["observed_at"] / 1000),
                        )
                    else:
                        db.execute("DELETE FROM pr_loc WHERE repo=? AND number=?", (repo, p["number"]))
            except Exception as error:
                detail = ""
                if isinstance(error, urllib.error.HTTPError):
                    detail = f" (HTTP {error.code}"
                    if error.code == 403:
                        detail += "; repository unavailable or access denied"
                    detail += ")"
                observation_errors[repo] = (
                    "Shared PR observations unavailable for " + repo + detail + "; retaining last-known data"
                )
            # Missing resources and failures are observed no more frequently than successful reads.
            db.executemany("UPDATE prs SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch])
            db.executemany(
                "UPDATE pr_observations SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch]
            )
            for number in batch:
                p = db.execute("SELECT * FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
                if p is None:
                    p = db.execute("SELECT * FROM pr_observations WHERE repo=? AND number=?", (repo, number)).fetchone()
                loc = db.execute("SELECT head FROM pr_loc WHERE repo=? AND number=?", (repo, number)).fetchone()
                counts = db.execute(
                    "SELECT head,count,checked,observed FROM pr_review_counts WHERE repo=? AND number=?", (repo, number)
                ).fetchone()
                landed_loc = p and p["landed"] is not None and loc and loc[0] == p["head"]
                settled = (
                    landed_loc
                    and counts
                    and counts[0] == p["head"]
                    and counts[1] is not None
                    and counts[3] is not None
                    and counts[2] >= p["landed"]
                )
                db.execute(
                    "UPDATE report_pr_refresh SET next_at=? WHERE repo=? AND number=?",
                    (
                        None
                        if settled
                        or (landed_loc and counts and counts[2] >= p["landed"] and now > p["landed"] + 7 * 86400)
                        else now + (3600 if landed_loc and counts and counts[2] >= p["landed"] else 300),
                        repo,
                        number,
                    ),
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
    local_row = db.execute("SELECT value FROM meta WHERE key='local_coordination'").fetchone()
    local_buckets = json.loads(local_row[0]) if local_row else {}
    local_coordination = {}
    for label, seconds in (("24h", 86400), ("7d", 7 * 86400)):
        local_coordination[label] = {
            field: sum(
                v.get(field, 0) for at, v in local_buckets.items() if float(at) >= (now - seconds) // 3600 * 3600
            )
            for field in ("started", "git_held")
        }
    from . import activity_rollup as rollup
    from .activity_rollup_sources import flush

    flush(db, now)
    upper, totals = rollup.snapshot(db, now, WINDOWS, column=column, identity=identity)
    where, args = (f"WHERE {column}=?", [identity]) if column else ("", [])
    rounds = {}
    for window, values in totals.items():
        kinds = {json.loads(key[6:])[0] for key in values if key.startswith("round:") and values[key]}
        rounds[window] = {
            kind: {
                field: int(values.get("round:" + json.dumps([kind, field]), 0))
                for field in ("finished", "failed", "unknown")
            }
            for kind in kinds
        }

    def minimum(table, field, extra=""):
        predicate = where + (" AND " if where else " WHERE ") + field + " IS NOT NULL"
        if extra:
            predicate += " AND " + extra
        return db.execute(f"SELECT {field} FROM {table} {predicate} ORDER BY {field} LIMIT 1", args).fetchone()

    first_round = minimum("rounds", "COALESCE(started,finished)")
    cutoff_row = db.execute("SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff'").fetchone()
    first_pr = minimum(
        "prs",
        "created",
        "created >= (SELECT CAST(value AS REAL) FROM meta WHERE key='history_cutoff')" if cutoff_row else "",
    )
    since = min((row[0] for row in (first_round, first_pr) if row is not None), default=None)
    native_since = minimum("rounds", "started")
    pr_windows = {
        window: {key: int(values.get(key, 0)) for key in ("authored", "landed", "assisted", "reviews", "fixes")}
        for window, values in totals.items()
    }
    interrupted_where = f"{where} {'AND' if where else 'WHERE'} outcome='interrupted-unfinished'"
    latest_interrupted = db.execute(
        f"SELECT id round_id,worker,started FROM rounds {interrupted_where} "
        "AND started IS NOT NULL ORDER BY started DESC,id DESC LIMIT 1",
        args,
    ).fetchone()
    cutoff = cutoff_row[0] if cutoff_row else since
    costs = activity_costs.summarize(db, now, cutoff, column, identity)
    for window in activity_costs.COST_WINDOWS:
        values = totals[window]
        row = costs["windows"][window]
        row.update(
            {
                key: int(values.get(key, 0))
                for key in (
                    "landed_loc_added",
                    "landed_prs",
                    "missing_loc_prs",
                    "known_loc_prs",
                )
            }
        )
        row["loc_basis"] = "landed_at"
        row.update(activity_summary.loc_estimate(row))
    for window, attribution in activity_attribution.summarize(db, now, activity_costs.COST_WINDOWS, column, identity):
        row = costs["windows"][window]
        attribution["attribution_missing_prs"] += max(
            0, row["landed_prs"] - attribution.get("attribution_authored_prs", 0)
        )
        row.update(attribution)
    raw_observed = minimum("prs", "observed")
    raw_checked = minimum("prs", "checked")
    unresolved = "landed IS NULL AND state IS NOT 'CLOSED'"
    oldest_observed = minimum("prs", "observed", unresolved)
    oldest_missing = minimum("prs", "created", unresolved + " AND observed IS NULL")
    return dict(
        prepared_at=now,
        review_coordination={
            r[0][len("coordination:") :]: json.loads(r[1])
            for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'coordination:%'")
        },
        local_coordination=local_coordination,
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
        interrupted_unfinished=int(upper.get("interrupted_total", 0)),
        interrupted_unfinished_details=dict(
            started_1h=int(totals["1h"].get("interrupted", 0)),
            started_24h=int(totals["24h"].get("interrupted", 0)),
            latest=dict(latest_interrupted) if latest_interrupted else None,
        ),
        open_prs=int(upper.get("open_stock", 0)),
        unknown_prs=int(upper.get("unknown_prs", 0)),
        unknown_landings=int(upper.get("unknown_landings", 0)),
        pr_observed_at=raw_observed[0] if raw_observed else None,
        pr_checked_at=raw_checked[0] if raw_checked else None,
        pr_freshness=dict(
            observed_at=oldest_observed[0] if oldest_observed else None,
            missing_since=oldest_missing[0] if oldest_missing else None,
        ),
    )


def _observation_error_subjects(db):
    """Resolve only errored repositories once, using repository-indexed facts."""
    prior = db.execute("SELECT value FROM meta WHERE key='observation_errors'").fetchone()
    subjects = {}
    for repo, error in (json.loads(prior[0]) if prior else {}).items():
        affected = subjects.setdefault(error, set())
        for worker, scope in db.execute(
            "SELECT worker,scope FROM prs WHERE repo=? UNION SELECT worker,scope FROM pr_contributions WHERE repo=?",
            (repo, repo),
        ):
            affected.update((("worker", worker), ("scope", scope)))
    return subjects


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
                try:
                    backfill(db, history_roots)
                except (OSError, ValueError, KeyError):
                    errors.append("PR activity receipt is unreadable")
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
                    pending = db.execute("SELECT value FROM meta WHERE key='activity_cost_error'").fetchone()
                    if pending:
                        errors.append("Conversation cost preparation incomplete: " + pending[0])
                except (OSError, ValueError, KeyError) as error:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_cost_error',?)", (type(error).__name__,))
                    errors.append("Conversation cost preparation failed; retaining last-known budget data")
                _refresh_prs(db, config, now, errors)
                _bors(db, errors)
                try:
                    activity_attribution.prepare(db, now)
                except Exception as error:
                    errors.append(
                        "Local attribution preparation failed; retaining last-known summary ("
                        + type(error).__name__
                        + ")"
                    )
            from . import expanded_cache, shared_mathlib, work_recovery

            try:
                from . import retention

                # Delete here too even when a collector is installed: deletion no longer waits on the
                # housekeeping lock, and an installed timer that has stalled must not stop it.
                housekeeping = work_recovery.housekeep(cleanup=True, **retention.COLLECTOR_SLICE)
                if housekeeping.get("cleanup_pending") and read_json(root() / "cleanup-service.json").get("installed"):
                    retention.request_cleanup()
            except Exception as error:
                errors.append(f"Recovery housekeeping deferred ({type(error).__name__}); retryable jobs retained")
            from . import verification_storage

            try:
                from .retention import worker_configs

                states = [state for cfg in worker_configs() for state in (cfg.state, cfg.state / "verification-state")]
                verification_storage.housekeep(states)
            except Exception as error:
                errors.append(f"Verification storage housekeeping deferred ({type(error).__name__})")
            with db:
                error_subjects = _observation_error_subjects(db)
                sharing = shared_mathlib.summary()
                value = dict(version=1, prepared_at=now, errors=errors, all=_summary(db, now), workers={}, groups={})
                value["all"]["shared_mathlib"] = sharing
                value["all"]["work_recovery"] = work_recovery.summary()
                value["all"]["storage_cleanup"] = read_json(root() / "cleanup.json")
                value["all"]["expanded_cache"] = expanded_cache.summary()
                value["all"]["verification_storage"] = read_json(verification_storage.root() / "status.json")
                from . import activity_live

                report_paths = [root() / "summary.json"]
                reports = []
                live_context = activity_live.publication_context()
                from .activity_rollup_sources import flush

                flush(db, now)
                subjects = [
                    json.loads(row[0]) for row in db.execute("SELECT subject FROM report_subjects WHERE refs>0")
                ]
                for column, key in (("worker", "workers"), ("scope", "groups")):
                    ids = [(identity,) for kind, identity in subjects if kind == column]
                    for row in ids:
                        if row[0] is None:
                            continue
                        value[key][row[0]] = _summary(db, now, column, row[0])
                        value[key][row[0]]["shared_mathlib"] = shared_mathlib.summary(
                            **{"scope" if key == "groups" else "worker": row[0]}
                        )
                        members = (
                            {r[0] for r in db.execute("SELECT worker FROM report_members WHERE scope=?", (row[0],))}
                            if key == "groups"
                            else {row[0]}
                        )
                        value[key][row[0]]["work_recovery"] = work_recovery.summary(workers=members)
                        if key == "groups":
                            # Storage belongs to the host, not to the accounting worker window.
                            value[key][row[0]]["storage_cleanup"] = value["all"]["storage_cleanup"]
                            value[key][row[0]]["expanded_cache"] = value["all"]["expanded_cache"]
                        report_path = scope_path(key, row[0])
                        report_paths.append(report_path)
                        reports.append(
                            (
                                report_path,
                                dict(
                                    version=1,
                                    prepared_at=now,
                                    all=value[key][row[0]],
                                    errors=[
                                        e
                                        for e in errors
                                        if e not in error_subjects or (column, row[0]) in error_subjects[e]
                                    ],
                                    live_subject={"worker" if key == "workers" else "scope": row[0]},
                                ),
                            )
                        )
            for report_path, report in reports:
                activity_live.publish_report(report_path, report, carry=live_context)
            # These are rebuildable views. Receipts and SQLite remain durable;
            # readers need atomic replacement, not a disk barrier for every view.
            activity_live.publish_report(
                root() / "summary.json",
                {k: v for k, v in value.items() if k not in ("workers", "groups")},
                carry=live_context,
            )
            activity_live.publish_inputs(value, report_paths)
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
        from . import processes, worker_scopes
        from .paths import self_argv, self_env

        oom_since = time.time() - 300
        while not stopping.is_set():
            started = time.monotonic()
            observed_at = time.time()
            try:
                if worker_scopes.collect_oom_events(config, oom_since):
                    oom_since = observed_at
            except Exception:
                pass
            try:
                # SQLite row stepping must not contend with manager socket I/O
                # for the GIL. Reuse the existing one-shot command and lock.
                argv = self_argv("activity", "refresh", "--json") + (["--config", str(config)] if config else [])
                processes.run(argv, env=self_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
            except Exception as error:
                intentional_stop = isinstance(error, subprocess.CalledProcessError) and error.returncode in (
                    -signal.SIGTERM,
                    -signal.SIGINT,
                )
                if not stopping.is_set() and not intentional_stop:
                    with contextlib.suppress(Exception):
                        atomic_json(root() / "preparation-error.json", dict(at=time.time(), error=type(error).__name__))
            stopping.wait(max(0, 60 - (time.monotonic() - started)))

    thread = threading.Thread(target=run, name="activity-preparation", daemon=True)
    thread.start()

    def live():
        from .activity_live import INTERVAL, sample

        while not stopping.is_set():
            try:
                sample()
            except Exception:
                pass  # Reporting must never stop the manager or paid work.
            stopping.wait(INTERVAL)

    threading.Thread(target=live, name="live-spending", daemon=True).start()
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
        # A complete inventory is an explicit request, never a periodic export.
        # Read the prepared facts without schema changes, collectors or network.
        path = root() / "activity.sqlite"
        value = dict(version=1, prs=[])
        if path.exists():
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                predicates, args_ = [], []
                for column, selected in (("worker", args.worker), ("scope", args.group)):
                    if selected:
                        predicates.append(column + "=?")
                        args_.append(selected)
                where = " WHERE " + " AND ".join(predicates) if predicates else ""
                value["prs"] = [dict(row) for row in db.execute("SELECT * FROM prs" + where, args_)]
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

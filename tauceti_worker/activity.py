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
    """)
    return db


def _ingest(db, e):
    if e.get("version") != 1 or not isinstance(e.get("at"), (int, float)):
        raise ValueError("invalid activity receipt")
    if not db.execute("INSERT OR IGNORE INTO events VALUES (?)", (e["id"],)).rowcount:
        return
    kind, worker, scope = e["kind"], e.get("worker", "unknown"), e.get("group", e.get("machine_id", "local"))
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
    from .paths import HERE

    def resource(row):
        r = row["round"]
        if r.get("activity_id") or r.get("activity_schema"):
            return []  # Already recorded by the activity lifecycle; never double-count.
        rid = r["id"]
        # The old resource receipt has no work type or success status.
        wid = row["worker_id"]
        machine = os.environ.get("TAUCETI_FLEET_MACHINE_ID") or read_json(
            Path.home() / ".config/tauceti-fleet/host.json"
        ).get("machine_id")
        scope = machine if machine and wid.startswith("fleet-" + machine[:8] + "-") else "local"
        return [
            dict(
                version=1,
                id="resource:" + rid,
                at=row["sampled_at"],
                kind="round_finished",
                round_id="resource:" + rid,
                worker=wid,
                group=scope,
                round_type="unknown",
                outcome="unknown",
            )
        ]

    paths = {Path(row[0]) for row in db.execute("SELECT path FROM cursors")}
    for directory in (HERE, *history_roots):
        paths.update((Path(directory) / "logs").glob("*/resources.jsonl"))
    path = Path(os.environ.get("TAUCETI_FLEET_EVENTS", Path.home() / ".local/state/tauceti-fleet/worker-events.jsonl"))
    paths.add(path)
    for path in sorted(paths):
        converter = (
            resource if path.name == "resources.jsonl" else lambda e: [e] if e.get("kind") == "pr_created" else []
        )
        backfill_file(db, path, converter)


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
    batches = {}
    prior = db.execute("SELECT value FROM meta WHERE key='observation_errors'").fetchone()
    observation_errors = json.loads(prior[0]) if prior else {}
    for p in db.execute(
        "SELECT * FROM prs WHERE landed IS NULL AND (checked IS NULL OR checked<=?) ORDER BY checked IS NOT NULL,checked,repo,number",
        (now - 300,),
    ):
        spec = specs.get(p["worker"])
        url = getattr(spec, "github_state_url", None) or os.environ.get("TAUCETI_GITHUB_STATE_URL")
        token = getattr(spec, "github_state_token_file", None) or os.environ.get("TAUCETI_GITHUB_STATE_TOKEN_FILE")
        if url and token:
            batches.setdefault((url, str(token), p["repo"]), []).append(p["number"])
    attempted_repos = set()
    deadline = time.monotonic() + 10
    for (url, token, repo), numbers in batches.items():
        if repo not in attempted_repos:
            observation_errors.pop(repo, None)
            attempted_repos.add(repo)
        for offset in range(0, len(numbers), 500):
            if time.monotonic() >= deadline:
                errors.append("Remaining shared PR observations deferred to the next preparation pass")
                break
            batch = numbers[offset : offset + 500]
            try:
                value = observe(url, token, repo, batch)
                seen = [p["number"] for p in value["prs"]] + value["missing"]
                if len(seen) != len(set(seen)) or set(seen) != set(batch):
                    raise ValueError("incomplete observation response")
                updates = []
                for p in value["prs"]:
                    if p["number"] not in batch or p.get("state") not in ("OPEN", "CLOSED", "MERGED"):
                        raise ValueError("invalid PR observation")
                    updates.append(
                        (
                            p["state"],
                            _epoch(p.get("landed_at")),
                            p["observed_at"] / 1000,
                            now,
                            p.get("head_sha"),
                            p.get("landing_source"),
                            _epoch(p.get("created_at")),
                            repo,
                            p["number"],
                        ),
                    )
                db.executemany(
                    "UPDATE prs SET state=?,landed=COALESCE(?,landed),observed=?,checked=?,head=?,source=COALESCE(?,source),created=COALESCE(?,created) WHERE repo=? AND number=?",
                    updates,
                )
                # Missing facts stay unknown/last-known, but do not cause a retry storm.
                db.executemany("UPDATE prs SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch])
            except Exception:
                observation_errors[repo] = (
                    "Shared PR observations unavailable for " + repo + "; retaining last-known data"
                )
                db.executemany("UPDATE prs SET checked=? WHERE repo=? AND number=?", [(now, repo, n) for n in batch])
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
    return dict(
        prepared_at=now,
        history_since=since,
        rounds=rounds,
        round_types=list(ROUND_TYPES),
        pr_windows=pr_windows,
        running=(
            db.execute(
                f"SELECT COUNT(*) FROM rounds {where} {'AND' if where else 'WHERE'} finished IS NULL AND outcome IS NULL",
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
        unknown_landings=sum(p["state"] in ("CLOSED", "MERGED") and p["landed"] is None for p in prs),
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
                # Once registered, old structured files continue to be tailed by
                # offset during a gradual runtime upgrade. No transcript parsing.
                if import_history or db.execute("SELECT 1 FROM cursors LIMIT 1").fetchone():
                    try:
                        backfill(db, history_roots)
                    except (OSError, ValueError, KeyError):
                        errors.append("Historical activity receipt is unreadable; imported history may be incomplete")
                _consume(db, errors)
                _refresh_prs(db, config, now, errors)
                _bors(db, errors)
            value = dict(version=1, prepared_at=now, errors=errors, all=_summary(db, now), workers={}, groups={})
            for column, key in (("worker", "workers"), ("scope", "groups")):
                ids = db.execute(f"SELECT {column} FROM rounds UNION SELECT {column} FROM prs").fetchall()
                for row in ids:
                    value[key][row[0]] = _summary(db, now, column, row[0])
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
    if not value.get("available"):
        print("Activity: unavailable; " + value.get("reason", "no recorded activity for this scope"))
        return
    s = value["summary"]
    age = max(0, int(time.time() - s["prepared_at"]))
    print(f"Activity: prepared {age}s ago" + (" (stale)" if age > 180 else ""))
    since = (
        datetime.fromtimestamp(s["history_since"]).astimezone().isoformat(timespec="seconds")
        if s["history_since"]
        else "no recorded history"
    )
    print("Recorded history begins: " + since + "; totals cover recorded activity only")
    observed_kinds = {kind for rows in s["rounds"].values() for kind in rows}
    kinds = list(dict.fromkeys(s.get("round_types", ROUND_TYPES)))
    kinds += sorted(observed_kinds - set(kinds))
    rows = []
    for kind in ["ALL", *kinds]:
        counts = [
            sum(r["finished"] for r in s["rounds"][w].values())
            if kind == "ALL"
            else s["rounds"][w].get(kind, {}).get("finished", 0)
            for w in WINDOWS
        ]
        label = "type unrecorded" if kind == "unknown" else kind
        rows.append(("  " + label, counts))
    rows.append(("    failed/timed out", [sum(r["failed"] for r in s["rounds"][w].values()) for w in WINDOWS]))
    unknown_outcomes = any(r.get("unknown") for rs in s["rounds"].values() for r in rs.values())
    if unknown_outcomes:
        rows.append(
            ("    outcome unrecorded", [sum(r.get("unknown", 0) for r in s["rounds"][w].values()) for w in WINDOWS])
        )
    for key, label in (("authored", "PRs authored"), ("landed", "Authored PRs landed")):
        rows.append((label, [s["pr_windows"][w][key] for w in WINDOWS]))
    headers = ("3h", "24h", "7d", "Recorded total")
    label_width = max(len("Finished rounds"), *(len(label) for label, _ in rows))
    widths = [max(len(header), *(len(str(counts[i])) for _, counts in rows)) for i, header in enumerate(headers)]
    print(
        f"{'Finished rounds':<{label_width}}  "
        + "  ".join(f"{header:>{width}}" for header, width in zip(headers, widths, strict=True))
    )
    for label, counts in rows:
        print(
            f"{label:<{label_width}}  "
            + "  ".join(f"{count:>{width}}" for count, width in zip(counts, widths, strict=True))
        )
    print("Outcome rows are subsets of finished rounds.")
    if "unknown" in observed_kinds or unknown_outcomes:
        print("Older resource logs did not record round types or outcomes; these receipts cannot supply them.")
    print(
        f"Currently: {s['running'] if s['running'] is not None else 'unknown'} rounds running; {s['open_prs']} known open authored PRs; {s['unknown_prs']} PR states unknown; {s['unknown_landings']} closed/merged PRs lack landing evidence"
    )
    if s["interrupted_unfinished"]:
        print(f"Interrupted without a finish receipt: {s['interrupted_unfinished']} (excluded from time-window counts)")
    observed = s.get("pr_observed_at")
    print(
        "PR evidence: "
        + (f"oldest observation {max(0, int(time.time() - observed))}s ago" if observed else "not yet available")
    )
    for error in value.get("errors", []):
        print("Activity coverage: " + error)


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

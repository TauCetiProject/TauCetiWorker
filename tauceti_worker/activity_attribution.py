"""Local cost shares, using existing receipts and already-synced review records.

No network, subprocesses, transcript scans, or new collector. Archive work has
both a per-pass record budget and a wall-clock budget; warm records are cached.
"""

import hashlib
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .constants import TAUCETI

AUTHOR_PHASES = {"roadmap"}
FIX_PHASES = {"fix", "fix-ci", "rebase", "lint-repair", "overlap-fix"}
FIX_PHASES.add("recovery")
MAX_RECORDS = 256
MAX_PRS = 32
MAX_SECONDS = 1
MAX_RECORD_BYTES = 256 * 1024
ARCHIVE_GRACE = 60


def archive_root():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    repo = os.environ.get("TAUCETI_DATA_REPO", "TauCetiProject/TauCetiData")
    return cache / "tauceti-review" / "data" / repo.replace("/", "__")


def invocation_context(phase=None):
    """A few fields on the existing admission receipt, including failed work."""
    number = os.environ.get("TAUCETI_GITHUB_PR", "")
    if phase is not None and phase not in FIX_PHASES | {"review", "bump"}:
        number = ""
    return dict(
        round_id=os.environ.get("TAUCETI_ACTIVITY_ROUND_ID"),
        repository=os.environ.get("TAUCETI_GITHUB_REPOSITORY", TAUCETI),
        pr=int(number) if number.isdigit() and int(number) > 0 else None,
    )


def archive_outbox(worker):
    home = Path(os.environ.get("TAUCETI_DATA_HOME") or Path.home())
    return home / ".cache" / "tauceti-review" / worker / "store" / TAUCETI.replace("/", "__") / "outbox"


def register_worker_paths(db, worker):
    """Register deterministic existing cache locations; never walk worker homes."""
    if not db.execute("INSERT OR IGNORE INTO attribution_worker_paths VALUES (?)", (worker,)).rowcount:
        return
    from .config import worker_state_root

    homes = {Path.home(), worker_state_root() / worker / "home"}
    if value := os.environ.get("TAUCETI_DATA_HOME"):
        homes.add(Path(value))
    data = os.environ.get("TAUCETI_DATA_REPO", "TauCetiProject/TauCetiData").replace("/", "__")
    for home in homes:
        cache = home / ".cache" / "tauceti-review"
        db.execute("INSERT OR IGNORE INTO review_archives VALUES (?,1)", (str(cache / "data" / data),))
        db.execute(
            "INSERT OR IGNORE INTO review_archives VALUES (?,0)",
            (str(cache / worker / "store" / TAUCETI.replace("/", "__") / "outbox"),),
        )


def archive_fetch(root):
    """Read existing Git metadata, without fetching or starting Git."""
    try:
        git = root / ".git"
        if git.is_file():
            git = (root / git.read_text().strip().removeprefix("gitdir: ")).resolve()
        fetch = git / "FETCH_HEAD"
        fetched = fetch.read_text().split()[0]
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            path = git / ref
            if path.is_file():
                head = path.read_text().strip()
            else:
                head = next(
                    line.split()[0]
                    for line in (git / "packed-refs").read_text().splitlines()
                    if line.endswith(" " + ref)
                )
        remote = git / "refs" / "remotes" / "origin" / "main"
        remote_head = (
            remote.read_text().strip()
            if remote.is_file()
            else next(
                (
                    line.split()[0]
                    for line in (git / "packed-refs").read_text().splitlines()
                    if line.endswith(" refs/remotes/origin/main")
                ),
                None,
            )
            if (git / "packed-refs").is_file()
            else None
        )
        if len(fetched) not in (40, 64) or any(c not in "0123456789abcdef" for c in fetched):
            return None
        if head != fetched and head != remote_head:
            return None  # A fetch/reset/push is still in progress.
        return fetch.stat().st_mtime
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_record(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("review round record too large")
    return json.loads(raw)


def round_work(row, repo, number):
    if not isinstance(row, dict) or not isinstance(row.get("ts"), str):
        raise ValueError("invalid review round")
    if row.get("repo") != repo:
        return None
    if row.get("pr") != number:
        raise ValueError("review round identity mismatch")
    if row.get("arm", "production") != "production" or row.get("source") == "shadow":
        return None
    ids = row.get("run_ids")
    if ids is None and isinstance(row.get("ran"), list):
        # Older records predate individual execution IDs. Their timestamp and
        # round identity still identify the review pass, without weighting its
        # individual checks or treating a model-free carry as another review.
        ids = [json.dumps([row.get("round_id"), row["ts"], row["ran"]])] if row["ran"] else []
    if not isinstance(ids, list) or any(not isinstance(r, str) or not r for r in ids):
        raise ValueError("review round lacks execution identities")
    # Cache only a timestamp and pass identity, not the individual review
    # records/IDs. Preparing many worker scopes reuses this compact evidence.
    return dict(
        at=epoch(row["ts"]),
        round_id=row.get("round_id"),
        review=hashlib.sha256(json.dumps(sorted(set(ids))).encode()).hexdigest() if ids else None,
    )


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS round_prs(round_id TEXT,repo TEXT,number INTEGER,
        PRIMARY KEY(round_id,repo,number));
      CREATE TABLE IF NOT EXISTS invocation_work(id TEXT PRIMARY KEY,phase TEXT,
        round_id TEXT,repo TEXT,number INTEGER);
      CREATE TABLE IF NOT EXISTS review_archives(path TEXT PRIMARY KEY,full INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE IF NOT EXISTS review_archive_sources(path TEXT PRIMARY KEY,fetched REAL);
      CREATE TABLE IF NOT EXISTS attribution_worker_paths(worker TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS pr_review_counts(repo TEXT,number INTEGER,head TEXT,count INTEGER,
        observed REAL,checked REAL,basis TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS review_archive_checks(repo TEXT,number INTEGER,
        checked REAL,complete INTEGER,fingerprint TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS review_archive_records(path TEXT,
        signature TEXT,repo TEXT,number INTEGER,payload TEXT,PRIMARY KEY(repo,number,path));
      CREATE INDEX IF NOT EXISTS archive_record_pr ON review_archive_records(repo,number);
    """)
    from .activity_attribution_prepare import install as install_adapters

    install_adapters(db)
    if db.execute("INSERT OR IGNORE INTO meta VALUES ('attribution-review-counts-v1','1')").rowcount:
        db.execute("UPDATE report_pr_refresh SET next_at=0 WHERE next_at IS NULL")


def ingest(db, event):
    if event.get("kind") not in ("pr_created", "pr_activity", "round_selected"):
        return
    repo, number, rid = event.get("repository"), event.get("pr"), event.get("round_id")
    if not repo or type(number) is not int or number <= 0:
        return
    db.execute("INSERT OR IGNORE INTO review_archive_checks VALUES (?,?,0,0,NULL)", (repo, number))
    if rid:
        if db.execute("INSERT OR IGNORE INTO round_prs VALUES (?,?,?)", (rid, repo, number)).rowcount:
            from .activity_attribution_prepare import queue

            queue(db, "round", [rid])
    if event.get("kind") == "round_selected" and event.get("round_type") == "review":
        db.execute("UPDATE review_archive_checks SET checked=0 WHERE repo=? AND number=?", (repo, number))
    if archive := event.get("review_archive"):
        db.execute("INSERT OR IGNORE INTO review_archives VALUES (?,1)", (archive,))
    if outbox := event.get("review_outbox"):
        db.execute("INSERT OR IGNORE INTO review_archives VALUES (?,0)", (outbox,))


class CostTargets:
    """Resolve changed sessions with indexed native-round and receipt reads."""

    def __init__(self, db, now=None):
        self.db = db

    def containing(self, worker, at):
        row = self.db.execute(
            "SELECT id,started,finished FROM rounds WHERE worker=? AND started<=? ORDER BY started DESC LIMIT 1",
            (worker, at),
        ).fetchone()
        return row[0] if row and row[2] is not None and row[1] <= at <= row[2] else None

    def resolve(self, session):
        context = session.get("activity") or {}
        rid = context.get("round_id") or self.containing(session["worker"], session["started"])
        repo, number = context.get("repository"), context.get("pr")
        if not (repo and type(number) is int and number > 0):
            phase = session.get("phase", "unknown")
            creating = phase in AUTHOR_PHASES or phase == "overlap-fix"
            targets = (
                set()
                if creating
                else {tuple(r) for r in self.db.execute("SELECT repo,number FROM round_prs WHERE round_id=?", (rid,))}
            )
            round = self.db.execute("SELECT worker,started,finished FROM rounds WHERE id=?", (rid,)).fetchone()
            if round and round[1] is not None and round[2] is not None:
                for table, field in (
                    (("prs", "created"),) if creating else (("prs", "created"), ("pr_contributions", "at"))
                ):
                    targets.update(
                        tuple(r)
                        for r in self.db.execute(
                            f"SELECT repo,number FROM {table} WHERE worker=? AND {field} BETWEEN ? AND ?",
                            tuple(round),
                        )
                    )
            repo, number = next(iter(targets)) if len(targets) == 1 else (None, None)
        phase = session.get("phase", "unknown")
        if phase == "overlap-fix" and not context.get("pr") and repo:
            created = self.db.execute(
                "SELECT 1 FROM prs p JOIN rounds r ON r.id=? WHERE p.repo=? AND p.number=? "
                "AND p.worker=r.worker AND p.created BETWEEN r.started AND r.finished",
                (rid, repo, number),
            ).fetchone()
            if created:
                phase = "roadmap"
        return session["id"], phase, rid, repo, number


def prepare_reviews(db, now, priority=()):
    """Bounded reads of small records for tracked PRs; never clone or refresh."""
    roots = {archive_root(): True}
    roots.update((Path(path), full) for path, full in db.execute("SELECT path,full FROM review_archives"))
    if explicit := os.environ.get("TAUCETI_ACTIVITY_REVIEW_ARCHIVES"):
        roots = {root: full for root, full in roots.items() if not full}
        roots.update((Path(p).expanduser(), True) for p in explicit.split(os.pathsep) if p)
    fetched, snapshots = 0, []
    for root, full in roots.items():
        stamp = archive_fetch(root) if full else None
        if stamp is not None:
            snapshots.append((stamp, root))
            fetched = max(fetched, stamp)
            prior = db.execute("SELECT fetched FROM review_archive_sources WHERE path=?", (str(root),)).fetchone()
            if not prior or prior[0] != stamp:
                db.execute("INSERT OR REPLACE INTO review_archive_sources VALUES (?,?)", (str(root), stamp))
        elif full:
            db.execute("DELETE FROM review_archive_sources WHERE path=?", (str(root),))
    # Every normal data clone contains the canonical history. Read the freshest
    # consistent clone and union local outboxes, rather than reading N copies.
    selected = {root for root, full in roots.items() if not full}
    if snapshots:
        selected.add(max(snapshots)[1])
    roots = sorted(p / "records" / "rounds" for p in selected if (p / "records" / "rounds").is_dir())
    if not roots:
        return
    pending = [
        tuple(key)
        for key in priority
        if db.execute(
            "SELECT 1 FROM review_archive_checks WHERE repo=? AND number=? AND checked<=?", (*key, now - 300)
        ).fetchone()
    ]
    pending += [
        tuple(r)
        for r in db.execute(
            "SELECT repo,number FROM review_archive_checks WHERE checked<=? ORDER BY checked LIMIT ?",
            (now - 300, MAX_PRS),
        )
    ]
    pending = list(dict.fromkeys(pending))[:MAX_PRS]
    deadline, remaining = time.monotonic() + MAX_SECONDS, MAX_RECORDS
    for repo, number in pending:
        counts = db.execute(
            "SELECT head,count,observed FROM pr_review_counts WHERE repo=? AND number=?", (repo, number)
        ).fetchone()
        observed = db.execute("SELECT head,landed FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
        observed = (
            observed
            or db.execute(
                "SELECT head,landed FROM pr_observations WHERE repo=? AND number=?", (repo, number)
            ).fetchone()
        )
        if (
            counts
            and counts[1] is not None
            and observed
            and observed[0] == counts[0]
            and observed[1] is not None
            and counts[2] is not None
            and counts[2] >= 0
        ):
            db.execute("UPDATE review_archive_checks SET checked=1e99 WHERE repo=? AND number=?", (repo, number))
            continue
        fingerprints = []
        for root in roots:
            try:
                stat = (root / str(number)).stat()
                stamp = [stat.st_ino, stat.st_mtime_ns]
            except FileNotFoundError:
                stamp = None
            fingerprints.append([str(root), stamp])
        fingerprint = json.dumps(fingerprints)
        prior = db.execute(
            "SELECT complete,fingerprint,checked FROM review_archive_checks WHERE repo=? AND number=?", (repo, number)
        ).fetchone()
        landed = db.execute("SELECT landed,state FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
        landed = (
            landed
            or db.execute(
                "SELECT landed,state FROM pr_observations WHERE repo=? AND number=?", (repo, number)
            ).fetchone()
        )
        settled = bool(
            landed
            and (
                (landed[0] is not None and fetched >= landed[0] + ARCHIVE_GRACE)
                or (landed[0] is None and landed[1] == "CLOSED")
            )
        )
        checked_at = 1e99 if settled else now
        if prior and prior[0] and prior[1] == fingerprint:
            # Archive records are write-if-absent. Unchanged directory entries
            # require no enumeration, per-file stats, or JSON parsing.
            db.execute(
                "UPDATE review_archive_checks SET checked=? WHERE repo=? AND number=?", (checked_at, repo, number)
            )
            if prior[2] < fetched:
                from .activity_attribution_prepare import queue

                queue(db, "pr", [repo, number])
            continue
        complete, found = True, set()
        for root in roots:
            for path in sorted((root / str(number)).glob("*.json")):
                if time.monotonic() >= deadline:
                    complete = False
                    break
                key = str(path)
                found.add(key)
                try:
                    stat = path.stat()
                    stamp = json.dumps([stat.st_ino, stat.st_size, stat.st_mtime_ns])
                    previous = db.execute(
                        "SELECT signature FROM review_archive_records WHERE path=? AND repo=? AND number=?",
                        (key, repo, number),
                    ).fetchone()
                    if previous and previous[0] == stamp:
                        continue
                    if remaining <= 0 or time.monotonic() >= deadline:
                        complete = False
                        break
                    remaining -= 1
                    row = round_work(read_record(path), repo, number)
                    db.execute(
                        "INSERT OR REPLACE INTO review_archive_records VALUES (?,?,?,?,?)",
                        (key, stamp, repo, number, json.dumps(row)),
                    )
                except Exception:
                    complete = False
            if not complete:
                break
        if complete:
            # A locally removed/corrected archive record must not survive in a
            # derived count. Never delete on an interrupted/incomplete scan.
            stale = [
                r[0]
                for r in db.execute("SELECT path FROM review_archive_records WHERE repo=? AND number=?", (repo, number))
                if r[0] not in found
                and not any(
                    Path(r[0]).is_relative_to(root)
                    for root in roots
                    if not snapshots or root != max(snapshots)[1] / "records" / "rounds"
                )
            ]
            db.executemany(
                "DELETE FROM review_archive_records WHERE path=? AND repo=? AND number=?",
                ((p, repo, number) for p in stale),
            )
        db.execute(
            "INSERT OR REPLACE INTO review_archive_checks VALUES (?,?,?,?,?)",
            (repo, number, checked_at if complete else now, complete, fingerprint),
        )
        if remaining <= 0 or time.monotonic() >= deadline:
            break


def calculate(records, now, windows, reference=None):
    """Estimate one local accounting scope from compact, prepared PR facts."""
    review_costs, review_dates = {}, {}
    for row in records:
        for key, cost, at in row.get("samples", []):
            review_costs[key] = review_costs.get(key, Decimal(0)) + Decimal(cost)
            review_dates[key] = max(at, review_dates.get(key, at))
    records = [r for r in records if r["landed"] is not None and r["landed"] <= now]
    # The rolling sample uses locally observed landed production. Fall back to
    # retained history when the last week has no usable authoring sample.
    authors = [r for r in records if r["owned"] and r["author_known"] and r["loc"]]
    recent = [r for r in authors if r["landed"] >= now - 7 * 86400]
    authors = recent or authors
    production_rate = (
        sum((r["production"] for r in authors), Decimal(0)) / sum(r["loc"] for r in authors) if authors else None
    )
    reviews = [rid for rid in review_costs if review_costs[rid] > 0]
    recent_reviews = [rid for rid in reviews if review_dates[rid] >= now - 7 * 86400]
    reviews = recent_reviews or reviews
    review_rate = (sum((review_costs[rid] for rid in reviews), Decimal(0)) / len(reviews)) if reviews else None
    if reference:
        production_rate, review_rate = reference["production"], reference["review"]
    for row in records:
        row["credited"] = None
        row["estimated_total"] = None
        if reference:
            total = reference["totals"].get(tuple(row["key"]))
            if total is not None and total > 0 and row["loc"] is not None:
                row["credited"] = Decimal(row["loc"]) * (row["production"] + row["review"]) / total
            continue
        if (
            row["loc"] is None
            or not row["review_known"]
            or (row["owned"] and not row["author_known"])
            or (row["review"] > 0 and not row["local_runs"])
        ):
            continue
        unit_cost = row["review"] / row["local_runs"] if row["local_runs"] else review_rate
        outside_runs = max(0, row["total_runs"] - row.get("attempted_runs", row["local_runs"]))
        if outside_runs and unit_cost is None:
            continue
        if not row["owned"] and production_rate is None:
            continue
        outside_production = (
            Decimal(0) if row["owned"] else max(Decimal(0), production_rate * row["loc"] - row["production"])
        )
        local = row["production"] + row["review"]
        total = local + outside_production + outside_runs * (unit_cost or Decimal(0))
        if total > 0:
            row["estimated_total"] = total
            row["credited"] = Decimal(row["loc"]) * local / total
    for name, seconds in windows.items():
        selected = [r for r in records if seconds is None or r["landed"] >= now - seconds]
        known = [r for r in selected if r["credited"] is not None]
        yield (
            name,
            dict(
                attribution_version=1,
                attributed_loc=str(sum((r["credited"] for r in known), Decimal(0))),
                attribution_known_prs=len(known),
                attribution_missing_prs=len(selected) - len(known),
                attribution_authored_prs=sum(r["owned"] for r in selected),
                production_usd_per_loc=str(production_rate) if production_rate is not None else None,
                review_usd_per_pass=str(review_rate) if review_rate is not None else None,
                review_counts_observed_at=min(
                    (r["review_observed_at"] for r in selected if r.get("review_observed_at") is not None), default=None
                ),
            ),
        )


def summarize(db, now, windows, column=None, identity=None):
    from .activity_attribution_prepare import history_pending

    subject = json.dumps([column, identity])
    cached = db.execute("SELECT payload FROM attribution_reports WHERE subject=?", (subject,)).fetchone()
    values = json.loads(cached[0]) if cached else {}
    preparing = (
        db.execute("SELECT 1 FROM attribution_dirty LIMIT 1").fetchone() is not None
        or db.execute("SELECT 1 FROM meta WHERE key='attribution-report-dirty'").fetchone() is not None
    )
    prior = db.execute("SELECT value FROM meta WHERE key='attribution-report-backfilling'").fetchone()
    no_reports = not cached and db.execute("SELECT 1 FROM attribution_reports LIMIT 1").fetchone() is None
    backfilling = history_pending(db) or bool(prior and prior[0] == "1") or no_reports
    for key in windows:
        row = values.get(
            key, dict(attribution_version=1, attributed_loc=None, attribution_known_prs=0, attribution_missing_prs=0)
        )
        yield key, {**row, "attribution_preparing": preparing, "attribution_backfilling": backfilling}


def prepare(db, now):
    from .activity_attribution_prepare import prepare as incremental_prepare

    db.execute("SAVEPOINT prepare_attribution")
    try:
        incremental_prepare(db, now)
    except BaseException:
        db.execute("ROLLBACK TO prepare_attribution")
        db.execute("RELEASE prepare_attribution")
        raise
    db.execute("RELEASE prepare_attribution")

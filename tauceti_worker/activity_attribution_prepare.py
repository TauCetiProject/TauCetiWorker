"""Incremental adapters for attribution; no historical cost scans on quiet ticks."""

import json
import time
from collections import defaultdict
from decimal import Decimal

from . import activity_attribution as a
from .activity_summary import COST_WINDOWS, WINDOW_SECONDS

MAX_SESSIONS = 1024
MAX_FACTS = 128
SECONDS = 1


def queue(db, source, key):
    db.execute(
        "INSERT OR IGNORE INTO attribution_dirty VALUES (?,?)", (source, json.dumps(list(key), separators=(",", ":")))
    )


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS attribution_dirty(source TEXT,key TEXT,PRIMARY KEY(source,key));
      CREATE TABLE IF NOT EXISTS attribution_prs(repo TEXT,number INTEGER,payload TEXT,PRIMARY KEY(repo,number));
      CREATE TABLE IF NOT EXISTS attribution_reports(subject TEXT PRIMARY KEY,payload TEXT,next_at REAL);
      CREATE TABLE IF NOT EXISTS attribution_due(repo TEXT,number INTEGER,at REAL,PRIMARY KEY(repo,number));
      CREATE INDEX IF NOT EXISTS attribution_due_at ON attribution_due(at);
      CREATE INDEX IF NOT EXISTS attribution_round_pr ON round_prs(repo,number);
      CREATE INDEX IF NOT EXISTS attribution_pr_worker ON prs(worker,created);
      CREATE INDEX IF NOT EXISTS attribution_contribution_worker ON pr_contributions(worker,at);
      CREATE INDEX IF NOT EXISTS invocation_pr ON invocation_work(repo,number);
      CREATE INDEX IF NOT EXISTS invocation_round ON invocation_work(round_id);
      CREATE INDEX IF NOT EXISTS attribution_session_worker ON cost_sessions(worker,started);
      CREATE INDEX IF NOT EXISTS attribution_round_worker ON rounds(worker,started DESC);
      CREATE INDEX IF NOT EXISTS attribution_archive_next ON review_archive_checks(checked);
    """)
    upgrade = db.execute("INSERT OR IGNORE INTO meta VALUES ('attribution-triggers-v3','1')").rowcount
    for table, source, keys, fields in (
        ("cost_sessions", "cost", ["id"], ["data", "scope"]),
        ("cost_accruals", "cost", ["id"], None),
        ("rounds", "round", ["id"], ["worker", "started", "finished", "outcome"]),
        ("prs", "receipt", ["repo", "number"], ["worker", "scope", "created", "landed", "head", "state"]),
        ("pr_contributions", "receipt", ["repo", "number"], None),
        ("pr_observations", "pr", ["repo", "number"], ["head", "landed", "state"]),
        ("pr_loc", "pr", ["repo", "number"], ["head", "added"]),
        ("pr_review_counts", "pr", ["repo", "number"], ["head", "count", "observed", "basis"]),
        ("review_archive_records", "pr", ["repo", "number"], ["payload"]),
        ("review_archive_checks", "pr", ["repo", "number"], ["complete"]),
    ):
        for action in ("INSERT", "UPDATE", "DELETE"):
            prefix = "OLD" if action == "DELETE" else "NEW"
            expression = "json_array(" + ",".join(prefix + "." + k for k in keys) + ")"
            changed = (
                " WHEN " + " OR ".join("NEW." + f + " IS NOT OLD." + f for f in fields)
                if fields and action == "UPDATE"
                else ""
            )
            if upgrade:
                db.execute(f"DROP TRIGGER IF EXISTS attribution_{table}_{action}")
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS attribution_{table}_{action}
                AFTER {action} ON {table}{changed} BEGIN
                INSERT OR IGNORE INTO attribution_dirty VALUES ('{source}',{expression});
                {f"UPDATE review_archive_checks SET checked=0 WHERE repo={prefix}.repo AND number={prefix}.number;" if table in {"prs", "pr_observations", "pr_review_counts"} else ""} END""")
    if db.execute("INSERT OR IGNORE INTO meta VALUES ('attribution-seed-v1','1')").rowcount:
        db.execute("INSERT OR IGNORE INTO attribution_dirty SELECT 'legacy_cost',json_array(id) FROM cost_sessions")
        db.execute("INSERT OR IGNORE INTO attribution_dirty SELECT 'legacy_receipt',json_array(repo,number) FROM prs")
        db.execute(
            "INSERT OR IGNORE INTO attribution_dirty SELECT 'legacy_receipt',json_array(repo,number) FROM pr_contributions"
        )
        db.execute("INSERT OR IGNORE INTO review_archive_checks SELECT repo,number,0,0,NULL FROM prs")
        db.execute("INSERT OR IGNORE INTO review_archive_checks SELECT repo,number,0,0,NULL FROM pr_contributions")

    if db.execute("INSERT OR IGNORE INTO meta VALUES ('attribution-facts-v2','1')").rowcount:
        db.execute(
            "INSERT OR IGNORE INTO attribution_dirty SELECT 'legacy_pr',json_array(repo,number) FROM attribution_prs"
        )


def rebuild_cost_queue(db, active):
    """Ledger reconstruction is backfill; current paid invocations remain live."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='attribution_dirty'").fetchone():
        return
    db.execute(
        "INSERT OR IGNORE INTO attribution_dirty SELECT 'legacy_cost',key FROM attribution_dirty WHERE source='cost'"
    )
    db.execute("DELETE FROM attribution_dirty WHERE source='cost'")
    for identity in active:
        queue(db, "cost", [identity])


def round_sessions(db, rid, now, source="cost"):
    for row in db.execute("SELECT id FROM invocation_work WHERE round_id=?", (rid,)):
        if (
            source == "legacy_cost"
            and db.execute("SELECT 1 FROM invocation_work WHERE id=? AND repo IS NOT NULL", tuple(row)).fetchone()
        ):
            continue
        queue(db, source, row)
    round = db.execute("SELECT worker,started,finished FROM rounds WHERE id=?", (rid,)).fetchone()
    if round and round[1] is not None:
        for row in db.execute(
            "SELECT id FROM cost_sessions WHERE worker=? AND started BETWEEN ? AND ?",
            (round[0], round[1], round[2] if round[2] is not None else now),
        ):
            if (
                source == "legacy_cost"
                and db.execute("SELECT 1 FROM invocation_work WHERE id=? AND repo IS NOT NULL", tuple(row)).fetchone()
            ):
                continue
            queue(db, source, row)


def receipt(db, key, now, legacy=False):
    repo, number = key
    queue(db, "legacy_pr" if legacy else "pr", key)
    db.execute("INSERT OR IGNORE INTO review_archive_checks VALUES (?,?,0,0,NULL)", key)
    targets = a.CostTargets(db)
    for worker, at in db.execute(
        "SELECT worker,created FROM prs WHERE repo=? AND number=? UNION ALL "
        "SELECT worker,at FROM pr_contributions WHERE repo=? AND number=?",
        (*key, *key),
    ):
        if at is not None and (rid := targets.containing(worker, at)):
            db.execute("INSERT OR IGNORE INTO round_prs VALUES (?,?,?)", (rid, repo, number))
            round_sessions(db, rid, now, "legacy_cost" if legacy else "cost")


def invocation(db, identity, legacy=False):
    source = "legacy_pr" if legacy else "pr"
    previous = db.execute("SELECT repo,number FROM invocation_work WHERE id=?", (identity,)).fetchone()
    if previous and previous[0]:
        queue(db, source, previous)
    raw = db.execute("SELECT data FROM cost_sessions WHERE id=?", (identity,)).fetchone()
    if raw:
        session = json.loads(raw[0])
        a.register_worker_paths(db, session["worker"])
        row = a.CostTargets(db).resolve(session)
        old = db.execute("SELECT * FROM invocation_work WHERE id=?", (identity,)).fetchone()
        if old is None or tuple(old) != row:
            db.execute("INSERT OR REPLACE INTO invocation_work VALUES (?,?,?,?,?)", row)
        if row[3]:
            queue(db, source, row[3:])
            if row[1] == "review" and not legacy:
                db.execute("UPDATE review_archive_checks SET checked=0 WHERE repo=? AND number=?", row[3:])
    else:
        db.execute("DELETE FROM invocation_work WHERE id=?", (identity,))
    db.execute(
        "DELETE FROM attribution_dirty WHERE source='legacy_cost' AND key=?",
        (json.dumps([identity], separators=(",", ":")),),
    )


def fact(db, key, now):
    repo, number = key
    raw = db.execute("SELECT * FROM prs WHERE repo=? AND number=?", key).fetchone()
    owned = dict(raw) if raw else None
    raw = raw or db.execute("SELECT * FROM pr_observations WHERE repo=? AND number=?", key).fetchone()
    p = dict(raw) if raw else {}
    loc = db.execute("SELECT head,added FROM pr_loc WHERE repo=? AND number=?", key).fetchone()
    end = min(now, p.get("landed") or now)
    work = {}
    future = [p["landed"]] if p.get("landed") is not None and p["landed"] > now else []
    for phase, rid, worker, scope, start, finish, cost, outcome, round_start, round_finish in db.execute(
        "SELECT w.phase,w.round_id,c.worker,c.scope,c.started,c.finished,c.cost,r.outcome,r.started,r.finished "
        "FROM invocation_work w INDEXED BY invocation_pr CROSS JOIN cost_accruals c ON c.id=w.id "
        "LEFT JOIN rounds r ON r.id=w.round_id WHERE w.repo=? AND w.number=?",
        key,
    ):
        kind = (
            "author"
            if phase in a.AUTHOR_PHASES
            else "fix"
            if phase in a.FIX_PHASES
            else "review"
            if phase == "review"
            else None
        )
        if not kind or start is None or finish is None:
            continue
        if start > now:
            future.append(start)
        if start > end:
            continue
        value = Decimal(cost)
        if finish > start:
            value *= Decimal(str(max(0, min(finish, end) - start))) / Decimal(str(finish - start))
        row = work.setdefault(
            (worker, scope),
            dict(author="0", fix="0", review="0", author_known=False, passes={}, attempted={}, unpriced=False),
        )
        if kind == "review" and rid and value > 0:
            row["attempted"][rid] = [round_start, round_finish]
        if outcome not in {"completed", "unconfirmed"}:
            continue  # Failed attempts are charged as overhead, never output.
        row[kind] = str(Decimal(row[kind]) + value)
        if kind == "author":
            row["author_known"] = True
        if kind == "review" and rid and value > 0:
            sample = row["passes"].setdefault(rid, dict(cost="0", at=start))
            sample["cost"] = str(Decimal(sample["cost"]) + value)
            sample["at"] = max(start, sample["at"])
    for rid, worker, scope in db.execute(
        "SELECT DISTINCT r.id,r.worker,r.scope FROM invocation_work w JOIN rounds r ON r.id=w.round_id "
        "WHERE w.repo=? AND w.number=? AND w.phase='review' AND r.outcome IN ('completed','unconfirmed') "
        "AND r.started<=? AND NOT EXISTS (SELECT 1 FROM cost_accruals c WHERE c.id=w.id)",
        (*key, end),
    ):
        row = work.setdefault(
            (worker, scope),
            dict(author="0", fix="0", review="0", author_known=False, passes={}, attempted={}, unpriced=False),
        )
        if rid not in row["passes"]:
            row["unpriced"] = True
    passes = set()
    archived_rounds = {}
    for row in db.execute("SELECT payload FROM review_archive_records WHERE repo=? AND number=?", key):
        record = json.loads(row[0])
        if record and record["review"]:
            if record["at"] <= end:
                passes.add(record["review"])
                if record.get("round_id"):
                    archived_rounds[record["round_id"]] = record["at"]
            elif record["at"] > now:
                future.append(record["at"])
    checked = db.execute("SELECT complete,checked FROM review_archive_checks WHERE repo=? AND number=?", key).fetchone()
    fetched = db.execute("SELECT MAX(fetched) FROM review_archive_sources").fetchone()[0] or 0
    fresh = (
        p.get("landed") is not None and fetched >= p["landed"] + a.ARCHIVE_GRACE and checked and checked[1] >= fetched
    )
    counter = db.execute("SELECT head,count,observed FROM pr_review_counts WHERE repo=? AND number=?", key).fetchone()
    cached_counts = bool(
        counter
        and counter[1] is not None
        and counter[0] == p.get("head")
        and p.get("landed") is not None
        and counter[2] is not None
        and counter[2] >= 0
    )
    value = dict(
        version=2,
        key=list(key),
        landed=p.get("landed"),
        loc=loc[1] if loc and loc[0] == p.get("head") else None,
        owner=[owned["worker"], owned["scope"]] if owned else None,
        work=[[worker, scope, row] for (worker, scope), row in work.items()],
        total_runs=counter[1] if cached_counts else len(passes),
        review_known=cached_counts or bool(checked and checked[0] and passes and fresh),
        archived_rounds=archived_rounds,
        counter_basis=cached_counts,
        review_observed_at=counter[2] if cached_counts else fetched or None,
    )
    payload = json.dumps(value)
    previous = db.execute("SELECT payload FROM attribution_prs WHERE repo=? AND number=?", key).fetchone()
    changed = not previous or previous[0] != payload
    if changed:
        db.execute("INSERT OR REPLACE INTO attribution_prs VALUES (?,?,?)", (repo, number, payload))
    db.execute("DELETE FROM attribution_due WHERE repo=? AND number=?", key)
    if future:
        db.execute("INSERT INTO attribution_due VALUES (?,?,?)", (repo, number, min(future)))
    return changed


def reports(db, now):
    profiles = defaultdict(list)
    next_at = now + 7 * 86400
    for (raw,) in db.execute("SELECT payload FROM attribution_prs"):
        p = json.loads(raw)
        groups = defaultdict(list)
        owners = set()
        if p["owner"]:
            worker, scope = p["owner"]
            owners.update([(None, None), ("worker", worker), ("scope", scope)])
        for worker, scope, work in p["work"]:
            for profile in ((None, None), ("worker", worker), ("scope", scope)):
                groups[profile].append(work)
        for profile in owners | groups.keys():
            work = groups[profile]
            passes = {}
            attempted = {}
            for w in work:
                attempted.update(w.get("attempted", {}))
                for rid, sample in w["passes"].items():
                    passes[rid] = sample
            row = dict(
                key=p["key"],
                landed=p["landed"],
                loc=p["loc"],
                owned=profile in owners,
                production=sum((Decimal(w["author"]) + Decimal(w["fix"]) for w in work), Decimal(0)),
                review=sum((Decimal(w["review"]) for w in work), Decimal(0)),
                author_known=any(w["author_known"] for w in work),
                local_runs=len(passes),
                attempted_runs=len(passes)
                if p.get("counter_basis")
                else len(
                    set(passes)
                    | {
                        rid
                        for rid, bounds in attempted.items()
                        if isinstance(bounds, list)
                        and len(bounds) == 2
                        and bounds[0] is not None
                        and bounds[1] is not None
                        and any(
                            bounds[0] <= at <= bounds[1]
                            for at in (
                                p.get("archived_rounds") if isinstance(p.get("archived_rounds"), dict) else {}
                            ).values()
                        )
                    }
                ),
                total_runs=max(p["total_runs"], len(passes)) if p.get("counter_basis") else p["total_runs"],
                review_known=p["review_known"]
                and (p.get("counter_basis") or p["total_runs"] >= len(passes))
                and not any(w["unpriced"] for w in work),
                review_observed_at=p.get("review_observed_at"),
                samples=[(json.dumps([p["key"], rid]), sample["cost"], sample["at"]) for rid, sample in passes.items()],
            )
            profiles[profile].append(row)
            for _, _, at in row["samples"]:
                if at + 7 * 86400 > now:
                    next_at = min(next_at, at + 7 * 86400 + 0.000001)
        if p["landed"] is not None:
            for seconds in (0, 86400, 7 * 86400):
                if p["landed"] + seconds > now:
                    next_at = min(next_at, p["landed"] + seconds + 0.000001)
    profiles.setdefault((None, None), [])
    db.execute("DELETE FROM attribution_reports")
    windows = {w: WINDOW_SECONDS[w] for w in COST_WINDOWS}
    all_values = dict(a.calculate(profiles[None, None], now, windows))
    reference = dict(
        production=Decimal(all_values["total"]["production_usd_per_loc"])
        if all_values["total"]["production_usd_per_loc"] is not None
        else None,
        review=Decimal(all_values["total"]["review_usd_per_pass"])
        if all_values["total"]["review_usd_per_pass"] is not None
        else None,
        totals={
            tuple(row["key"]): row["estimated_total"]
            for row in profiles[None, None]
            if row["landed"] is not None and row["landed"] <= now
        },
    )
    for profile, rows in profiles.items():
        values = all_values if profile == (None, None) else dict(a.calculate(rows, now, windows, reference))
        db.execute("INSERT INTO attribution_reports VALUES (?,?,?)", (json.dumps(profile), json.dumps(values), next_at))


def prepare(db, now):
    deadline = time.monotonic() + SECONDS
    changed = False
    for source in ("round", "receipt", "cost", "pr", "legacy_cost", "legacy_pr", "legacy_receipt"):
        if source == "pr":
            started = time.monotonic()
            priority = [
                json.loads(r[0])
                for r in db.execute("SELECT key FROM attribution_dirty WHERE source='pr' LIMIT ?", (a.MAX_PRS,))
            ]
            a.prepare_reviews(db, now, priority)
            deadline += time.monotonic() - started
        maximum = MAX_SESSIONS if source in {"cost", "legacy_cost"} else MAX_FACTS
        keys = [r[0] for r in db.execute("SELECT key FROM attribution_dirty WHERE source=? LIMIT ?", (source, maximum))]
        for encoded in keys:
            if time.monotonic() >= deadline:
                break
            key = json.loads(encoded)
            db.execute("DELETE FROM attribution_dirty WHERE source=? AND key=?", (source, encoded))
            if source == "round":
                round_sessions(db, key[0], now)
            elif source == "receipt":
                receipt(db, key, now)
            elif source == "cost":
                invocation(db, key[0])
            elif source == "legacy_cost":
                invocation(db, key[0], legacy=True)
            elif source == "legacy_receipt":
                receipt(db, key, now, legacy=True)
            else:
                changed = fact(db, key, now) or changed
    for repo, number in db.execute("SELECT repo,number FROM attribution_due WHERE at<=? LIMIT ?", (now, MAX_FACTS)):
        changed = fact(db, (repo, number), now) or changed
    if changed:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('attribution-report-dirty','1')")
    last = db.execute("SELECT value FROM meta WHERE key='attribution-report-last'").fetchone()
    small = len(db.execute("SELECT 1 FROM attribution_prs LIMIT 129").fetchall()) <= 128
    pending = db.execute("SELECT 1 FROM meta WHERE key='attribution-report-dirty'").fetchone()
    cached = db.execute("SELECT next_at FROM attribution_reports LIMIT 1").fetchone()
    if not cached or cached[0] <= now or (pending and (small or not last or float(last[0]) <= now - 300)):
        reports(db, now)
        db.execute("DELETE FROM meta WHERE key='attribution-report-dirty'")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('attribution-report-last',?)", (str(now),))

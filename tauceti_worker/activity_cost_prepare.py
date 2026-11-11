"""Prepare only ledger changes, live invocations, and changed response owners."""

import json
import sys
import time
import uuid
from decimal import Decimal

from .activity_costs import cached_usage, usage_signature
from .activity_ledger import load
from .activity_live import calibrate, cohort
from .budget import Budget, money
from .budget_recovery import interval, live_identity


def stored_usage(db, identity):
    row = db.execute("SELECT payload FROM prepared_usage WHERE id=?", (identity,)).fetchone()
    return json.loads(row[0]) if row else None


def write_rows(db, table, identity, rows):
    existing = [tuple(r) for r in db.execute(f"SELECT * FROM {table} WHERE id=? ORDER BY rowid", (identity,))]
    if existing == [tuple(r) for r in rows]:
        return
    db.execute(f"DELETE FROM {table} WHERE id=?", (identity,))
    if rows:
        db.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows)


def winner(db, provider, message):
    row = db.execute(
        "SELECT invocation FROM cost_messages WHERE provider=? AND message=? ORDER BY started,ordinal,invocation LIMIT 1",
        (provider, message),
    ).fetchone()
    return row[0] if row else None


def allocate(db, budget, session, now, rates):
    identity = session["id"]
    stored = db.execute("SELECT scope,ordinal FROM cost_sessions WHERE id=?", (identity,)).fetchone()
    scope, ordinal = stored
    settled = session["status"] == "settled"
    value = session.get("cost", "0")
    saved = stored_usage(db, identity)
    items = [
        json.loads(row[0])
        for row in db.execute(
            "SELECT m.payload FROM cost_messages m WHERE m.invocation=? AND NOT EXISTS ("
            "SELECT 1 FROM cost_messages earlier WHERE earlier.provider=m.provider AND earlier.message=m.message "
            "AND (earlier.started,earlier.ordinal,earlier.invocation)<(m.started,m.ordinal,m.invocation)) "
            "ORDER BY m.rowid",
            (identity,),
        )
    ]
    priced = [item for item in items if item["cost"] is not None]
    total = sum((money(item["cost"]) for item in priced), Decimal(0))
    native = settled and session.get("cost_provenance", "native-reported") == "native-reported"
    previous_cohorts = {r[0] for r in db.execute("SELECT cohort FROM cost_calibration WHERE invocation=?", (identity,))}
    if (
        native
        and saved
        and saved.get("closed")
        and not saved.get("inflight")
        and not session.get("resume")
        and session.get("outcome") not in {"launch-failed", "reconciled"}
        and money(value) > 0
        and saved.get("journal_format")
        and saved.get("journal_offset", 0) > saved.get("journal_start_bytes", 0)
        and saved.get("messages")
        and all(m["complete"] and m["cost"] is not None for m in saved["messages"].values())
    ):
        sample = dict(
            model=session["model"],
            phase=session["phase"],
            cost=value,
            journal_format=saved["journal_format"],
            prices_sha256=saved.get("prices_sha256"),
            bytes=saved["journal_offset"] - saved["journal_start_bytes"],
        )
        desired = {cohort(session["model"], phase, sample): sample for phase in (session["phase"], None)}
    else:
        desired = {}
    db.execute("DELETE FROM cost_calibration WHERE invocation=?", (identity,))
    db.executemany(
        "INSERT INTO cost_calibration VALUES(?,?,?,?,?)",
        ((key, identity, session["started"], ordinal, json.dumps(sample)) for key, sample in desired.items()),
    )
    for key in previous_cohorts | desired.keys():
        samples = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM cost_calibration WHERE cohort=? ORDER BY started DESC,ordinal DESC LIMIT 128",
                (key,),
            )
        ][::-1]
        calibrated = calibrate(samples)
        if key in calibrated:
            rates[key] = calibrated[key]
        else:
            rates.pop(key, None)
    if not settled:
        value = str(max(money(value), total, money(session.get("lower_bound_cost", "0"))))
    incomplete = not settled and (
        not saved
        or not saved["closed"]
        or saved["inflight"]
        or any(not item["complete"] or item["cost"] is None for item in items)
    )
    live = not settled and live_identity(session, now, budget.directory)["live"]
    kind = 0 if settled else 1 if live else 2
    accruals = []
    if total > 0:
        allocated = Decimal(0)
        for index, item in enumerate(priced):
            part = money(value) - allocated if index == len(priced) - 1 else money(value) * money(item["cost"]) / total
            allocated += part
            accruals.append(
                (
                    identity,
                    session["worker"],
                    scope,
                    item["at"],
                    item["at"],
                    str(part),
                    "native-allocated" if native else "token-priced",
                    kind,
                )
            )
    elif money(value) > 0:
        recorded = interval(session, now)
        finish = (
            float(recorded["finish"])
            if recorded["finish"] is not None
            else session["started"]
            if settled
            else saved["observed_at"]
            if saved
            else now
        )
        accruals.append(
            (
                identity,
                session["worker"],
                scope,
                session["started"],
                max(session["started"], finish),
                value,
                "interval-estimate",
                kind,
            )
        )
    moving = bool(not settled and not saved and money(value) > 0 and interval(session, now)["finish"] is None)
    db.execute("UPDATE cost_sessions SET moving=? WHERE id=? AND moving!=?", (int(moving), identity, int(moving)))
    if money(value) < 0:
        raise ValueError("Negative conversation cost")
    write_rows(
        db,
        "conversation_costs",
        identity,
        [
            (
                identity,
                session["worker"],
                scope,
                session["started"],
                session.get("_activity_settled_at") if settled else None,
                value,
            )
        ],
    )
    write_rows(db, "cost_accruals", identity, accruals)
    write_rows(
        db,
        "conversation_quality",
        identity,
        [(identity, session["worker"], scope, int(native), int(not native), int(incomplete))],
    )
    write_rows(
        db,
        "conversation_accounting",
        identity,
        [
            (
                identity,
                session["worker"],
                scope,
                int(live),
                int(settled and not native),
                int(settled and bool(session.get("recovery_audit")) and not native),
                int(not settled and not live),
            )
        ],
    )


def register_usage(
    db, budget, state, identity, changed, replay, now, scopes, scope_for_worker, metrics, owners, messages, retry=False
):
    session = state["sessions"].get(identity)
    if not session:
        return False
    stored = db.execute("SELECT ordinal,scope,data,moving FROM cost_sessions WHERE id=?", (identity,)).fetchone()
    previous = db.execute("SELECT scope FROM conversation_costs WHERE id=?", (identity,)).fetchone()
    if session.get("scope") is not None:
        scope = session["scope"]
    elif stored:
        scope = stored[1]
    elif previous:
        scope = previous[0]
    else:
        worker = session["worker"]
        if worker not in scopes:
            scopes[worker] = scope_for_worker(worker)
        scope = scopes[worker]
    raw = json.dumps(session)
    watch = db.execute("SELECT 1 FROM cost_watch WHERE id=?", (identity,)).fetchone()
    valid_id = len(identity) == 32 and all(c in "0123456789abcdef" for c in identity)
    signature = None
    if stored and identity not in changed and not watch and not retry:
        metrics["active_checked"] += 1
        old_signature = db.execute("SELECT signature FROM prepared_usage WHERE id=?", (identity,)).fetchone()
        signature = usage_signature(budget, session, metrics) if valid_id else None
        live = live_identity(session, now, budget.directory)["live"]
        prior_live = db.execute("SELECT live FROM conversation_accounting WHERE id=?", (identity,)).fetchone()
        if (
            stored[1] == scope
            and stored[2] == raw
            and (old_signature[0] if old_signature else None) == signature
            and prior_live
            and bool(prior_live[0]) == live
            and not stored[3]
        ):
            if hasattr(state["sessions"], "changed"):
                state["sessions"].changed.clear()
            return False
    if stored:
        ordinal = stored[0]
    elif hasattr(state["sessions"], "db"):
        ordinal = state["sessions"].db.execute("SELECT ordinal FROM sessions WHERE id=?", (identity,)).fetchone()[0]
    else:
        ordinal = state["_activity_ordinals"][identity]
    db.execute(
        "INSERT OR REPLACE INTO cost_sessions VALUES(?,?,?,?,?,?,0)",
        (
            identity,
            session["worker"],
            scope,
            session["started"],
            ordinal,
            raw,
        ),
    )
    saved = stored_usage(db, identity)
    # Settled journals are frozen. Only recent, explicitly tracked final
    # checkpoint races get a bounded retry; old unclosed files do not.
    if valid_id and (session["status"] != "settled" or not saved or identity in changed or watch):
        saved = cached_usage(db, budget, session, metrics, signature=signature)
    if (
        valid_id
        and session["status"] == "settled"
        and session.get("_activity_settled_at", 0) >= now - 300
        and not (saved and saved.get("closed"))
    ):
        db.execute("INSERT OR IGNORE INTO cost_watch VALUES(?,?)", (identity, session["_activity_settled_at"] + 300))
    elif saved and saved.get("closed"):
        db.execute("DELETE FROM cost_watch WHERE id=?", (identity,))
    before = (
        []
        if replay["rebuilt"]
        else list(db.execute("SELECT provider,message FROM cost_messages WHERE invocation=?", (identity,)))
    )
    after = {(m["session"], m["message_id"]): m for m in (saved or {}).get("messages", {}).values()}
    keys = set() if replay["rebuilt"] else {tuple(r) for r in before} | after.keys()
    for provider, message in keys:
        owner = winner(db, provider, message)
        if owner:
            owners.add(owner)
    messages.update(keys)
    if not replay["rebuilt"]:
        owners.add(identity)
    db.execute("DELETE FROM cost_messages WHERE invocation=?", (identity,))
    db.executemany(
        "INSERT INTO cost_messages VALUES(?,?,?,?,?,?)",
        (
            (provider, message, identity, session["started"], ordinal, json.dumps(payload))
            for (provider, message), payload in after.items()
        ),
    )
    metrics["processed_invocations"] += 1
    if hasattr(state["sessions"], "changed"):
        state["sessions"].changed.clear()
    return True


def record_failure(db, identity, now, error):
    prior = db.execute("SELECT attempts FROM cost_failed WHERE id=?", (identity,)).fetchone()
    attempts = (prior[0] if prior else 0) + 1
    db.execute(
        "INSERT OR REPLACE INTO cost_failed VALUES(?,?,?,?)",
        (identity, now + min(300, 60 * 2 ** min(attempts - 1, 3)), attempts, type(error).__name__ + ": " + str(error)),
    )
    # This cache has no unique evidence. Retry the native journal for just this
    # invocation rather than pinning the entire financial cursor on bad JSON.
    db.execute("DELETE FROM prepared_usage WHERE id=?", (identity,))


def prepare(db, now, scope_for_worker):
    started, cpu = time.monotonic(), time.process_time()
    metrics = dict(
        usage_metadata_checks=0,
        usage_decodes=0,
        usage_decode_seconds=0,
        processed_invocations=0,
        response_owners_reallocated=0,
        active_checked=0,
    )
    budget = Budget(clock=lambda: now)
    if not budget.ledger.exists():
        if db.execute("SELECT 1 FROM meta WHERE key='activity_funding'").fetchone():
            raise ValueError("Budget ledger unavailable")
        return
    old = db.execute("SELECT value FROM meta WHERE key='activity_funding'").fetchone()
    old = json.loads(old[0]) if old else {}
    with load(db, budget) as (state, changed, replay):
        metrics.update(
            {k: v for k, v in replay.items() if k != "cursor"}, ledger_replay_seconds=time.monotonic() - started
        )
        phase = time.monotonic()
        budget.accrue(state)
        rates = dict(old.get("live_rates", {}))
        count = state.get("_activity_invocations", len(state["sessions"]) if isinstance(state["sessions"], dict) else 0)
        if replay["rebuilt"]:
            rates = {}
            for table in (
                "cost_sessions",
                "cost_messages",
                "cost_calibration",
                "cost_watch",
                "cost_failed",
            ):
                db.execute(f"DELETE FROM {table}")
        if isinstance(state["sessions"], dict):
            state["_activity_ordinals"] = {key: index for index, key in enumerate(state["sessions"])}
        active = {s["id"]: s for s in budget.sessions(state, active=True)}
        dirty = (
            set(changed)
            | active.keys()
            | {r[0] for r in db.execute("SELECT id FROM cost_watch WHERE deadline>=?", (now,))}
        )
        db.execute("DELETE FROM cost_watch WHERE deadline<?", (now,))
        dirty.update(
            row[0]
            for row in db.execute("SELECT id FROM cost_failed WHERE next_at<=? ORDER BY next_at LIMIT 256", (now,))
        )
        scopes, owners, messages, registration_failed = {}, set(), set(), set()
        for identity in sorted(dirty):
            failure = db.execute("SELECT next_at,attempts FROM cost_failed WHERE id=?", (identity,)).fetchone()
            if failure and failure[0] > now and identity not in changed:
                continue
            db.execute("SAVEPOINT register_usage")
            try:
                register_usage(
                    db,
                    budget,
                    state,
                    identity,
                    changed,
                    replay,
                    now,
                    scopes,
                    scope_for_worker,
                    metrics,
                    owners,
                    messages,
                    retry=bool(failure),
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                db.execute("ROLLBACK TO register_usage")
                record_failure(db, identity, now, error)
                registration_failed.add(identity)
            finally:
                db.execute("RELEASE register_usage")
                if hasattr(state["sessions"], "changed"):
                    state["sessions"].changed.clear()
        for provider, message in messages:
            owner = winner(db, provider, message)
            if owner:
                owners.add(owner)
        if replay["rebuilt"]:
            # Rebuilds stream current sessions; they never retain all response
            # keys or all decoded session dictionaries in Python memory.
            owners = (row[0] for row in db.execute("SELECT id FROM cost_sessions ORDER BY started,ordinal"))
        else:
            owners = sorted(owners)
        for identity in owners:
            if identity in registration_failed:
                continue
            row = db.execute("SELECT data FROM cost_sessions WHERE id=?", (identity,)).fetchone()
            if row:
                db.execute("SAVEPOINT allocate_usage")
                proposed = dict(rates)
                try:
                    allocate(db, budget, json.loads(row[0]), now, proposed)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    db.execute("ROLLBACK TO allocate_usage")
                    record_failure(db, identity, now, error)
                else:
                    rates = proposed
                    db.execute("DELETE FROM cost_failed WHERE id=?", (identity,))
                    metrics["response_owners_reallocated"] += 1
                finally:
                    db.execute("RELEASE allocate_usage")
        if replay["rebuilt"]:
            for table in (
                "conversation_costs",
                "cost_accruals",
                "conversation_quality",
                "conversation_accounting",
                "prepared_usage",
            ):
                db.execute(f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM cost_sessions)")
        metrics["usage_and_allocation_seconds"] = time.monotonic() - phase
        info = budget.ledger.stat()
        pending = sum((money(s.get("reserve", s["estimate"])) for s in active.values()), Decimal(0))
        funding = dict(
            balance=str(state["balance"]),
            pending=str(pending),
            available=str(state["balance"] - pending),
            grant_rate=str(state["rate"]),
            prepared_at=now,
            ledger_sequence=state["seq"],
            ledger_identity=[info.st_dev, info.st_ino],
            generation=uuid.uuid4().hex,
            live_rates=rates,
        )
        metrics.update(
            table_write_seconds=0,
            seconds=time.monotonic() - started,
            cpu_seconds=time.process_time() - cpu,
            ledger_bytes=info.st_size,
            invocations=count,
        )
        try:
            import resource

            metrics["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                1 if sys.platform == "darwin" else 1024
            )
        except ImportError:
            pass
        funding["preparation_metrics"] = metrics
        db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_funding',?)", (json.dumps(funding),))
        if replay["cursor"]:
            db.execute(
                "INSERT INTO meta VALUES ('activity_cost_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value",
                (json.dumps(replay["cursor"]),),
            )
        failed = db.execute("SELECT count(*) FROM cost_failed").fetchone()[0]
        if failed:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES('activity_cost_error',?)",
                (f"{failed} invocations awaiting local evidence repair",),
            )
        else:
            db.execute("DELETE FROM meta WHERE key='activity_cost_error'")

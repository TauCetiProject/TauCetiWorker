"""Deterministic stopped-session repair. Plans and audit files contain no content."""

import contextlib
import copy
import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path
from statistics import median

from . import budget as accounting
from .budget import ESTIMATE_PROVENANCES, Budget, BudgetError, money
from .budget_usage import evidence
from .claude_api import safe_message
from .runtime_status import atomic_json

POLICY = "stopped-usage-v1"
WEEK = 7 * 86400


def live_identity(session, now):
    owner = accounting.bridge_owner_alive(session.get("owner"))
    heartbeat = None
    if session.get("bridge"):
        try:
            heartbeat = (Path(session["bridge"]) / "outbox/heartbeat").stat().st_mtime
        except FileNotFoundError:
            pass
    live = owner or heartbeat is not None and now - heartbeat <= 30
    return dict(live=bool(live), owner=session.get("owner"), owner_alive=owner, heartbeat=heartbeat)


def interval(session, now, saved=None):
    """Use recorded endpoints only; repair time is never a stopped session's end."""
    start = session.get("started")
    finish = session.get("finished")
    source = "ledger-terminal-event"
    if finish is None and saved:
        finish = saved["checkpoint"].get("observed_at")
        source = "usage-observed-at"
    try:
        if isinstance(start, bool) or isinstance(finish, bool) or start is None or finish is None:
            raise ValueError
        start, finish = money(start), money(finish)
        if start < 0 or finish <= start or finish > money(now):
            raise ValueError
        return dict(start=str(start), finish=str(finish), seconds=str(finish - start), source=source)
    except ValueError:
        return dict(start=None, finish=None, seconds=None, source="unusable-recorded-duration")


def historical_estimate(session, sessions, now, duration):
    audit = dict(policy=POLICY, duration=duration, comparable_ids=[], rate_usd_per_second=None)
    if duration["seconds"] is not None:
        candidates = []
        # The seven-day window precedes the invocation, not the time of repair.
        end = money(session["started"])
        for other in sessions.values():
            if (
                other["id"] == session["id"]
                or other["model"] != session["model"]
                or other["status"] != "settled"
                or other.get("cost_provenance", "native-reported") != "native-reported"
                or ("estimat" in other.get("outcome", "").lower() and not other.get("totals"))
                or other.get("outcome") == "reconciled"
            ):
                continue
            span = interval(other, now)
            if span["seconds"] is None or not end - WEEK <= money(span["finish"]) <= end:
                continue
            cost = money(other["cost"])
            if cost < 0:
                continue
            candidates.append((other, cost / money(span["seconds"])))
        phased = [(other, rate) for other, rate in candidates if other["phase"] == session["phase"]]
        selected = phased if len(phased) >= 3 else candidates
        if len(selected) >= 3:
            rate = median(rate for _, rate in selected)
            audit.update(
                tier="model-phase" if len(phased) >= 3 else "model",
                comparable_ids=[s["id"] for s, _ in selected],
                comparables=[dict(id=s["id"], cost=s["cost"], duration=interval(s, now)) for s, _ in selected],
                rate_usd_per_second=str(rate),
                window_start=str(end - WEEK),
                window_end=str(end),
            )
            return (
                (rate * money(duration["seconds"])).quantize(Decimal("0.000000000001")),
                "historical-estimate",
                audit,
            )
    audit.update(tier="prelaunch", saved_prelaunch_estimate=session["estimate"])
    return money(session["estimate"]), "prelaunch-estimate", audit


class Projection(Budget):
    """Run the real cumulative receipt reconciliation against a private ledger."""

    def __init__(self, budget, records, now):
        super().__init__(budget.directory, clock=lambda: now)
        self.projected_records = copy.deepcopy(records)
        self.events = []

    def locked(self):
        return contextlib.nullcontext()

    def replay(self, records=None):
        # Budget replay updates admission dictionaries in place.
        return super().replay(copy.deepcopy(self.projected_records if records is None else records))

    def append(self, state, events):
        for event in events:
            if event["type"] == "settle" and event.get("cost_provenance") == "native-reported":
                # Replaying an old receipt now does not make downtime part of its duration.
                event["finished"] = state["sessions"][event["id"]].get("finished")
        self.events.extend(e for e in events if e["type"] != "accrue")
        self.projected_records.append(json.loads(self.record_bytes(state, events)))


def pending(session):
    return Decimal(0) if session["status"] == "settled" else money(session.get("reserve", session["estimate"]))


def recover(budget, note, *, dry_run=False):
    if not dry_run and not note.strip():
        raise BudgetError("receipt recovery requires --note")
    now = budget.clock()
    with budget.locked():
        records = budget.records()
        before = budget.replay(copy.deepcopy(records))
        original = before["sessions"]
        identities = {key: live_identity(s, now) for key, s in original.items()}
        projection = Projection(budget, records, now)
        rows, errors, checkpoints = [], [], []
        usage_evidence = {}
        for key, session in original.items():
            row = dict(
                id=key,
                worker=session["worker"],
                model=session["model"],
                phase=session["phase"],
                identity=identities[key],
                source="existing-ledger",
                native_amount_usd=session.get("cost")
                if session["status"] == "settled" and session.get("cost_provenance") == "native-reported"
                else None,
                known_prefix_usd=session.get("lower_bound_cost", "0"),
                existing_charge_usd=session.get("cost", "0"),
                reservation_usd=str(pending(session)),
                proposed_estimate_usd=None,
                confidence="live-untouched" if identities[key]["live"] else "previously-settled",
                audit_inputs={},
            )
            rows.append(row)
            if identities[key]["live"] or (
                session["status"] == "settled" and session.get("cost_provenance") not in ESTIMATE_PROVENANCES
            ):
                continue
            event_count, record_count = len(projection.events), len(projection.projected_records)
            try:
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise BudgetError("invalid receipt invocation ID")
                if session.get("archive"):
                    raise BudgetError("torn-ledger reservation requires explicit reconciliation")
                if session.get("bridge"):
                    path = Path(session["bridge"]) / "inbox" / (key + ".receipt")
                    if path.exists():
                        receipt = safe_message(path)
                        if not isinstance(receipt, dict) or receipt.get("id") != key:
                            raise BudgetError("receipt invocation mismatch")
                        row["audit_inputs"]["receipt_sha256"] = hashlib.sha256(
                            json.dumps(receipt, sort_keys=True).encode()
                        ).hexdigest()
                        projection.settle_receipt(receipt, note=note)
                        current = projection.replay()["sessions"][key]
                        if current["status"] == "settled" and current["cost_provenance"] == "native-reported":
                            row.update(source="native-receipt", native_amount_usd=current["cost"], confidence="native")
                        else:
                            row["source"] = "native-prefix"
                state = projection.replay()
                current = state["sessions"][key]
                if current["status"] != "settled":
                    saved = evidence(budget.directory, current, state["sessions"], checkpoints=usage_evidence)
                    floor = max(
                        money(current.get("cost", "0")),
                        money(current.get("lower_bound_cost", "0")),
                        saved["cost"] if saved else Decimal(0),
                    )
                    duration = interval(session, now, saved)
                    if saved:
                        checkpoint = saved["checkpoint"]
                        usage_evidence[key] = checkpoint
                        row["source"] = checkpoint.get("recovered_from", "usage-journal")
                        row["audit_inputs"].update(
                            usage_sha256=hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
                            usage_sequence=checkpoint.get("sequence"),
                            closed=checkpoint["closed"],
                            inflight=checkpoint["inflight"],
                            complete=saved["complete"],
                            prices_sha256=checkpoint.get("prices_sha256"),
                            sources=checkpoint.get("sources", []),
                        )
                    row["known_prefix_usd"] = str(floor)
                    if saved and saved["complete"]:
                        estimate, provenance = floor, "token-priced"
                        audit = dict(policy=POLICY, tier="complete-token-usage", duration=duration, comparable_ids=[])
                    else:
                        estimate, provenance, audit = historical_estimate(session, state["sessions"], now, duration)
                    cost = max(floor, estimate)
                    row.update(proposed_estimate_usd=str(estimate), confidence=provenance)
                    row["audit_inputs"].update(audit, floor_usd=str(floor), settled_total_usd=str(cost))
                    # Establish a labelled cumulative baseline only from explicit provider identity.
                    primary = session.get("resume") or (saved["checkpoint"].get("provider_session") if saved else None)
                    if not primary and len(current.get("lower_bound_totals", {})) == 1:
                        primary = next(iter(current["lower_bound_totals"]))
                    totals = {}
                    if primary and primary != "unknown":
                        baseline = Decimal(0)
                        for prior in state["sessions"].values():
                            if prior["id"] == key:
                                break
                            baseline = max(baseline, money(prior.get("totals", {}).get(primary, "0")))
                        totals = {primary: str(baseline + cost)}
                    events = []
                    previous = money(current.get("cost", "0"))
                    if previous:
                        events.append(dict(type="correction", id=key, delta=str(previous), cost="0", note=note))
                    events.append(
                        dict(
                            type="settle",
                            id=key,
                            cost=str(cost),
                            totals=totals,
                            outcome="recovered stopped usage estimate",
                            cost_provenance=provenance,
                            finished=duration["finish"],
                            recovery_audit=row["audit_inputs"],
                            note=note,
                        )
                    )
                    projection.append(state, events)
                    if saved and saved["checkpoint"].get("recovered_from"):
                        checkpoints.append((session, saved["checkpoint"]))
                # A late native cumulative receipt can also correct prior/successor estimates.
                after_step = projection.replay()["sessions"]
                if any(after_step[k] != original[k] for k in original if identities[k]["live"]):
                    raise BudgetError("cumulative correction would change a live session; reconcile after it stops")
            except (ValueError, OSError, TypeError, KeyError) as error:
                del projection.events[event_count:]
                del projection.projected_records[record_count:]
                errors.append(f"{key}: {type(error).__name__}")
                row.update(confidence="unresolved-stopped", error=type(error).__name__)
        after = projection.replay()
        for row in rows:
            old, new = original[row["id"]], after["sessions"][row["id"]]
            row.update(
                final_charge_usd=new.get("cost", "0"),
                delta_usd=str(money(new.get("cost", "0")) - money(old.get("cost", "0"))),
                released_reservation_usd=str(pending(old) - pending(new)),
            )
        delta = after["spent"] - before["spent"]
        released = sum((money(row["released_reservation_usd"]) for row in rows), Decimal(0))
        if sum((money(row["delta_usd"]) for row in rows), Decimal(0)) != delta:
            raise BudgetError("recovery deltas do not reconcile")
        manifest = dict(
            policy=POLICY,
            generated_at=now,
            dry_run=dry_run,
            ledger_sequence=before["seq"],
            ledger_sha256=hashlib.sha256(budget.ledger.read_bytes() if budget.ledger.exists() else b"").hexdigest(),
            before_spent_usd=str(before["spent"]),
            after_spent_usd=str(after["spent"]),
            applied_delta_usd=str(delta),
            released_reservation_usd=str(released),
            before_reserved_usd=str(sum((pending(s) for s in original.values()), Decimal(0))),
            after_reserved_usd=str(sum((pending(s) for s in after["sessions"].values()), Decimal(0))),
            invocations=rows,
            errors=errors,
        )
        path = budget.directory / "recovery-audits" / (uuid.uuid4().hex + ".json")
        atomic_json(path, manifest)
        Budget(path.parent).sync_directory()
        budget.sync_directory()
        if not dry_run and projection.events:
            # Recheck identities immediately before committing the single append-only transaction.
            changed = {e.get("id") for e in projection.events if e.get("id")}
            if any(live_identity(original[k], budget.clock())["live"] for k in changed):
                raise BudgetError(f"session became live; rerun recovery (plan retained at {path})")
            from .usage_accounting import usage_path

            for session, checkpoint in checkpoints:
                atomic_json(usage_path(budget.directory, session["id"], session.get("bridge")), checkpoint)
            budget.append(
                before,
                [budget.accrue(before), *[dict(event, recovery_manifest=path.name) for event in projection.events]],
            )
        if not dry_run:
            changed = {e.get("id") for e in projection.events if e.get("id")}
            for row in rows:
                session = original[row["id"]]
                if (
                    row["id"] in changed
                    and session.get("bridge")
                    and not row["identity"]["live"]
                    and after["sessions"][row["id"]]["status"] == "settled"
                    and not live_identity(session, budget.clock())["live"]
                ):
                    (Path(session["bridge"]) / "key").unlink(missing_ok=True)
    return dict(
        budget.snapshot(),
        recovery_errors=errors,
        recovery_actions=[r for r in rows if r["confidence"] not in {"previously-settled", "live-untouched"}],
        recovery_manifest=str(path),
        recovery_totals={
            k: manifest[k]
            for k in ("before_spent_usd", "after_spent_usd", "applied_delta_usd", "released_reservation_usd")
        },
        dry_run=dry_run,
    )

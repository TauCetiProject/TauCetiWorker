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


def live_identity(session, now, directory=None):
    owner = accounting.bridge_owner_alive(session.get("owner"))
    heartbeat = None
    if session.get("bridge"):
        try:
            heartbeat = (Path(session["bridge"]) / "outbox/heartbeat").stat().st_mtime
        except FileNotFoundError:
            pass
    native = False
    if session.get("lifecycle"):
        from .usage_supervisor import invocation_alive

        native = invocation_alive(directory or accounting.budget_dir(), session)
    live = native or owner or heartbeat is not None and now - heartbeat <= 30
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
        # The seven-day window precedes the invocation, not the time of repair.
        end = money(session["started"])

        def candidates(*, same_phase=False):
            result = []
            for other in sessions.values():
                if (
                    other["id"] == session["id"]
                    or other["model"] != session["model"]
                    or same_phase
                    and other["phase"] != session["phase"]
                    or other["status"] != "settled"
                    or other.get("cost_provenance", "native-reported") != "native-reported"
                    or ("estimat" in other.get("outcome", "").lower() and not other.get("totals"))
                    or other.get("outcome") in {"reconciled", "launch-failed"}
                ):
                    continue
                # Reject out-of-window endpoints before validating and pricing
                # an interval. Most of a large fleet's history is irrelevant.
                finish = other.get("finished")
                try:
                    if isinstance(finish, bool) or finish is None or not end - WEEK <= money(finish) <= end:
                        continue
                except BudgetError:
                    continue
                span = interval(other, now)
                if span["seconds"] is None:
                    continue
                cost = money(other["cost"])
                if cost > 0:
                    result.append((other, cost / money(span["seconds"])))
            return result

        # Only compute other phases' rates when the preferred tier is too small.
        phased = candidates(same_phase=True)
        selected = phased if len(phased) >= 3 else candidates()
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
        self.state = super().replay(copy.deepcopy(records), _samples=False)
        self.events = []

    def locked(self):
        return contextlib.nullcontext()

    def replay(self, records=None, *, _state=None):
        if records is not None:
            return super().replay(records, _state=_state)
        # Callers only mutate top-level accrual fields; append uses copy-on-write
        # sessions so prior snapshots remain valid for candidate rollback.
        return dict(self.state)

    def current(self):
        return self.replay()

    def append(self, state, events):
        for event in events:
            if event["type"] == "settle" and event.get("cost_provenance") == "native-reported":
                event.setdefault("finished", state["sessions"][event["id"]].get("finished"))
        working = {**self.state, "sessions": dict(self.state["sessions"]), "samples": {}}
        for key in {e.get("id") for e in events if e.get("id")}:
            working["sessions"][key] = dict(working["sessions"][key])
        record = json.loads(self.record_bytes(self.state, events))
        # Budget.estimate queries sessions directly. Rebuilding the unused
        # sample history here would scan the entire fleet for every correction.
        self.state = super().replay([record], _state=working, _samples=False)
        self.events.extend(e for e in events if e["type"] != "accrue")


def pending(session):
    return Decimal(0) if session["status"] == "settled" else money(session.get("reserve", session["estimate"]))


def recover(budget, note, *, dry_run=False, plan=None):
    if not dry_run and not note.strip():
        raise BudgetError("receipt recovery requires --note")
    expected = None
    if plan:
        if dry_run:
            raise BudgetError("--plan requires an apply, not --dry-run")
        from .usage_accounting import _read

        expected = json.loads(_read(Path(plan), 128 * 1024**2))
        if not isinstance(expected, dict) or expected.get("policy") != POLICY or not expected.get("dry_run"):
            raise BudgetError("--plan requires a dry-run manifest for this policy")
    now = budget.clock()
    with budget.locked():
        raw = budget.ledger.read_bytes() if budget.ledger.exists() else b""
    # A preview is a snapshot, not a transaction. Parsing history and calculating
    # historical rates can take minutes on a large fleet; neither may block
    # admissions. Applying still replans under the lock and checks --plan.
    initial = budget.replay(budget.records(raw))
    # Large transcript/journal reads happen without the fleet accounting lock.
    usage_evidence = {}
    for key, session in initial["sessions"].items():
        if (
            len(key) == 32
            and all(c in "0123456789abcdef" for c in key)
            and session["status"] != "settled"
            and not live_identity(session, now, budget.directory)["live"]
        ):
            try:
                evidence(budget.directory, session, initial["sessions"], checkpoints=usage_evidence)
            except (ValueError, OSError, TypeError, KeyError):
                usage_evidence[key] = None
    with contextlib.nullcontext() if dry_run else budget.locked():
        # Applications use fresh state; previews retain the captured ledger.
        records = budget.records(raw) if dry_run else budget.records()
        before = budget.replay(copy.deepcopy(records))
        original = before["sessions"]
        identities = {key: live_identity(s, budget.clock(), budget.directory) for key, s in original.items()}
        live_sessions = {key: original[key] for key, identity in identities.items() if identity["live"]}
        projection = Projection(budget, records, now)
        rows, errors, checkpoints = [], [], []
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
            if session["status"] == "settled" and (
                not session.get("bridge") or not (Path(session["bridge"]) / "inbox" / (key + ".receipt")).exists()
            ):
                continue
            event_count, prior_state = len(projection.events), projection.state
            try:
                if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                    raise BudgetError("invalid receipt invocation ID")
                if session.get("archive"):
                    raise BudgetError("torn-ledger reservation requires explicit reconciliation")
                if session.get("bridge"):
                    path = Path(session["bridge"]) / "inbox" / (key + ".receipt")
                    if path.exists():
                        try:
                            receipt = safe_message(path)
                            if not isinstance(receipt, dict) or receipt.get("id") != key:
                                raise BudgetError("receipt invocation mismatch")
                            digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
                            row["audit_inputs"]["receipt_sha256"] = digest
                            if (
                                session["status"] == "settled"
                                and (session.get("recovery_audit") or {}).get("receipt_sha256") == digest
                            ):
                                continue
                            projection.settle_receipt(receipt, note=note)
                            current = projection.replay()["sessions"][key]
                            if current["status"] == "settled" and current["cost_provenance"] == "native-reported":
                                row.update(
                                    source="native-receipt", native_amount_usd=current["cost"], confidence="native"
                                )
                            else:
                                row["source"] = "native-prefix"
                        except (ValueError, OSError, TypeError, KeyError) as error:
                            if (
                                str(error)
                                == "cumulative correction would change a live session; reconcile after it stops"
                            ):
                                raise
                            del projection.events[event_count:]
                            projection.state = prior_state
                            row["source"] = "unusable-receipt"
                            row["audit_inputs"]["receipt_error"] = "unreadable-or-invalid-native-receipt"
                            try:
                                from .usage_accounting import _read

                                row["audit_inputs"]["receipt_sha256"] = hashlib.sha256(_read(path, 65536)).hexdigest()
                            except (ValueError, OSError):
                                row["audit_inputs"]["receipt_sha256"] = None
                state = projection.replay()
                current = state["sessions"][key]
                if current["status"] != "settled":
                    if key not in usage_evidence:
                        raise BudgetError("usage evidence changed; rerun recovery")
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
                            recovery_audit={
                                "policy": POLICY,
                                "tier": audit["tier"],
                                "duration": duration,
                                "rate_usd_per_second": audit.get("rate_usd_per_second"),
                                "comparable_count": len(audit["comparable_ids"]),
                                "inputs_sha256": hashlib.sha256(
                                    json.dumps(row["audit_inputs"], sort_keys=True).encode()
                                ).hexdigest(),
                                "receipt_sha256": row["audit_inputs"].get("receipt_sha256"),
                            },
                            note=note,
                        )
                    )
                    projection.append(state, events)
                    if saved and saved["checkpoint"].get("recovered_from"):
                        checkpoints.append((session, saved["checkpoint"]))
                # A late native cumulative receipt can also correct prior/successor estimates.
                after_step = projection.replay()["sessions"]
                if any(after_step[key] != session for key, session in live_sessions.items()):
                    raise BudgetError("cumulative correction would change a live session; reconcile after it stops")
            except (ValueError, OSError, TypeError, KeyError) as error:
                del projection.events[event_count:]
                projection.state = prior_state
                reasons = {
                    "invalid receipt invocation ID": "invalid-invocation-id",
                    "torn-ledger reservation requires explicit reconciliation": "torn-ledger-reconciliation-required",
                    "usage evidence changed; rerun recovery": "evidence-not-prefetched-rerun",
                    "cumulative correction would change a live session; reconcile after it stops": "live-cumulative-correction-deferred",
                }
                code = reasons.get(str(error), "invalid-evidence-or-policy-input")
                errors.append(f"{key}: {code}")
                row.update(confidence="unresolved-stopped", error=code)
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
            status="planned",
            generated_at=now,
            dry_run=dry_run,
            ledger_sequence=before["seq"],
            ledger_sha256=hashlib.sha256(
                raw if dry_run else budget.ledger.read_bytes() if budget.ledger.exists() else b""
            ).hexdigest(),
            before_spent_usd=str(before["spent"]),
            after_spent_usd=str(after["spent"]),
            applied_delta_usd=str(delta),
            released_reservation_usd=str(released),
            before_reserved_usd=str(sum((pending(s) for s in original.values()), Decimal(0))),
            after_reserved_usd=str(sum((pending(s) for s in after["sessions"].values()), Decimal(0))),
            invocations=rows,
            errors=errors,
        )
        if expected:

            def proposals(values):
                return [{k: v for k, v in row.items() if k != "identity"} for row in values]

            if (
                expected["ledger_sequence"] != manifest["ledger_sequence"]
                or expected["ledger_sha256"] != manifest["ledger_sha256"]
                or proposals(expected["invocations"]) != proposals(rows)
            ):
                raise BudgetError("dry-run plan no longer matches ledger/evidence; run dry-run again")
        path = budget.directory / "recovery-audits" / (uuid.uuid4().hex + ".json")
        atomic_json(path, manifest)
        Budget(path.parent).sync_directory()
        budget.sync_directory()
        application = dict(status="no-change", manifest=path.name, ledger_sequence=before["seq"])
        if not dry_run and projection.events:
            # Recheck identities immediately before committing the single append-only transaction.
            changed = {e.get("id") for e in projection.events if e.get("id")}
            if any(live_identity(original[k], budget.clock(), budget.directory)["live"] for k in changed):
                raise BudgetError(f"session became live; rerun recovery (plan retained at {path})")
            from .usage_accounting import usage_path

            for session, checkpoint in checkpoints:
                atomic_json(usage_path(budget.directory, session["id"], session.get("bridge")), checkpoint)
            committed = budget.append(
                before,
                [budget.accrue(before), *[dict(event, recovery_manifest=path.name) for event in projection.events]],
            )
            application.update(status="committed", **committed)
        if not dry_run:
            atomic_json(path.with_suffix(".application.json"), application)
            Budget(path.parent).sync_directory()
            changed = {e.get("id") for e in projection.events if e.get("id")}
            for row in rows:
                session = original[row["id"]]
                if (
                    row["id"] in changed
                    and session.get("bridge")
                    and not row["identity"]["live"]
                    and after["sessions"][row["id"]]["status"] == "settled"
                    and not live_identity(session, budget.clock(), budget.directory)["live"]
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


def recover_stopped(budget, invocation):
    """One stopped provider group. Slow evidence/rate work never owns the fleet lock.

    Normal receipt/prefix recovery runs first. This applies the existing explicit
    stopped policy to the remaining uncertainty, with the same correctable totals.
    """
    from .usage_accounting import usage_path

    now = budget.clock()
    with budget.locked():
        state = budget.current()
        linked = budget.evidence_sessions(state, invocation)
        target = linked.get(invocation)
        if not target or target["status"] == "settled" or target.get("archive"):
            return False
        linked = copy.deepcopy(linked)
        filters = dict(
            model=target["model"],
            settled=True,
            positive=True,
            native=True,
            finished_after=float(target["started"]) - WEEK,
            finished_before=target["started"],
        )
        comparables = budget.sessions(state, phase=target["phase"], **filters)
        # Validate eligibility with the real policy outside the lock; the model
        # fallback is fetched below if these candidates do not form a rate.
    if live_identity(target, now, budget.directory)["live"]:
        return False

    def stamps(sessions):
        values = {}
        for s in sessions.values():
            paths = [usage_path(budget.directory, s["id"], s.get("bridge")), usage_path(budget.directory, s["id"])]
            paths += [p.with_suffix(".jsonl") for p in paths]
            if s.get("bridge"):
                paths.append(Path(s["bridge"]) / "inbox" / (s["id"] + ".receipt"))
            for p in paths:
                try:
                    info = p.lstat()
                    values[str(p)] = (
                        info.st_dev,
                        info.st_ino,
                        info.st_mtime_ns,
                        info.st_size,
                        info.st_uid,
                        info.st_mode,
                    )
                except FileNotFoundError:
                    values[str(p)] = None
        return values

    stamp = stamps(linked)
    saved = evidence(budget.directory, target, linked)
    providers = set(target.get("totals", {})) | set(target.get("lower_bound_totals", {}))
    if target.get("resume"):
        providers.add(target["resume"])
    if saved and saved["checkpoint"].get("provider_session"):
        providers.add(saved["checkpoint"]["provider_session"])
    with budget.locked():
        fresh = budget.current()
        related = {s["id"]: copy.deepcopy(s) for s in budget.related(fresh, providers, include=invocation)}
    if any(related.get(k) != s for k, s in linked.items()) or stamps(linked) != stamp:
        return False
    linked = related
    if any(live_identity(s, now, budget.directory)["live"] for s in linked.values()):
        return False
    stamp = stamps(linked)
    saved = evidence(budget.directory, target, linked)
    if stamps(linked) != stamp:
        return False
    # Transcript recovery records the actual source metadata; validate it again
    # at commit, as the synthetic checkpoint does not yet exist on disk.
    sources = []
    if saved:
        for source in saved["checkpoint"].get("sources", []):
            p = Path(source["file"])
            info = p.stat()
            if info.st_size != source["size"] or info.st_mtime_ns != source["mtime_ns"]:
                return False
            sources.append((p, info.st_size, info.st_mtime_ns))
    duration = interval(target, now, saved)
    floor = max(
        money(target.get("cost", "0")),
        money(target.get("lower_bound_cost", "0")),
        saved["cost"] if saved else Decimal(0),
    )
    if saved and saved["complete"]:
        estimate, provenance = floor, "token-priced"
        audit = dict(policy=POLICY, tier="complete-token-usage", duration=duration, comparable_ids=[])
    else:
        estimate, provenance, audit = historical_estimate(target, {s["id"]: s for s in comparables}, now, duration)
        if audit["tier"] == "prelaunch" and duration["seconds"] is not None:
            with budget.locked():
                comparables = budget.sessions(budget.current(), **filters)
            estimate, provenance, audit = historical_estimate(target, {s["id"]: s for s in comparables}, now, duration)
    cost = max(floor, estimate)
    primary = target.get("resume") or (saved["checkpoint"].get("provider_session") if saved else None)
    if not primary and len(target.get("lower_bound_totals", {})) == 1:
        primary = next(iter(target["lower_bound_totals"]))
    baseline = Decimal(0)
    if primary:
        for prior in linked.values():
            if prior["id"] == invocation:
                break
            baseline = max(baseline, money(prior.get("totals", {}).get(primary, "0")))
    totals = {primary: str(baseline + cost)} if primary and primary != "unknown" else {}
    audit.update(
        known_prefix_usd=str(floor),
        proposed_estimate_usd=str(estimate),
        final_charge_usd=str(cost),
        existing_charge_usd=target.get("cost", "0"),
        reservation_usd=str(pending(target)),
        evidence_stamps=stamp,
        usage_sha256=hashlib.sha256(json.dumps(saved["checkpoint"], sort_keys=True).encode()).hexdigest()
        if saved
        else None,
    )
    path = budget.directory / "recovery-audits" / (uuid.uuid4().hex + ".json")
    atomic_json(
        path,
        dict(
            policy=POLICY,
            status="planned",
            generated_at=now,
            automatic=True,
            invocations=[
                dict(
                    id=invocation,
                    worker=target["worker"],
                    model=target["model"],
                    phase=target["phase"],
                    confidence=provenance,
                    audit_inputs=audit,
                )
            ],
        ),
    )
    Budget(path.parent).sync_directory()
    budget.sync_directory()
    previous = money(target.get("cost", "0"))
    note = "usage supervisor: stopped usage policy v1"
    events = [dict(type="correction", id=invocation, delta=str(previous), cost="0", note=note)] if previous else []
    events.append(
        dict(
            type="settle",
            id=invocation,
            cost=str(cost),
            totals=totals,
            outcome="recovered stopped usage estimate",
            cost_provenance=provenance,
            finished=duration["finish"],
            recovery_manifest=path.name,
            note=note,
            recovery_audit=dict(
                policy=POLICY,
                tier=audit["tier"],
                duration=duration,
                rate_usd_per_second=audit.get("rate_usd_per_second"),
                comparable_count=len(audit["comparable_ids"]),
                inputs_sha256=hashlib.sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest(),
            ),
        )
    )
    with budget.locked():
        current = budget.current()
        group = {s["id"]: s for s in budget.related(current, providers, include=invocation)}
        if group != linked or stamps(group) != stamp:
            return False
        if any(live_identity(s, budget.clock(), budget.directory)["live"] for s in group.values()):
            return False  # Includes a live resumed successor.
        if any(current["sessions"].get(s["id"]) != s for s in comparables):
            return False
        if any(p.stat().st_size != size or p.stat().st_mtime_ns != mtime for p, size, mtime in sources):
            return False
        if saved and saved["checkpoint"].get("recovered_from"):
            atomic_json(usage_path(budget.directory, invocation, target.get("bridge")), saved["checkpoint"])
        committed = budget.append(current, [budget.accrue(current), *events])
    atomic_json(
        path.with_suffix(".application.json"),
        dict(
            status="committed",
            manifest=path.name,
            applied_delta_usd=str(cost - previous),
            released_reservation_usd=str(pending(target)),
            **committed,
        ),
    )
    Budget(path.parent).sync_directory()
    return True

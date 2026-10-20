"""Shared local grant accounting. The append-only ledger is the authority."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

ESTIMATE_PROVENANCES = {"token-priced", "historical-estimate", "prelaunch-estimate"}


class BudgetError(ValueError):
    pass


def admission_pause(snapshot):
    """Preflight funding and ledger integrity; missing receipts are audit work."""
    if snapshot.get("ledger_repair_required"):
        return "ledger repair requires reconciliation"
    if money(snapshot["available"]) <= 0 or money(snapshot["available"]) < money(snapshot["admission_threshold"]):
        return "waiting for funding"
    return None


def process_identity():
    """Host bridge identity, including PID reuse and reboot protection on Linux."""
    if sys.platform != "linux":
        return None
    try:
        pid = os.getpid()
        return dict(
            pid=pid,
            boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            start=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
        )
    except (OSError, IndexError):
        return None


def bridge_owner_alive(owner):
    if not owner or sys.platform != "linux":
        return False
    try:
        fields = Path(f"/proc/{int(owner['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
        return (
            owner["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            and owner["start"] == fields[19]
            and fields[0] not in {"Z", "X"}
        )
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        return False


def money(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise BudgetError("amount must be a finite decimal") from None
    if not number.is_finite():
        raise BudgetError("amount must be a finite decimal")
    return number


def budget_dir() -> Path:
    if override := os.environ.get("TAUCETI_BUDGET_DIR"):
        return Path(override).expanduser().resolve()
    if xdg := os.environ.get("XDG_STATE_HOME"):
        return Path(xdg).expanduser().resolve() / "tauceti" / "budget"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/tauceti/state/budget"
    return Path.home() / ".local/state/tauceti/budget"


class Budget:
    def __init__(self, directory: Path | None = None, *, clock=time.time):
        self.directory = directory or budget_dir()
        self.ledger = self.directory / "events.jsonl"
        self.clock = clock

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / "budget.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def records(self, data: bytes | None = None) -> list[dict]:
        if data is None:
            try:
                data = self.ledger.read_bytes()
            except FileNotFoundError:
                return []
        if data and not data.endswith(b"\n"):
            raise BudgetError(f"torn ledger write: {self.ledger}; preserve it and use budget --repair-ledger")
        records = []
        for i, line in enumerate(data.splitlines(), 1):
            try:
                rec = json.loads(line)
                if rec["version"] != 1 or rec["seq"] != i or not isinstance(rec["events"], list):
                    raise ValueError("invalid sequence or schema")
                records.append(rec)
            except (ValueError, KeyError, TypeError):
                raise BudgetError(f"invalid ledger record {i}: {self.ledger}") from None
        return records

    def replay(self, records: list[dict] | None = None):
        state = dict(
            balance=Decimal(0),
            rate=Decimal(0),
            spent=Decimal(0),
            watermark=Decimal(0),
            sessions={},
            waiting={},
            cooldowns={},
            samples={},
            seq=0,
        )
        try:
            for rec in self.records() if records is None else records:
                state["seq"] = rec["seq"]
                for event in rec["events"]:
                    kind = event["type"]
                    if kind == "accrue":
                        state["balance"] += money(event["amount"])
                        state["watermark"] = money(event["watermark"])
                    elif kind in {"grant", "set", "correction"}:
                        state["balance"] += money(event["delta"])
                        if kind == "correction":
                            state["spent"] -= money(event["delta"])
                            state["sessions"][event["id"]]["cost"] = event["cost"]
                            for field in ("cost_provenance", "totals", "recovery_audit"):
                                if field in event:
                                    state["sessions"][event["id"]][field] = event[field]
                    elif kind == "rate":
                        state["rate"] = money(event["amount"])
                    elif kind == "wait":
                        state["waiting"][event["worker"]] = event
                    elif kind == "admit":
                        state["sessions"][event["id"]] = event
                        state["waiting"].pop(event["worker"], None)
                    elif kind == "settle":
                        session = state["sessions"][event["id"]]
                        session.update(
                            status="settled",
                            cost=event["cost"],
                            outcome=event["outcome"],
                            totals=event.get("totals", {}),
                            cost_provenance=event.get("cost_provenance", "native-reported"),
                            finished=event.get("finished", rec["timestamp"]),
                            recovery_audit=event.get("recovery_audit"),
                        )
                        cost = money(event["cost"])
                        state["balance"] -= cost
                        state["spent"] += cost
                        key = session["model"] + ":" + session["phase"]
                        state["samples"].setdefault(key, []).append(cost)
                    elif kind == "unresolved":
                        session = state["sessions"][event["id"]]
                        session["status"] = "provisional" if event.get("provisional") else "unresolved"
                        session.setdefault("finished", rec["timestamp"])
                        if "reserve" in event:
                            session.update(
                                reserve=event["reserve"],
                                lower_bound_cost=event["lower_bound_cost"],
                                lower_bound_totals=event["lower_bound_totals"],
                                outcome=event["outcome"],
                            )
                            for field in ("usage_checkpoint", "usage_tail"):
                                if field in event:
                                    session[field] = event[field]
                    elif kind == "cooldown":
                        state["cooldowns"][event["model"]] = event["until"]
                    else:
                        raise BudgetError(f"unknown ledger event {kind}")
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise BudgetError(f"invalid accounting event in {self.ledger}: {exc}") from None
        state["samples"] = {}
        for session in state["sessions"].values():
            if session["status"] == "settled":
                key = session["model"] + ":" + session["phase"]
                state["samples"].setdefault(key, []).append(money(session["cost"]))
        return state

    def accrue(self, state):
        now = max(state["watermark"], money(self.clock()))
        amount = (now - state["watermark"]) * state["rate"] / Decimal(3600)
        state["balance"] += amount
        state["watermark"] = now
        return {"type": "accrue", "amount": str(amount), "watermark": str(now)}

    def record_bytes(self, state, events):
        record = dict(
            version=1,
            seq=state["seq"] + 1,
            transaction=uuid.uuid4().hex,
            timestamp=self.clock(),
            actor=f"uid:{os.getuid()}",
            events=events,
        )
        return (json.dumps(record, separators=(",", ":")) + "\n").encode()

    def sync_directory(self):
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def append(self, state, events):
        data = self.record_bytes(state, events)
        fd = os.open(self.ledger, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("ledger write made no progress")
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        # Persist a newly created ledger's directory entry as well as its contents.
        self.sync_directory()

    def configure(self, *, grant=None, set_grant=None, rate=None):
        if grant is not None and set_grant is not None:
            raise BudgetError("--grant and --set-grant are mutually exclusive")
        grant = money(grant) if grant is not None else None
        target = money(set_grant) if set_grant is not None else None
        rate = money(rate) if rate is not None else None
        if grant is not None and grant <= 0:
            raise BudgetError("--grant must be positive")
        if rate is not None and rate < 0:
            raise BudgetError("--grant-rate must be nonnegative")
        with self.locked():
            state = self.replay()
            events = [self.accrue(state)]
            if grant is not None:
                events.append(dict(type="grant", delta=str(grant)))
            if target is not None:
                events.append(
                    dict(
                        type="set",
                        previous=str(state["balance"]),
                        target=str(target),
                        delta=str(target - state["balance"]),
                    )
                )
            if rate is not None:
                events.append(dict(type="rate", amount=str(rate), previous=str(state["rate"])))
            self.append(state, events)
        return self.snapshot()

    def waiting_state(self, state):
        try:
            queue = json.loads((self.directory / "waiting.json").read_text())
            if not isinstance(queue, dict) or any(not isinstance(v, dict) for v in queue.values()):
                raise ValueError("invalid waiting state")
            return queue
        except (FileNotFoundError, ValueError):
            return state["waiting"]

    def save_waiting(self, queue):
        # Queue liveness is disposable; financial decisions remain in the fsynced audit ledger.
        temporary = self.directory / ("waiting-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(queue, file)
        os.replace(temporary, self.directory / "waiting.json")

    def refresh_waiter(self, worker):
        with self.locked():
            state = self.replay()
            queue = self.waiting_state(state)
            if worker in queue and self.clock() - queue[worker].get("seen", 0) < 120:
                queue[worker]["seen"] = self.clock()
                self.save_waiting(queue)

    def wait_for_funding(self, worker, model, phase):
        with self.locked():
            state = self.replay()
            queue = self.waiting_state(state)
            previous = queue.get(worker, {})
            now = self.clock()
            if now - previous.get("seen", 0) >= 120:
                previous = {}
            estimate = self.estimate(state, model, phase)[1]
            queue[worker] = dict(
                type="wait",
                worker=worker,
                model=model,
                ticket=previous.get("ticket", uuid.uuid4().hex),
                since=previous.get("since", now),
                seen=now,
                reason="waiting for funding",
                estimate=str(estimate),
            )
            self.save_waiting(queue)

    def estimate(self, state, model, phase=None, *, include_pending=True):
        costs = [c for c in state["samples"].get(model + ":" + phase, []) if c > 0] if phase else []
        costs = costs or [
            money(s["cost"])
            for s in state["sessions"].values()
            if s["model"] == model and s["status"] == "settled" and money(s["cost"]) > 0
        ]
        if not costs and include_pending:
            costs = [
                money(s["reserve"])
                for s in state["sessions"].values()
                if s["model"] == model and s["status"] != "settled" and money(s.get("reserve", "0")) > 0
            ]
        costs = sorted(costs[-20:])
        if costs:
            return costs, costs[math.ceil(len(costs) * 0.75) - 1]
        recent = [money(s["cost"]) for s in state["sessions"].values() if s["status"] == "settled"][-20:]
        return costs, max([Decimal(1), *recent])

    def snapshot(self, *, worker=None, model=None, phase=None):
        with self.locked():
            state = self.replay()
            self.accrue(state)
            state["waiting"] = self.waiting_state(state)
        active = [s for s in state["sessions"].values() if s["status"] != "settled"]
        pending = sum((money(s.get("reserve", s["estimate"])) for s in active), Decimal(0))
        now = self.clock()
        available = state["balance"] - pending
        waiting = [item for item in state["waiting"].values() if now - item["seen"] < 120]
        for item in waiting:
            if item["reason"] == "waiting for funding" and state["rate"] > 0:
                needed = max(money(item["estimate"]), Decimal("0.000000001")) - available
                item["funding_at"] = now + float(max(0, needed) * 3600 / state["rate"])
        zero_at = None
        if state["balance"] < 0 and state["rate"] > 0:
            zero_at = now + float(-state["balance"] * 3600 / state["rate"])
        threshold = Decimal(0)
        if worker is not None:
            previous = next((v for v in waiting if v["worker"] == worker), None)
            if previous:
                threshold = money(previous["estimate"])
            elif model:
                threshold = self.estimate(state, model, phase)[1]
        funding_at = None
        if worker is not None and state["rate"] > 0 and available < max(threshold, Decimal("0.000000001")):
            funding_at = now + float((max(threshold, Decimal("0.000000001")) - available) * 3600 / state["rate"])
        return dict(
            balance=str(state["balance"]),
            grant_rate=str(state["rate"]),
            spent=str(state["spent"]),
            pending=str(pending),
            available=str(available),
            active_sessions=active,
            unresolved=[s["id"] for s in active if s["status"] == "unresolved"],
            ledger_repair_required=any(s.get("archive") for s in active),
            provisional=[s["id"] for s in active if s["status"] == "provisional"],
            provisional_lower_bound_usd=str(
                sum((money(s["lower_bound_cost"]) for s in active if s["status"] == "provisional"), Decimal(0))
            ),
            waiting=waiting,
            balance_zero_at=zero_at,
            admission_threshold=str(threshold),
            funding_at=funding_at,
            clock_backwards=money(now) < state["watermark"],
            ledger=str(self.ledger),
            cooldowns=state["cooldowns"],
        )

    def admit(self, invocation, worker, model, phase, *, bridge=None, resume=None, owner=None):
        with self.locked():
            state = self.replay()
            if invocation in state["sessions"]:
                raise BudgetError("an invocation ID cannot be launched twice")
            events = [self.accrue(state)]
            active = [s for s in state["sessions"].values() if s["status"] != "settled"]
            if resume and not any(resume in s.get("totals", {}) for s in state["sessions"].values()):
                raise BudgetError("resuming requires a recorded cost baseline for that provider session ID")
            for session in active:
                if session["status"] == "active" and session.get("bridge"):
                    heartbeat = Path(session["bridge"]) / "outbox/heartbeat"
                    try:
                        orphaned = self.clock() - heartbeat.stat().st_mtime > 30
                    except FileNotFoundError:
                        orphaned = True
                    if orphaned and not bridge_owner_alive(session.get("owner")):
                        session["status"] = "unresolved"
                        events.append(dict(type="unresolved", id=session["id"], outcome="bridge disappeared"))
            costs, estimate = self.estimate(state, model, phase)
            live = [s for s in active if s["status"] == "active"]
            available = state["balance"] - sum((money(s.get("reserve", s["estimate"])) for s in active), Decimal(0))
            now = self.clock()
            queue = self.waiting_state(state)
            previous = queue.get(worker, {})
            if now - previous.get("seen", 0) >= 120:
                previous = {}
            ticket = previous.get("ticket", uuid.uuid4().hex)
            first = min(
                (
                    v
                    for v in queue.values()
                    if now - v["seen"] < 120
                    and v["reason"] in {"waiting for funding", "waiting behind another worker"}
                    and v.get("model") == model
                ),
                key=lambda v: (v["since"], v["ticket"]),
                default=None,
            )
            reason = None
            if any(s.get("archive") for s in active):
                reason = "ledger repair requires reconciliation"
            elif resume and any(s.get("resume") == resume for s in active):
                reason = (
                    "provider session already being resumed"
                    if any(s.get("resume") == resume for s in live)
                    else "provider session cost baseline unresolved"
                )
            elif state["cooldowns"].get(model, 0) > now:
                reason = "API throughput cooldown"
            elif not costs and any(s["model"] == model for s in live):
                reason = "waiting for calibration session cost"
            elif available <= 0 or available < estimate:
                reason = "waiting for funding"
            elif first and first["worker"] != worker:
                reason = "waiting behind another worker"
            if reason:
                wait = dict(
                    type="wait",
                    worker=worker,
                    model=model,
                    ticket=ticket,
                    seen=now,
                    since=previous.get("since", now),
                    reason=reason,
                    estimate=str(estimate),
                )
                queue[worker] = wait
                self.save_waiting(queue)
                if previous.get("reason") != reason or len(events) > 1:
                    events.append(wait)
                    self.append(state, events)
                return dict(allowed=False, reason=reason, estimate=str(estimate), balance=str(state["balance"]))
            events.append(
                dict(
                    type="admit",
                    id=invocation,
                    worker=worker,
                    model=model,
                    phase=phase,
                    estimate=str(estimate),
                    status="active",
                    started=now,
                    bridge=bridge,
                    resume=resume,
                    owner=owner,
                )
            )
            self.append(state, events)
            queue.pop(worker, None)
            self.save_waiting(queue)
            return dict(allowed=True, estimate=str(estimate))

    def settle(self, invocation, cost, outcome="completed", *, note=None, totals=None, provenance="native-reported"):
        value = money(cost) if cost is not None else None
        if value is not None and value < 0:
            raise BudgetError("session cost cannot be negative")
        with self.locked():
            state = self.replay()
            if invocation not in state["sessions"]:
                raise BudgetError("unknown invocation")
            session = state["sessions"][invocation]
            if session["status"] == "settled":
                if value is not None and money(session["cost"]) != value:
                    raise BudgetError("conflicting cost receipt; reconcile explicitly")
                return
            events = [self.accrue(state)]
            if value is None:
                events.append(dict(type="unresolved", id=invocation, outcome=outcome))
            else:
                if "cost" in session:
                    # Prefixes use ordinary correction events, so older ledger
                    # readers also see the right balance and total spend.
                    events.append(
                        dict(
                            type="correction",
                            id=invocation,
                            delta=session["cost"],
                            cost="0",
                            note="replace estimated prefix",
                        )
                    )
                events.append(
                    dict(
                        type="settle",
                        id=invocation,
                        cost=str(value),
                        outcome=outcome,
                        note=note,
                        totals=totals or {},
                        cost_provenance=provenance,
                    )
                )
            self.append(state, events)

    def reconcile(self, invocation, cost, note):
        if not note.strip():
            raise BudgetError("reconciliation requires --note")
        value = money(cost)
        if value < 0:
            raise BudgetError("cost cannot be negative")
        with self.locked():
            state = self.replay()
            session = state["sessions"].get(invocation)
            if not session:
                raise BudgetError("unknown invocation")
            if session["status"] != "settled" and session.get("bridge"):
                try:
                    live = self.clock() - (Path(session["bridge"]) / "outbox/heartbeat").stat().st_mtime <= 30
                except FileNotFoundError:
                    live = False
                if live or bridge_owner_alive(session.get("owner")):
                    raise BudgetError("cannot reconcile an active session with a live accounting bridge")
            if session["status"] != "settled":
                events = [self.accrue(state)]
                if "cost" in session:
                    events.append(
                        dict(
                            type="correction",
                            id=invocation,
                            delta=session["cost"],
                            cost="0",
                            note="replace estimated prefix through reconciliation",
                        )
                    )
                events.append(dict(type="settle", id=invocation, cost=str(value), outcome="reconciled", note=note))
            else:
                previous = money(session["cost"])
                # Corrections also update the session through replay, without erasing the first receipt.
                events = [
                    self.accrue(state),
                    dict(
                        type="correction",
                        id=invocation,
                        delta=str(previous - value),
                        cost=str(value),
                        previous=str(previous),
                        note=note,
                    ),
                ]
            self.append(state, events)

    def settle_receipt(self, receipt, *, note=None):
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("id"), str)
            or not isinstance(receipt.get("outcome"), str)
        ):
            raise BudgetError("invalid receipt identity or outcome")
        if "totals" not in receipt:
            raise BudgetError("receipt is missing cost totals")
        totals = receipt["totals"]
        provisional = receipt.get("provisional") or "process has not exited" in receipt["outcome"]
        lower = receipt.get("lower_bound_totals", totals if provisional else None)
        if lower:
            if not isinstance(lower, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or money(v) < 0 for k, v in lower.items()
            ):
                raise BudgetError("invalid provisional cost evidence")
        if provisional:
            # A native result is lower-bound evidence until process termination:
            # a further request may have begun before its next stream event.
            totals = None
        if totals is not None:
            if (
                not isinstance(totals, dict)
                or not totals
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in totals.items())
            ):
                raise BudgetError("receipt totals must map provider IDs to decimal strings")
            if any(money(v) < 0 for v in totals.values()):
                raise BudgetError("receipt totals cannot be negative")
        cost = None
        with self.locked():
            state = self.replay()
            session = state["sessions"].get(receipt["id"])
            if not session:
                raise BudgetError("receipt has no recorded admission")
            if session["status"] == "settled" and session.get("cost_provenance") not in ESTIMATE_PROVENANCES:
                return
            ordered = list(state["sessions"].values())
            position = next(i for i, entry in enumerate(ordered) if entry["id"] == receipt["id"])
            baseline = {}
            for prior in state["sessions"].values():
                if prior["id"] == receipt["id"]:
                    break
                for provider_id, value in prior.get("totals", {}).items():
                    baseline[provider_id] = max(baseline.get(provider_id, Decimal(0)), money(value))
            if session["status"] == "settled" and totals is None:
                return
            if totals is None and lower:
                differences = [money(v) - baseline.get(k, Decimal(0)) for k, v in lower.items()]
                if all(v >= 0 for v in differences):
                    known = sum(differences, Decimal(0))
                    # Admission uses estimates, not a per-invocation hard cap.
                    # Keep the known lower bound plus a full additional estimate
                    # reserved; uncertainty is visible without freezing peers.
                    tail = max(
                        money(session["estimate"]),
                        self.estimate(state, session["model"], session["phase"], include_pending=False)[1],
                        known,
                    )
                    charged = money(session.get("cost", "0"))
                    if session.get("cost_provenance") == "token-priced-prefix" and "usage_tail" in session:
                        tail = money(session["usage_tail"])
                    reserve = max(money(session.get("reserve", "0")), max(Decimal(0), known - charged) + tail)
                    if (
                        session["status"] == ("provisional" if provisional else "unresolved")
                        and session.get("lower_bound_totals") == lower
                        and money(session["reserve"]) == reserve
                    ):
                        return
                    event = dict(
                        type="unresolved",
                        id=receipt["id"],
                        provisional=bool(provisional),
                        reserve=str(reserve),
                        lower_bound_cost=str(known),
                        lower_bound_totals=lower,
                        outcome=receipt["outcome"],
                        note=note,
                    )
                    self.append(state, [self.accrue(state), event])
                    return
            if totals is not None:
                events = [self.accrue(state)]
                # An estimate may exceed a later native cumulative total. Clamp
                # only the estimated segment, never an authoritative receipt.
                for provider, native in totals.items():
                    if money(native) >= baseline.get(provider, Decimal(0)):
                        continue
                    priors = [p for p in ordered[:position] if provider in p.get("totals", {})]
                    exact = max(
                        (
                            money(p["totals"][provider])
                            for p in priors
                            if p.get("cost_provenance", "native-reported") not in ESTIMATE_PROVENANCES
                        ),
                        default=Decimal(0),
                    )
                    estimates = [
                        p
                        for p in priors
                        if p.get("cost_provenance") in ESTIMATE_PROVENANCES and money(p["totals"][provider]) > exact
                    ]
                    if money(native) < exact or not estimates or any(set(p["totals"]) != {provider} for p in estimates):
                        break
                    weight = sum((money(p["cost"]) for p in estimates), Decimal(0))
                    cursor = exact
                    for index, prior in enumerate(estimates):
                        value = (
                            (money(native) - cursor)
                            if index == len(estimates) - 1
                            else (money(native) - exact) * money(prior["cost"]) / weight
                            if weight
                            else Decimal(0)
                        )
                        cursor += value
                        events.append(
                            dict(
                                type="correction",
                                id=prior["id"],
                                delta=str(money(prior["cost"]) - value),
                                cost=str(value),
                                totals={provider: str(cursor)},
                                cost_provenance=prior["cost_provenance"],
                                note="estimate constrained by native cumulative total",
                            )
                        )
                    baseline[provider] = cursor
                differences = [money(v) - baseline.get(k, Decimal(0)) for k, v in totals.items()]
                if all(v >= 0 for v in differences):
                    cost = sum(differences, Decimal(0))
                    previous = money(session.get("cost", "0"))
                    if session["status"] == "settled":
                        events.append(
                            dict(
                                type="correction",
                                id=receipt["id"],
                                delta=str(previous - cost),
                                cost=str(cost),
                                totals=totals,
                                cost_provenance="native-reported",
                                note=note or "native receipt replaces estimate",
                            )
                        )
                        # Correcting an earlier estimated resume baseline must
                        # preserve the next native cumulative total as well.
                        later = [
                            p
                            for p in ordered[position + 1 :]
                            if p["status"] == "settled" and set(p.get("totals", {})) & set(totals)
                        ]
                        for successor in later:
                            if set(totals) != set(successor.get("totals", {})):
                                raise BudgetError("late receipt has mixed cumulative baselines; reconcile explicitly")
                            delta = cost - previous
                            if successor.get("cost_provenance") in ESTIMATE_PROVENANCES:
                                rebased = {key: str(money(value) + delta) for key, value in successor["totals"].items()}
                                events.append(
                                    dict(
                                        type="correction",
                                        id=successor["id"],
                                        delta="0",
                                        cost=successor["cost"],
                                        totals=rebased,
                                        note="rebase estimated cumulative totals after native correction",
                                    )
                                )
                                continue
                            adjusted = money(successor["cost"]) - delta
                            if adjusted < 0:
                                raise BudgetError(
                                    "late receipt conflicts with later cumulative baseline; reconcile explicitly"
                                )
                            events.append(
                                dict(
                                    type="correction",
                                    id=successor["id"],
                                    delta=str(delta),
                                    cost=str(adjusted),
                                    note="preserve native cumulative total after baseline correction",
                                )
                            )
                            break
                    else:
                        if previous:
                            events.append(
                                dict(
                                    type="correction",
                                    id=receipt["id"],
                                    delta=str(previous),
                                    cost="0",
                                    note="replace token-priced prefix",
                                )
                            )
                        events.append(
                            dict(
                                type="settle",
                                id=receipt["id"],
                                cost=str(cost),
                                totals=totals,
                                outcome=receipt["outcome"],
                                cost_provenance="native-reported",
                                note=note,
                            )
                        )
                    self.append(state, events)
                    return
        if session["status"] != "settled":
            self.settle(receipt["id"], None, receipt["outcome"], note=note)

    def recover_usage(self, invocation, *, note="saved usage checkpoint", allow_live_owner=False, dry_run=False):
        """Settle a stopped complete journal, or charge its prefix and reserve uncertainty.

        No reservations are treated as costs. Repeated recovery is idempotent.
        A late native receipt reverses the estimated prefix before settlement.
        """
        from .budget_usage import evidence

        with self.locked():
            sessions = self.replay()["sessions"]
        session = sessions.get(invocation)
        if (
            not session
            or session["status"] == "settled"
            or (not allow_live_owner and bridge_owner_alive(session.get("owner")))
        ):
            return None
        saved = evidence(self.directory, session, sessions)
        if not saved or not saved["items"]:
            return None
        with self.locked():
            state = self.replay()
            session = state["sessions"].get(invocation)
            if (
                not session
                or session["status"] == "settled"
                or (not allow_live_owner and bridge_owner_alive(session.get("owner")))
            ):
                return None
            cost = max(saved["cost"], money(session.get("lower_bound_cost", "0")))
            stamp = (
                str(saved["checkpoint"].get("sequence", 0))
                + ":"
                + str(saved["checkpoint"].get("observed_at"))
                + ":"
                + str(cost)
            )
            tail = (
                money(session["usage_tail"])
                if session.get("usage_checkpoint") == stamp and "usage_tail" in session
                else Decimal(0)
                if saved["complete"]
                else max(
                    money(session["estimate"]),
                    self.estimate(state, session["model"], session["phase"], include_pending=False)[1],
                    cost,
                )
            )
            result = dict(
                id=invocation,
                action="settle token-priced estimate"
                if saved["complete"]
                else "charge token-priced prefix; reserve incomplete request",
                estimated_usd=str(cost),
                reserve_usd=str(tail),
            )
            result["source"] = saved["checkpoint"].get("recovered_from", "usage-journal")
            if dry_run:
                return result
            if saved["checkpoint"].get("recovered_from"):
                from .runtime_status import atomic_json
                from .usage_accounting import usage_path

                atomic_json(usage_path(self.directory, invocation, session.get("bridge")), saved["checkpoint"])
            previous = money(session.get("cost", "0"))
            if saved["complete"]:
                events = (
                    [dict(type="correction", id=invocation, delta=str(previous), cost="0", note=note)]
                    if previous
                    else []
                )
                # Estimated totals establish a resume baseline. They are labelled
                # so a subsequent native cumulative receipt can correct them.
                primary = session.get("resume") or saved["checkpoint"].get("provider_session")
                totals = {}
                if primary and primary != "unknown":
                    baseline = Decimal(0)
                    for prior in state["sessions"].values():
                        if prior["id"] == invocation:
                            break
                        baseline = max(baseline, money(prior.get("totals", {}).get(primary, "0")))
                    totals = {primary: str(baseline + cost)}
                events.append(
                    dict(
                        type="settle",
                        id=invocation,
                        cost=str(cost),
                        outcome="recovered usage estimate",
                        cost_provenance="token-priced",
                        totals=totals,
                        note=note,
                    )
                )
            else:
                if previous == cost and session.get("reserve") == str(tail) and session["status"] == "provisional":
                    return result
                events = [
                    dict(
                        type="correction",
                        id=invocation,
                        delta=str(previous - cost),
                        cost=str(cost),
                        cost_provenance="token-priced-prefix",
                        note=note,
                    ),
                    dict(
                        type="unresolved",
                        id=invocation,
                        provisional=True,
                        reserve=str(tail),
                        lower_bound_cost=session.get("lower_bound_cost", "0"),
                        lower_bound_totals=session.get("lower_bound_totals", {}),
                        usage_checkpoint=stamp,
                        usage_tail=str(tail),
                        outcome="incomplete request; token-priced prefix charged",
                        note=note,
                    ),
                ]
            self.append(state, [self.accrue(state), *events])
            return result

    def recover(self, note, *, dry_run=False):
        from .budget_recovery import recover

        return recover(self, note, dry_run=dry_run)

    def cooldown(self, model, seconds):
        with self.locked():
            state = self.replay()
            until = max(state["cooldowns"].get(model, 0), self.clock() + max(0, seconds))
            self.append(state, [self.accrue(state), dict(type="cooldown", model=model, until=until)])

    def repair(self, note):
        """Archive exact damaged bytes and start a new ledger by replaying only the verified prefix."""
        if not note.strip():
            raise BudgetError("ledger repair requires --note")
        with self.locked():
            raw = self.ledger.read_bytes()
            # Only a torn final line is repairable automatically. Complete invalid records require investigation.
            if raw.endswith(b"\n"):
                raise BudgetError("only a torn final write can be repaired automatically")
            prefix = raw[: raw.rfind(b"\n") + 1]
            state = self.replay(self.records(prefix))
            archive = self.directory / f"events.damaged-{uuid.uuid4().hex}.jsonl"
            replacement = self.directory / f"events.repair-{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(raw)
                    out.flush()
                    os.fsync(out.fileno())
                self.sync_directory()
                # A torn suffix may represent a paid launch or a settlement. Freeze admissions until audited.
                events = [
                    self.accrue(state),
                    dict(
                        type="admit",
                        id="repair-" + uuid.uuid4().hex,
                        worker="ledger-repair",
                        model="unknown",
                        phase="repair",
                        estimate="0",
                        status="unresolved",
                        started=self.clock(),
                        archive=str(archive),
                        note=note,
                    ),
                ]
                fd = os.open(replacement, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(prefix + self.record_bytes(state, events))
                    out.flush()
                    os.fsync(out.fileno())
                # The authoritative path is always either the damaged original (which fails closed)
                # or a complete replacement containing the unresolved repair marker.
                os.replace(replacement, self.ledger)
                self.sync_directory()
            except BaseException:
                # Preserve both files on failure; never silently restore a discarded suffix.
                raise BudgetError(f"repair incomplete; original bytes preserved at {archive}") from None
        return self.snapshot()

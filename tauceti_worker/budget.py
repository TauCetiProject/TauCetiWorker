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


class BudgetError(ValueError):
    pass


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
                        )
                        cost = money(event["cost"])
                        state["balance"] -= cost
                        state["spent"] += cost
                        key = session["model"] + ":" + session["phase"]
                        state["samples"].setdefault(key, []).append(cost)
                    elif kind == "unresolved":
                        state["sessions"][event["id"]]["status"] = "unresolved"
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

    def snapshot(self):
        with self.locked():
            state = self.replay()
            self.accrue(state)
            state["waiting"] = self.waiting_state(state)
        active = [s for s in state["sessions"].values() if s["status"] != "settled"]
        pending = sum((money(s["estimate"]) for s in active), Decimal(0))
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
        return dict(
            balance=str(state["balance"]),
            grant_rate=str(state["rate"]),
            spent=str(state["spent"]),
            pending=str(pending),
            available=str(available),
            active_sessions=active,
            unresolved=[s["id"] for s in active if s["status"] == "unresolved"],
            waiting=waiting,
            balance_zero_at=zero_at,
            clock_backwards=money(now) < state["watermark"],
            ledger=str(self.ledger),
            cooldowns=state["cooldowns"],
        )

    def admit(self, invocation, worker, model, phase, *, bridge=None, resume=None):
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
                    if orphaned:
                        session["status"] = "unresolved"
                        events.append(dict(type="unresolved", id=session["id"], outcome="bridge disappeared"))
            costs = state["samples"].get(model + ":" + phase) or [
                money(s["cost"]) for s in state["sessions"].values() if s["model"] == model and s["status"] == "settled"
            ]
            costs = sorted(costs[-20:])
            estimate = costs[math.ceil(len(costs) * 0.75) - 1] if costs else Decimal(0)
            available = state["balance"] - sum((money(s["estimate"]) for s in active), Decimal(0))
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
            if any(s["status"] == "unresolved" for s in active):
                reason = "unresolved session cost; use budget --reconcile"
            elif resume and any(s.get("resume") == resume for s in active):
                reason = "provider session already being resumed"
            elif state["cooldowns"].get(model, 0) > now:
                reason = "API throughput cooldown"
            elif not costs and any(s["model"] == model for s in active):
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
                )
            )
            self.append(state, events)
            queue.pop(worker, None)
            self.save_waiting(queue)
            return dict(allowed=True, estimate=str(estimate))

    def settle(self, invocation, cost, outcome="completed", *, note=None, totals=None):
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
                events.append(
                    dict(type="settle", id=invocation, cost=str(value), outcome=outcome, note=note, totals=totals or {})
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
            if session["status"] == "active" and session.get("bridge"):
                try:
                    live = self.clock() - (Path(session["bridge"]) / "outbox/heartbeat").stat().st_mtime <= 30
                except FileNotFoundError:
                    live = False
                if live:
                    raise BudgetError("cannot reconcile an active session with a live accounting bridge")
            if session["status"] != "settled":
                events = [
                    self.accrue(state),
                    dict(type="settle", id=invocation, cost=str(value), outcome="reconciled", note=note),
                ]
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
        totals = receipt["totals"]
        cost = None
        with self.locked():
            state = self.replay()
            session = state["sessions"].get(receipt["id"])
            if not session:
                raise BudgetError("receipt has no recorded admission")
            if session["status"] == "settled":
                return
            baseline = {}
            for prior in state["sessions"].values():
                if prior["id"] == receipt["id"]:
                    continue
                for provider_id, value in prior.get("totals", {}).items():
                    baseline[provider_id] = max(baseline.get(provider_id, Decimal(0)), money(value))
            if totals is not None:
                differences = [money(v) - baseline.get(k, Decimal(0)) for k, v in totals.items()]
                if all(v >= 0 for v in differences):
                    cost = sum(differences, Decimal(0))
        self.settle(receipt["id"], cost, receipt["outcome"], totals=totals, note=note)

    def recover(self, note):
        if not note.strip():
            raise BudgetError("receipt recovery requires --note")
        from .claude_api import safe_message

        with self.locked():
            sessions = self.replay()["sessions"]
        for session in sessions.values():
            if session["status"] == "settled" or not session.get("bridge"):
                continue
            bridge = Path(session["bridge"])
            try:
                if self.clock() - (bridge / "outbox/heartbeat").stat().st_mtime <= 30:
                    continue
            except FileNotFoundError:
                pass
            invocation = session["id"]
            if len(invocation) != 32 or any(c not in "0123456789abcdef" for c in invocation):
                raise BudgetError("invalid receipt invocation ID")
            receipt = bridge / "inbox" / (invocation + ".receipt")
            if receipt.exists():
                value = safe_message(receipt)
                if value["id"] != invocation:
                    raise BudgetError("receipt invocation mismatch")
                self.settle_receipt(value, note=note)
            else:
                self.settle(invocation, None, "orphaned without receipt", note=note)
            (bridge / "key").unlink(missing_ok=True)
        return self.snapshot()

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

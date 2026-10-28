"""Shared local grant accounting. The append-only ledger is the authority."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

ESTIMATE_PROVENANCES = {"token-priced", "historical-estimate", "prelaunch-estimate"}


class BudgetError(ValueError):
    pass


class LedgerError(BudgetError):
    """Unavailable accounting history; runtime work may continue without it."""


ACCOUNTING_ERRORS = (LedgerError, OSError, sqlite3.Error)


def admission_pause(snapshot, *, worker=None, model=None, now=None):
    """Cheap preflight; invocation admission remains the authoritative atomic check."""
    now = time.time() if now is None else now
    waiting = snapshot.get("waiting", [])
    if model is None and worker is not None:
        previous = next((item for item in waiting if item["worker"] == worker), None)
        model = previous.get("model") if previous else None
    if model and snapshot.get("cooldowns", {}).get(model, 0) > now:
        return "API throughput cooldown"
    if model and model in snapshot.get("calibrating_models", []):
        return "waiting for calibration session cost"
    if money(snapshot["available"]) <= 0 or money(snapshot["available"]) < money(snapshot["admission_threshold"]):
        return "waiting for funding"
    if worker is not None and model:
        first = min(
            (
                item
                for item in waiting
                if item.get("model") == model
                and item["reason"] in {"waiting for funding", "waiting behind another worker"}
            ),
            key=lambda item: (item["since"], item["ticket"]),
            default=None,
        )
        if first and first["worker"] != worker:
            return "waiting behind another worker"
    return None


def process_identity(pid=None):
    """Host bridge identity, including PID reuse and reboot protection on Linux."""
    if sys.platform == "darwin":
        from .usage_lifecycle import identity

        try:
            return identity(pid)
        except OSError:
            return None
    if sys.platform != "linux":
        return None
    try:
        pid = os.getpid() if pid is None else pid
        return dict(
            pid=pid,
            boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            view=os.readlink("/proc/self/ns/pid"),
            start=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
        )
    except (OSError, IndexError):
        return None


def bridge_owner_alive(owner):
    if owner and sys.platform == "darwin":
        from .usage_lifecycle import alive

        return alive(owner)
    if not owner or sys.platform != "linux":
        return False
    try:
        if owner.get("view") and owner["view"] != os.readlink("/proc/self/ns/pid"):
            return True
        fields = Path(f"/proc/{int(owner['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
        return (
            owner["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            and owner["start"] == fields[19]
            and fields[0] not in {"Z", "X"}
        )
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        return False


def orphaned_bridge(session, now):
    """Use the same liveness evidence for preflight and authoritative admission."""
    if session["status"] != "active" or not session.get("bridge"):
        return False
    heartbeat = Path(session["bridge"]) / "outbox/heartbeat"
    try:
        stale = now - heartbeat.stat().st_mtime > 30
    except FileNotFoundError:
        stale = True
    return stale and not bridge_owner_alive(session.get("owner"))


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
        self._ledger_failure = None

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / "budget.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.recover_tail()
            yield

    def recover_tail(self):
        """Discard an interrupted append under budget.lock; archives are best effort."""
        try:
            stream = self.ledger.open("r+b")
        except FileNotFoundError:
            return
        with stream:
            end = stream.seek(0, os.SEEK_END)
            if not end:
                return
            stream.seek(end - 1)
            if stream.read(1) == b"\n":
                return
            offset = end
            chunks = []
            while offset:
                size = min(offset, 8192)
                offset -= size
                stream.seek(offset)
                chunk = stream.read(size)
                if (newline := chunk.rfind(b"\n")) >= 0:
                    offset += newline + 1
                    chunks.append(chunk[newline + 1 :])
                    break
                chunks.append(chunk)
            suffix = b"".join(reversed(chunks))
            # A complete transaction missing only its newline can be retained.
            try:
                record = json.loads(suffix)
                complete = record["version"] == 1 and isinstance(record["events"], list)
            except (ValueError, KeyError, TypeError):
                complete = False
            if complete:
                stream.seek(end)
                stream.write(b"\n")
                stream.flush()
            else:
                archive = self.directory / f"events.discarded-{uuid.uuid4().hex}.json"
                with contextlib.suppress(OSError):
                    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "wb") as out:
                        out.write(suffix)
                stream.truncate(offset)
            os.fsync(stream.fileno())

    def records(self, data: bytes | None = None, *, start=0) -> list[dict]:
        if data is None:
            try:
                data = self.ledger.read_bytes()
            except FileNotFoundError:
                return []
        if data and not data.endswith(b"\n"):
            raise LedgerError(f"incomplete ledger tail: {self.ledger}")
        records = []
        for i, line in enumerate(data.splitlines(), start + 1):
            try:
                rec = json.loads(line)
                if rec["version"] != 1 or rec["seq"] != i or not isinstance(rec["events"], list):
                    raise ValueError("invalid sequence or schema")
                records.append(rec)
            except (ValueError, KeyError, TypeError):
                raise LedgerError(f"invalid ledger record {i}: {self.ledger}") from None
        return records

    def replay(self, records: list[dict] | None = None, *, _state=None, _samples=True):
        state = (
            _state
            if _state is not None
            else dict(
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
                            for field in (
                                "cost_provenance",
                                "totals",
                                "recovery_audit",
                                "finished",
                                "recovery_manifest",
                            ):
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
                            finished=event.get("finished", None if event.get("note") else rec["timestamp"]),
                            recovery_manifest=event.get("recovery_manifest"),
                            recovery_audit=event.get("recovery_audit"),
                        )
                        cost = money(event["cost"])
                        state["balance"] -= cost
                        state["spent"] += cost
                    elif kind == "unresolved":
                        session = state["sessions"][event["id"]]
                        session["status"] = "provisional" if event.get("provisional") else "unresolved"
                        if "finished" in event:
                            session["finished"] = event["finished"]
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
                        raise LedgerError(f"unknown ledger event {kind}")
        except (KeyError, TypeError, InvalidOperation, BudgetError) as exc:
            raise LedgerError(f"invalid accounting event in {self.ledger}: {exc}") from None
        if not _samples:
            return state
        state["samples"] = {}
        for session in state["sessions"].values():
            if session["status"] == "settled":
                key = session["model"] + ":" + session["phase"]
                state["samples"].setdefault(key, []).append(money(session["cost"]))
        return state

    def current(self):
        """Read indexed state under locked(); the JSONL ledger remains authoritative."""
        import sqlite3

        from .budget_projection import Projection

        if self._ledger_failure:
            stamp, error = self._ledger_failure
            if self.ledger_stamp() == stamp:
                raise LedgerError(error)
            self._ledger_failure = None
        projection = Projection(self)
        try:
            return projection.load()
        except LedgerError as error:
            self._ledger_failure = (self.ledger_stamp(), str(error))
            raise
        except (sqlite3.Error, OSError, BudgetError):
            # A disposable optimization must never become a funding outage.
            try:
                projection.discard()
            except OSError:
                pass
            try:
                return self.replay()
            except LedgerError as error:
                self._ledger_failure = (self.ledger_stamp(), str(error))
                raise

    def ledger_stamp(self):
        try:
            info = self.ledger.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        except FileNotFoundError:
            return None

    @staticmethod
    def sessions(state, **filters):
        sessions = state["sessions"]
        if hasattr(sessions, "select"):
            return sessions.select(**filters)
        rows = list(sessions.values())
        for name, value in filters.items():
            if name == "active" and value:
                rows = [s for s in rows if s["status"] != "settled"]
            elif name == "settled" and value:
                rows = [s for s in rows if s["status"] == "settled"]
            elif name in ("model", "phase") and value is not None:
                rows = [s for s in rows if s[name] == value]
            elif name == "estimated" and value:
                rows = [s for s in rows if s.get("cost_provenance") in ESTIMATE_PROVENANCES]
            elif name == "positive" and value:
                rows = [s for s in rows if money(s.get("cost", "0")) > 0]
        return rows[-filters["limit"] :] if filters.get("limit") is not None else rows

    @staticmethod
    def related(state, providers, include=None):
        sessions = state["sessions"]
        if hasattr(sessions, "related"):
            return sessions.related(providers, include=include)
        return [
            s
            for s in sessions.values()
            if s["id"] == include or set(s.get("totals", {})) & set(providers) or s.get("resume") in providers
        ]

    def evidence_sessions(self, state, invocation):
        """Copy the small provider-linked history before releasing the file lock."""
        session = state["sessions"].get(invocation)
        if not session:
            return {}
        providers = set(session.get("totals", {})) | set(session.get("lower_bound_totals", {}))
        if session.get("resume"):
            providers.add(session["resume"])
        if session.get("bridge"):
            from .claude_api import safe_message

            try:
                receipt = safe_message(Path(session["bridge"]) / "inbox" / (invocation + ".receipt"))
                if isinstance(receipt, dict) and receipt.get("id") == invocation:
                    providers.update(receipt.get("lower_bound_totals", {}))
            except (OSError, ValueError, TypeError):
                pass
        return {s["id"]: s for s in self.related(state, providers, include=invocation)}

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
        transaction = json.loads(data)["transaction"]
        fd = os.open(self.ledger, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        before = None
        try:
            before = os.fstat(fd)
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("ledger write made no progress")
                data = data[written:]
        except OSError:
            if data and before is not None:
                # A failed short write must not poison every later reader.
                # A complete write whose fsync failed remains available to replay.
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, before.st_size)
                    os.utime(fd, ns=(before.st_atime_ns, before.st_mtime_ns))
            raise
        finally:
            # Write durability is best effort; complete records remain usable.
            with contextlib.suppress(OSError):
                os.fsync(fd)
            os.close(fd)
        # Persist a newly created ledger's directory entry as well as its contents.
        with contextlib.suppress(OSError):
            self.sync_directory()
        return dict(transaction=transaction, ledger_sequence=state["seq"] + 1)

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
            state = self.current()
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
            state = self.current()
            queue = self.waiting_state(state)
            if worker in queue and self.clock() - queue[worker].get("seen", 0) < 120:
                queue[worker]["seen"] = self.clock()
                self.save_waiting(queue)

    def wait_for_funding(self, worker, model, phase):
        self.wait_for_admission(worker, model, phase, "waiting for funding")

    def wait_for_admission(self, worker, model, phase, reason):
        with self.locked():
            state = self.current()
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
                reason=reason,
                estimate=str(estimate),
            )
            self.save_waiting(queue)

    def estimate(self, state, model, phase=None, *, include_pending=True):
        costs = (
            [
                money(s["cost"])
                for s in self.sessions(state, model=model, phase=phase, settled=True, positive=True, limit=20)
            ]
            if phase
            else []
        )
        costs = costs or [
            money(s["cost"]) for s in self.sessions(state, model=model, settled=True, positive=True, limit=20)
        ]
        if not costs and include_pending:
            costs = [
                money(s["reserve"])
                for s in self.sessions(state, model=model, active=True)
                if money(s.get("reserve", "0")) > 0
            ]
        costs = sorted(costs[-20:])
        if costs:
            return costs, costs[math.ceil(len(costs) * 0.75) - 1]
        recent = [money(s["cost"]) for s in self.sessions(state, settled=True, limit=20)]
        return costs, max([Decimal(1), *recent])

    def snapshot(self, *, worker=None, model=None, phase=None):
        with self.locked():
            state = self.current()
            self.accrue(state)
            state["waiting"] = self.waiting_state(state)
            active = [s for s in self.sessions(state, active=True) if not s.get("archive")]
            estimate = self.estimate(state, model, phase)[1] if model else Decimal(0)
            now = self.clock()
            calibrating = [
                name
                for name in {s["model"] for s in active if s["status"] == "active" and not orphaned_bridge(s, now)}
                if not self.estimate(state, name)[0]
            ]
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
                threshold = estimate
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
            ledger_repair_required=False,
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
            calibrating_models=calibrating,
        )

    def runtime_snapshot(self, **kwargs):
        """Accounting outages are not an instruction to stop worker progress."""
        try:
            return self.snapshot(**kwargs)
        except ACCOUNTING_ERRORS:
            return None

    def admit_work(self, *args, **kwargs):
        """Apply available budget policy; allow work when bookkeeping is unavailable."""
        try:
            return self.admit(*args, **kwargs)
        except ACCOUNTING_ERRORS as error:
            invocation, worker, model, phase = args
            event = dict(
                type="admit",
                id=invocation,
                worker=worker,
                model=model,
                phase=phase,
                estimate="0",
                status="active",
                started=self.clock(),
                **{key: value for key, value in kwargs.items() if key != "lifecycle"},
            )
            return dict(allowed=True, recorded=False, accounting_error=str(error), unrecorded_admit=event)

    def restore_admission(self, event):
        """Record work that already ran during an outage, without a new funding decision."""
        with self.locked():
            state = self.current()
            if event["id"] not in state["sessions"]:
                self.append(state, [self.accrue(state), event])

    def admit(self, invocation, worker, model, phase, *, bridge=None, resume=None, owner=None, lifecycle=None):
        with self.locked():
            state = self.current()
            if invocation in state["sessions"]:
                raise BudgetError("an invocation ID cannot be launched twice")
            events = [self.accrue(state)]
            active = [s for s in self.sessions(state, active=True) if not s.get("archive")]
            for session in active:
                if orphaned_bridge(session, self.clock()):
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
            if resume and any(s.get("resume") == resume for s in live):
                reason = "provider session already being resumed"
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
                with contextlib.suppress(OSError):
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
                    **({"lifecycle": lifecycle} if lifecycle else {}),
                )
            )
            self.append(state, events)
            queue.pop(worker, None)
            with contextlib.suppress(OSError):
                self.save_waiting(queue)
            return dict(allowed=True, estimate=str(estimate))

    def settle(
        self,
        invocation,
        cost,
        outcome="completed",
        *,
        note=None,
        totals=None,
        provenance="native-reported",
        finished=None,
    ):
        value = money(cost) if cost is not None else None
        if value is not None and value < 0:
            raise BudgetError("session cost cannot be negative")
        with self.locked():
            state = self.current()
            if invocation not in state["sessions"]:
                raise BudgetError("unknown invocation")
            session = state["sessions"][invocation]
            if session["status"] == "settled":
                if value is not None and money(session["cost"]) != value:
                    raise BudgetError("conflicting cost receipt; reconcile explicitly")
                return
            events = [self.accrue(state)]
            if value is None:
                event = dict(type="unresolved", id=invocation, outcome=outcome)
                if finished is not None:
                    event["finished"] = finished
                events.append(event)
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
                        **({"finished": finished} if finished is not None else {}),
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
            state = self.current()
            session = state["sessions"].get(invocation)
            if not session:
                raise BudgetError("unknown invocation")
            if session["status"] != "settled" and session.get("bridge"):
                try:
                    live = self.clock() - (Path(session["bridge"]) / "outbox/heartbeat").stat().st_mtime <= 30
                except FileNotFoundError:
                    live = False
                from .usage_supervisor import invocation_alive

                if live or invocation_alive(self.directory, session):
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
            state = self.current()
            session = state["sessions"].get(receipt["id"])
            if not session:
                raise BudgetError("receipt has no recorded admission")
            from .usage_lifecycle import alive
            from .usage_supervisor import external_exit_proved, invocation_lifecycle

            if receipt.get("outcome") == "launch-failed" and totals and all(money(v) == 0 for v in totals.values()):
                from .usage_accounting import session_usage

                journal = session_usage(self.directory, session)
                if money(session.get("cost", "0")) > 0 or (journal and journal["messages"]):
                    raise BudgetError("no-inference receipt contradicts durable usage; reserve retained")

            lifecycle = invocation_lifecycle(self.directory, session)
            if (
                totals is not None
                and lifecycle
                and lifecycle.get("native")
                and alive(lifecycle["native"])
                and not (lifecycle.get("external") and external_exit_proved(lifecycle))
            ):
                raise BudgetError("cannot settle a live native invocation; receipt retained for recovery")
            if session["status"] == "settled" and session.get("cost_provenance") not in ESTIMATE_PROVENANCES:
                return
            finished = receipt.get("finished")
            if finished is not None and (
                isinstance(finished, bool)
                or money(finished) < money(session["started"])
                or money(finished) > money(self.clock())
            ):
                raise BudgetError("invalid receipt finish time")
            ordered = self.related(state, set(totals or {}) | set(lower or {}), include=receipt["id"])
            position = next(i for i, entry in enumerate(ordered) if entry["id"] == receipt["id"])
            baseline = {}
            for prior in ordered:
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
                        **({"finished": finished} if finished is not None else {}),
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
                                **({"finished": finished} if finished is not None else {}),
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
                                **({"finished": finished} if finished is not None else {}),
                            )
                        )
                    self.append(state, events)
                    return
        if session["status"] != "settled":
            self.settle(receipt["id"], None, receipt["outcome"], note=note, finished=finished)

    def recover_usage(self, invocation, *, note="saved usage checkpoint", allow_live_owner=False, dry_run=False):
        """Settle a stopped complete journal, or charge its prefix and reserve uncertainty.

        No reservations are treated as costs. Repeated recovery is idempotent.
        A late native receipt reverses the estimated prefix before settlement.
        """
        from .budget_usage import evidence
        from .usage_supervisor import invocation_alive, invocation_lifecycle

        def live(session):
            # Legacy finish may bypass its bridge owner; a registered native
            # invocation must always be stopped, even during bridge cleanup.
            return (
                invocation_alive(self.directory, session)
                if invocation_lifecycle(self.directory, session)
                else (not allow_live_owner and bridge_owner_alive(session.get("owner")))
            )

        with self.locked():
            sessions = self.evidence_sessions(self.current(), invocation)
        session = sessions.get(invocation)
        if not session or session["status"] == "settled" or live(session):
            return None
        saved = evidence(self.directory, session, sessions)
        if not saved or not saved["items"]:
            return None
        with self.locked():
            state = self.current()
            session = state["sessions"].get(invocation)
            if not session or session["status"] == "settled" or live(session):
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
                    for prior in self.related(state, [primary], include=invocation):
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

    def recover(self, note, *, dry_run=False, plan=None):
        from .budget_recovery import recover

        return recover(self, note, dry_run=dry_run, plan=plan)

    def cooldown(self, model, seconds):
        with self.locked():
            state = self.current()
            until = max(state["cooldowns"].get(model, 0), self.clock() + max(0, seconds))
            self.append(state, [self.accrue(state), dict(type="cooldown", model=model, until=until)])

    def repair(self, note):
        """Compatibility command: normal locked access already repairs interrupted appends."""
        if not note.strip():
            raise BudgetError("ledger repair requires --note")
        return self.snapshot()

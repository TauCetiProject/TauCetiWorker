"""Local usage recovery outside round process groups and agent sandboxes.

Only new, explicitly registered invocations participate. Journals, receipts and
ledger corrections remain the authority; lifecycle observations never close a
journal or turn a stopped process into proof of exact expenditure.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .budget import ACCOUNTING_ERRORS, Budget, BudgetError, LedgerError, bridge_owner_alive, money
from .claude_api import safe_message
from .usage_accounting import session_usage, usage_path
from .usage_lifecycle import alive, durable, identity, resolve


def lifecycle_path(directory, invocation):
    return Path(directory) / "lifecycles" / (invocation + ".json")


def read_lifecycle(directory, invocation):
    try:
        path = lifecycle_path(directory, invocation)
        try:
            value = safe_message(path)
        except FileNotFoundError:
            value = safe_message(path.parent / "done" / path.name)
        if value.get("version") != 1 or value.get("id") != invocation:
            raise ValueError("invalid usage lifecycle")
        return value
    except FileNotFoundError:
        return None


def invocation_lifecycle(directory, session):
    value = read_lifecycle(directory, session["id"]) or session.get("lifecycle")
    if value and value["phase"] == "admitted":
        try:
            permit = safe_message(Path(value["bridge"]) / "outbox" / (value["id"] + ".permit"))
            if permit.get("id") != value["id"]:
                raise ValueError("native permit invocation mismatch")
            value = {
                **value,
                "phase": "permitted",
                "native": permit.get("native", {"unsupported": True}),
                "gate": permit["gate"],
            }
        except FileNotFoundError:
            pass
    return value


def invocation_alive(directory, session, *, legacy_owner=True):
    value = invocation_lifecycle(directory, session)
    if value is None:
        return legacy_owner and bridge_owner_alive(session.get("owner"))
    if value.get("phase") == "reconciled":
        return False
    if value.get("external"):
        return not external_exit_proved(value)
    return alive(value["shim"]) or bool(value.get("native") and alive(value["native"]))


def external_exit_proved(value):
    """VM PIDs are never interpreted on the host. Require an exact native receipt.

    A remote bridge death alone leaves liveness unknown and retains the reserve.
    The copied shim records its native wait result with the exec gate's birth ID.
    """
    bridge, invocation = Path(value["bridge"]), value["id"]
    try:
        boundary = safe_message(bridge / "outbox" / ("boundary-" + str(value.get("boundary")) + ".stopped.json"))
        if (
            value.get("boundary")
            and boundary.get("bridge") == bridge.name
            and boundary.get("boundary") == value["boundary"]
        ):
            return True
    except FileNotFoundError:
        pass
    for suffix in ("terminal", "no-exec"):
        try:
            proof = safe_message(bridge / "inbox" / (invocation + "." + suffix))
            if proof.get("id") == invocation and value.get("gate") and proof.get("native") == value["gate"]:
                return True
        except FileNotFoundError:
            pass
    try:
        receipt = safe_message(bridge / "inbox" / (invocation + ".receipt"))
        totals = receipt.get("totals")
        if (
            receipt.get("id") == invocation
            and receipt.get("outcome") == "exit:0"
            and not receipt.get("provisional")
            and isinstance(totals, dict)
            and totals
        ):
            from .budget import money

            return all(isinstance(k, str) and isinstance(v, str) and money(v) >= 0 for k, v in totals.items())
    except FileNotFoundError:
        pass
    return False


def registration(
    invocation,
    ticket,
    bridge,
    shim,
    *,
    enabled,
    owner,
    parent,
    deadline=None,
    allow_remote=False,
    boundary=None,
    supervisor_view=None,
):
    try:
        process = resolve(shim)
    except (OSError, ValueError):
        if not allow_remote:
            raise
        process = None
    if (
        process is None
        and allow_remote
        and isinstance(shim, dict)
        and shim.get("pid")
        and shim.get("start")
        and shim.get("boot")
    ):
        process = dict(remote=shim)
    if process is not None and supervisor_view and process.get("view") and process["view"] != supervisor_view:
        process = dict(remote=shim)
    if process is None:
        raise ValueError("admission requester stopped before identity was recorded")
    return dict(
        version=1,
        id=invocation,
        admission_id=ticket,
        bridge=str(bridge),
        shim=process,
        external=bool(process.get("remote")),
        boundary=boundary,
        owner=owner,
        parent=parent,
        deadline=deadline,
        budgeted=enabled,
        phase="admitted",
        events=[dict(kind="admitted", at=time.time())],
    )


def register(directory, value):
    path = lifecycle_path(directory, value["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() and not (path.parent / "done" / path.name).exists():
        durable(path, value)


def prepare_boundary(bridge):
    boundary = uuid.uuid4().hex
    durable(Path(bridge) / "outbox/active-boundary.json", dict(boundary=boundary))
    return boundary


def record_boundary_stopped(bridge, boundary):
    bridge = Path(bridge)
    durable(
        bridge / "outbox" / ("boundary-" + boundary + ".stopped.json"),
        dict(bridge=bridge.name, boundary=boundary, at=time.time()),
    )


def archive_lifecycle(path, value):
    value["phase"] = "reconciled"
    durable(path, value)
    done = path.parent / "done"
    done.mkdir(exist_ok=True, mode=0o700)
    path.rename(done / path.name)
    for directory in (done, path.parent):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def evidence_stamp(directory, value):
    inbox = Path(value["bridge"]) / "inbox"
    paths = [inbox / (value["id"] + suffix) for suffix in (".receipt", ".exec-failed", ".no-exec")]
    journals = [usage_path(directory, value["id"], value["bridge"]), usage_path(directory, value["id"])]
    proofs = [
        inbox / (value["id"] + ".terminal"),
        Path(value["bridge"]) / "outbox" / ("boundary-" + str(value.get("boundary")) + ".stopped.json"),
    ]
    return [stat_stamp(p) for p in paths + journals + [p.with_suffix(".jsonl") for p in journals] + proofs]


def reserve_unknown_usage(budget, value, stamp):
    """Unknown remote liveness can enlarge a reserve, never commit expenditure."""
    if value.get("unknown_reserve_stamp") == stamp:
        return
    from .budget_usage import evidence

    with budget.locked():
        sessions = budget.evidence_sessions(budget.current(), value["id"])
    session = sessions.get(value["id"])
    if not session or session["status"] == "settled":
        return
    saved = evidence(budget.directory, session, sessions)
    if not saved:
        return
    with budget.locked():
        state = budget.current()
        session = state["sessions"][value["id"]]
        if session["status"] == "settled":
            return
        known = max(saved["cost"], money(session.get("lower_bound_cost", "0")))
        tail = max(
            money(session["estimate"]),
            budget.estimate(state, session["model"], session["phase"], include_pending=False)[1],
            known,
        )
        reserve = max(
            money(session.get("reserve", session["estimate"])),
            max(money(0), known - money(session.get("cost", "0"))) + tail,
        )
        if money(session.get("reserve", session["estimate"])) < reserve or session["status"] != "provisional":
            budget.append(
                state,
                [
                    budget.accrue(state),
                    dict(
                        type="unresolved",
                        id=value["id"],
                        provisional=True,
                        reserve=str(reserve),
                        lower_bound_cost=str(known),
                        lower_bound_totals=session.get("lower_bound_totals", {}),
                        outcome="remote liveness unknown; token-priced usage plus uncertain tail reserved",
                    ),
                ],
            )
    value["unknown_reserve_stamp"] = stamp


def round_key(owner):
    import hashlib

    return hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest() + ".json"


def record_round_end(directory, owner, kind, returncode=None):
    if owner is None:
        return
    path = Path(directory) / "round-exits" / round_key(owner)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    durable(path, dict(owner=owner, kind=kind, returncode=returncode, at=time.time()))


def stat_stamp(path):
    try:
        info = Path(path).stat()
        return [info.st_ino, info.st_mtime_ns, info.st_size]
    except FileNotFoundError:
        return None


def tick(budget, cache=None, stop=None):
    """One restart-safe pass; replay the ledger only when its bytes changed."""
    cache = {} if cache is None else cache
    ledger_stamp = stat_stamp(budget.ledger)
    if cache.get("ledger_stamp") != ledger_stamp or "sessions" not in cache:
        try:
            with budget.locked():
                state = budget.current()
                rows = budget.sessions(state, active=True)
                if "sessions" not in cache:
                    rows += budget.sessions(state, settled=True, estimated=True)
                sessions = {session["id"]: session for session in rows}
                for path in (budget.directory / "lifecycles").glob("*.json"):
                    value = state["sessions"].get(path.stem)
                    if value:
                        sessions.setdefault(path.stem, value)
            cache["ledger_available"] = True
        except ACCOUNTING_ERRORS as error:
            # Launch supervision must keep running while ledger reads recover.
            rows, sessions = [], cache.get("sessions", {})
            cache["ledger_available"] = False
            if not isinstance(error, LedgerError):
                ledger_stamp = None  # Retry transient storage errors, not unchanged corrupt bytes.
        for session in rows:
            if session.get("lifecycle"):
                register(budget.directory, session["lifecycle"])
        cache.update(ledger_stamp=ledger_stamp, sessions=sessions)
    errors = cache.setdefault("errors", {})
    paths = list((budget.directory / "lifecycles").glob("*.json"))
    paths.sort(key=lambda path: cache["sessions"].get(path.stem, {}).get("status") != "active")
    for path in paths:
        if stop is not None and stop.is_set():
            return
        try:
            recover_invocation(budget, path, cache=cache, session=cache["sessions"].get(path.stem))
            errors.pop(path.stem, None)
        except Exception as error:
            stamp = [str(error), stat_stamp(path), ledger_stamp, stat_stamp(budget.directory / "usage-recovery-mode")]
            if errors.get(path.stem) != stamp:
                print(f"{path.stem}: usage recovery retained for retry: {type(error).__name__}: {error}", flush=True)
                errors[path.stem] = stamp


def recovery_mode(directory):
    try:
        mode = (Path(directory) / "usage-recovery-mode").read_text().strip()
    except FileNotFoundError:
        mode = "auto"
    if mode not in {"auto", "record-only"}:
        raise ValueError("usage-recovery-mode must contain auto or record-only")
    return mode


def recover_invocation(budget, path, *, cache=None, session=None):
    mode = recovery_mode(budget.directory)
    value = read_lifecycle(budget.directory, path.stem)
    bridge, invocation = Path(value["bridge"]), value["id"]
    inbox, outbox = bridge / "inbox", bridge / "outbox"
    stamp = evidence_stamp(budget.directory, value)
    if value["phase"] in {"stopped", "no-inference"} and value.get("recovery_stamp") == stamp:
        return
    if value.get("unknown_reserve_stamp") == stamp and any(
        e["kind"] == "remote-liveness-unconfirmed" for e in value["events"]
    ):
        return
    if (session and session["status"] == "settled" and session.get("cost_provenance") == "native-reported") or (
        not value["budgeted"] and value["phase"] == "stopped"
    ):
        if session:
            kind = (
                "failed-launch"
                if session.get("outcome") == "launch-failed"
                else "successful-exit"
                if session.get("outcome") == "exit:0"
                else "native-receipt"
            )
            if not any(e["kind"] == kind for e in value["events"]):
                value["events"].append(dict(kind=kind, at=time.time()))
        archive_lifecycle(path, value)
        return
    changed = False

    def event(kind, value=value, **fields):
        nonlocal changed
        if not any(e["kind"] == kind for e in value["events"]):
            value["events"].append(dict(kind=kind, at=time.time(), **fields))
            changed = True

    launch = inbox / (invocation + ".launch")
    if value["phase"] == "admitted" and launch.exists():
        message = safe_message(launch)
        if message.get("id") != invocation:
            raise ValueError("native launch invocation mismatch")
        if value.get("gate") != message.get("native"):
            value["gate"] = message["native"]
            changed = True
    permit = outbox / (invocation + ".permit")
    # A permit is a second durable copy of the native identity. Rebuilding an
    # admission sidecar must never erase evidence that inference was authorized.
    if value["phase"] == "admitted" and permit.exists():
        permission = safe_message(permit)
        if permission.get("id") != invocation:
            raise ValueError("native permit invocation mismatch")
        value.update(phase="permitted", native=permission.get("native", {"unsupported": True}), gate=permission["gate"])
        event("launch-permitted")
    if (
        value["phase"] == "admitted"
        and launch.exists()
        and alive(value["shim"])
        and session_usage(budget.directory, value)
    ):
        message = safe_message(launch)
        if message.get("id") != invocation:
            raise ValueError("native launch invocation mismatch")
        native = dict(remote=message["native"]) if value.get("external") else resolve(message.get("native"))
        if native is not None and alive(native):
            value.update(phase="permitted", native=native, gate=message["native"])
            event("launch-permitted")
            durable(path, value)  # Identity must survive before the gate can exec.
    if value["phase"] == "permitted" and alive(value["shim"]) and alive(value["native"]):
        permit = outbox / (invocation + ".permit")
        if not permit.exists():
            durable(permit, dict(id=invocation, gate=value["gate"], native=value["native"]))
    round_end = budget.directory / "round-exits" / round_key(value.get("owner"))
    if value.get("owner") and round_end.exists():
        ending = safe_message(round_end)
        event("round-" + ending["kind"], returncode=ending.get("returncode"))
    terminal = inbox / (invocation + ".terminal")
    if terminal.exists():
        message = safe_message(terminal)
        if message.get("id") != invocation:
            raise ValueError("native terminal invocation mismatch")
        event(message["kind"], returncode=message.get("returncode"))
    if not bridge_owner_alive(value.get("owner")):
        event("bridge-death")
    if not alive(value.get("parent")):
        event("parent-death", scope="bridge-parent")
    stopped = (
        external_exit_proved(value)
        if value.get("external")
        else not alive(value["shim"]) and not (value.get("native") and alive(value["native"]))
    )
    if value.get("external") and not stopped:
        event("remote-liveness-unconfirmed")
    journal = session_usage(budget.directory, value)
    has_usage = bool(journal and journal["messages"]) or bool(session and money(session.get("cost", "0")) > 0)
    if stopped:
        event("stopped")
        if value["phase"] == "admitted" and not launch.exists() and not has_usage:
            # No permit was issued, and no future pass may issue one. Even
            # a delayed orphan gate cannot execute inference after this.
            value["phase"] = "no-inference"
            event("failed-launch")
        elif value["phase"] == "permitted":
            value["phase"] = "stopped"
            changed = True
    if changed:
        durable(path, value)
    if cache is not None and not cache.get("ledger_available", True):
        return  # Supervision above still runs; defer ledger work until it is readable.
    if not stopped or not value["budgeted"]:
        if value["budgeted"] and value.get("external") and mode == "auto":
            prior_stamp = value.get("unknown_reserve_stamp")
            reserve_unknown_usage(budget, value, stamp)
            if prior_stamp != value.get("unknown_reserve_stamp"):
                durable(path, value)
        if stopped and not value["budgeted"]:
            archive_lifecycle(path, value)
        return
    if mode == "record-only":
        return
    receipt_path = inbox / (invocation + ".receipt")
    if value.get("recovery_stamp") == stamp or (cache is not None and cache.get(invocation) == stamp):
        return
    if value.get("unrecorded_admit"):
        budget.restore_admission(value["unrecorded_admit"])
    if session and session["status"] == "settled" and session.get("cost_provenance") == "native-reported":
        value["recovery_stamp"] = stamp
        durable(path, value)
        return
    no_exec = False
    proof_path = inbox / (invocation + ".no-exec")
    if proof_path.exists():
        proof = safe_message(proof_path)
        no_exec = proof.get("id") == invocation and proof.get("native") == value.get("gate")
    if no_exec:
        event("contradictory-no-exec-evidence" if has_usage else "failed-launch")
    if (value["phase"] == "no-inference" or no_exec) and not has_usage:
        budget.settle_receipt(dict(id=invocation, totals={invocation: "0"}, outcome="launch-failed"))
    elif receipt_path.exists():
        receipt = safe_message(receipt_path)
        if receipt.get("id") != invocation:
            raise ValueError("native receipt invocation mismatch")
        if receipt.get("outcome") != "launch-failed" or not has_usage:
            budget.settle_receipt(receipt, note="usage supervisor: durable native receipt")
        else:
            event("contradictory-no-exec-evidence")
    budget.recover_usage(invocation, note="usage supervisor: stopped invocation")
    # Missing evidence also retains the admission reserve. Do not keep
    # appending unresolved events on every supervisor pass.
    with budget.locked():
        session = budget.current()["sessions"].get(invocation)
    if session and session["status"] == "active":
        budget.settle(invocation, None, "stopped; missing usage evidence")
    value["recovery_stamp"] = stamp
    durable(path, value)
    if cache is not None:
        cache[invocation] = stamp


def ensure_supervisor(directory, *, refresh=False):
    """Called by outer workers and before contexts; restarting replays saved evidence."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready = directory / "usage-supervisor.json"
    version = code_version()
    try:
        value = safe_message(ready)
        same_source = value.get("source") == str(Path(__file__).resolve())
        if alive(value["owner"]) and (
            refresh or not value.get("ledger_optional") or same_source and value.get("code_version") != version
        ):
            durable(directory / "usage-supervisor-retire.json", dict(owner=value["owner"], at=time.time()))
            deadline = time.monotonic() + 10
            while alive(value["owner"]) and time.monotonic() < deadline:
                time.sleep(0.05)
        elif alive(value["owner"]) and value.get("protocol") == 1 and time.time() - value["heartbeat"] < 10:
            return
    except (OSError, ValueError, KeyError):
        pass
    from .paths import self_env

    # This process is created by the host bridge/outer worker, never by the
    # copied shim inside an agent sandbox. setsid survives round group teardown.
    with (directory / "usage-supervisor.log").open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "tauceti_worker.usage_supervisor", str(directory)],
            env=self_env(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value = safe_message(ready)
            if (
                alive(value["owner"])
                and value.get("protocol") == 1
                and value.get("ledger_optional")
                and time.time() - value["heartbeat"] < 10
            ):
                # Reap duplicate starters which lost the singleton lock.
                threading.Thread(target=process.wait, daemon=True).start()
                return
        except (OSError, ValueError, KeyError):
            pass
        if process.poll() not in (None, 0):
            raise BudgetError("usage supervisor failed to start; see usage-supervisor.log")
        time.sleep(0.05)
    raise BudgetError("usage supervisor unavailable; no inference admitted")


def code_version():
    import hashlib

    names = ("usage_supervisor.py", "usage_lifecycle.py", "usage_accounting.py", "budget.py", "budget_usage.py")
    return hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes() for name in names)).hexdigest()


def run(directory):
    budget = Budget(Path(directory))
    with (budget.directory / "usage-supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stop = threading.Event()
        version = code_version()
        owner = identity()

        def heartbeat():
            while not stop.is_set():
                try:
                    try:
                        retirement = safe_message(budget.directory / "usage-supervisor-retire.json")
                    except FileNotFoundError:
                        retirement = {}
                    if retirement.get("owner") == owner or code_version() != version:
                        stop.set()  # Let the next new-launch check load current code.
                        return
                    durable(
                        budget.directory / "usage-supervisor.json",
                        dict(
                            owner=owner,
                            heartbeat=time.time(),
                            code_version=version,
                            protocol=1,
                            ledger_optional=True,
                            source=str(Path(__file__).resolve()),
                        ),
                    )
                except OSError:
                    stop.set()
                    return
                stop.wait(1)

        monitor = threading.Thread(target=heartbeat, daemon=True)
        monitor.start()
        cache = {}
        original_directory = budget.directory.stat().st_ino
        try:
            while (
                not stop.is_set() and budget.directory.exists() and budget.directory.stat().st_ino == original_directory
            ):
                try:
                    tick(budget, cache=cache, stop=stop)
                except Exception as error:
                    message = f"usage recovery retained for retry: {type(error).__name__}: {error}"
                    if cache.get("last_error") != message:
                        print(message, flush=True)
                        cache["last_error"] = message
                stop.wait(0.5)
        finally:
            stop.set()
            monitor.join()


if __name__ == "__main__":
    run(sys.argv[1])

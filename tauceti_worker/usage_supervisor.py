"""Local usage recovery outside round process groups and containment boundaries.

Only new, explicitly registered invocations participate. Journals, receipts and
ledger corrections remain the authority; lifecycle observations never close a
journal or turn a stopped process into proof of exact expenditure.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from .budget import Budget, BudgetError, bridge_owner_alive
from .claude_api import safe_message
from .usage_accounting import session_usage, usage_path
from .usage_lifecycle import alive, durable, identity, resolve


def lifecycle_path(directory, invocation):
    return Path(directory) / "lifecycles" / (invocation + ".json")


def read_lifecycle(directory, invocation):
    try:
        value = safe_message(lifecycle_path(directory, invocation))
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
    if value.get("external"):
        return not external_exit_proved(value)
    return alive(value["shim"]) or bool(value.get("native") and alive(value["native"]))


def external_exit_proved(value):
    """VM PIDs are never interpreted on the host. Require an exact native receipt.

    A remote bridge death alone leaves liveness unknown and retains the reserve.
    The copied shim records its native wait result with the exec gate's birth ID.
    """
    bridge, invocation = Path(value["bridge"]), value["id"]
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


def registration(invocation, ticket, bridge, shim, *, enabled, owner, parent, deadline=None, allow_remote=False):
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
    if process is None:
        raise ValueError("admission requester stopped before identity was recorded")
    return dict(
        version=1,
        id=invocation,
        admission_id=ticket,
        bridge=str(bridge),
        shim=process,
        external=bool(process.get("remote")),
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
    if not path.exists():
        durable(path, value)


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
        with budget.locked():
            sessions = budget.replay()["sessions"]
        for session in sessions.values():
            if session.get("lifecycle"):
                register(budget.directory, session["lifecycle"])
        cache.update(ledger_stamp=ledger_stamp, sessions=sessions)
    errors = cache.setdefault("errors", {})
    for path in (budget.directory / "lifecycles").glob("*.json"):
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
    if value.get("external") and not stopped and not bridge_owner_alive(value.get("owner")):
        event("remote-liveness-unconfirmed")
    journal = session_usage(budget.directory, value)
    has_usage = bool(journal and journal["messages"]) or bool(
        session and session.get("cost") and float(session["cost"]) > 0
    )
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
    if not stopped or not value["budgeted"]:
        return
    if mode == "record-only":
        return
    receipt_path = inbox / (invocation + ".receipt")
    evidence_paths = [
        receipt_path,
        inbox / (invocation + ".exec-failed"),
        inbox / (invocation + ".no-exec"),
        usage_path(budget.directory, invocation, value["bridge"]),
        usage_path(budget.directory, invocation),
    ]
    evidence_paths += [p.with_suffix(".jsonl") for p in evidence_paths[-2:]]
    stamp = [stat_stamp(p) for p in evidence_paths]
    if value.get("recovery_stamp") == stamp or (cache is not None and cache.get(invocation) == stamp):
        return
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
        session = budget.replay()["sessions"].get(invocation)
    if session and session["status"] == "active":
        budget.settle(invocation, None, "stopped; missing usage evidence")
    value["recovery_stamp"] = stamp
    durable(path, value)
    if cache is not None:
        cache[invocation] = stamp


def ensure_supervisor(directory):
    """Called by outer workers and before contexts; restarting replays saved evidence."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready = directory / "usage-supervisor.json"
    version = code_version()
    try:
        value = safe_message(ready)
        if alive(value["owner"]) and value.get("code_version") != version:
            deadline = time.monotonic() + 10
            while alive(value["owner"]) and time.monotonic() < deadline:
                time.sleep(0.05)
        elif alive(value["owner"]) and time.time() - value["heartbeat"] < 10:
            return
    except (OSError, ValueError, KeyError):
        pass
    from .paths import self_env

    # This process is created by the host bridge/outer worker, never by the
    # copied shim inside agent containment. setsid survives round group teardown.
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
            if alive(value["owner"]) and value.get("code_version") == version and time.time() - value["heartbeat"] < 10:
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
                    if code_version() != version:
                        stop.set()  # Let the next new-launch check load current code.
                        return
                    durable(
                        budget.directory / "usage-supervisor.json",
                        dict(owner=owner, heartbeat=time.time(), code_version=version),
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

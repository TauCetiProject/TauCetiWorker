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
import time
from pathlib import Path

from .budget import Budget, bridge_owner_alive
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


def invocation_alive(directory, session, *, legacy_owner=True):
    value = read_lifecycle(directory, session["id"])
    if value is None:
        return legacy_owner and bridge_owner_alive(session.get("owner"))
    return alive(value["shim"]) or bool(value.get("native") and alive(value["native"]))


def registration(invocation, ticket, bridge, shim, *, enabled, owner, parent, deadline=None):
    process = resolve(shim)
    if process is None:
        raise ValueError("admission requester stopped before identity was recorded")
    return dict(
        version=1,
        id=invocation,
        admission_id=ticket,
        bridge=str(bridge),
        shim=process,
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
    path = Path(directory) / "round-exits" / round_key(owner)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    durable(path, dict(owner=owner, kind=kind, returncode=returncode, at=time.time()))


def tick(budget, cache=None):
    """One restart-safe recovery pass. All writes are additive evidence."""
    # Admission and its exact launch identity are one durable ledger event.
    # Rebuild the sidecar if the bridge died between that event and its reply.
    with budget.locked():
        sessions = budget.replay()["sessions"]
    for session in sessions.values():
        if session.get("lifecycle"):
            register(budget.directory, session["lifecycle"])
    for path in (budget.directory / "lifecycles").glob("*.json"):
        try:
            recover_invocation(budget, path, cache=cache)
        except Exception as error:
            print(f"{path.stem}: usage recovery retained for retry: {type(error).__name__}: {error}", flush=True)


def recovery_mode(directory):
    try:
        mode = (Path(directory) / "usage-recovery-mode").read_text().strip()
    except FileNotFoundError:
        mode = "auto"
    if mode not in {"auto", "record-only"}:
        raise ValueError("usage-recovery-mode must contain auto or record-only")
    return mode


def recover_invocation(budget, path, *, cache=None):
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
    if (
        value["phase"] == "admitted"
        and launch.exists()
        and alive(value["shim"])
        and session_usage(budget.directory, value)
    ):
        message = safe_message(launch)
        if message.get("id") != invocation:
            raise ValueError("native launch invocation mismatch")
        native = resolve(message.get("native"))
        if native is not None and alive(native):
            value.update(phase="permitted", native=native, gate=message["native"])
            event("launch-permitted")
            durable(path, value)  # Identity must survive before the gate can exec.
    if value["phase"] == "permitted" and alive(value["shim"]) and alive(value["native"]):
        permit = outbox / (invocation + ".permit")
        if not permit.exists():
            durable(permit, dict(id=invocation, gate=value["gate"]))
    round_end = budget.directory / "round-exits" / round_key(value.get("owner"))
    if round_end.exists():
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
        event("parent-death")
    stopped = not alive(value["shim"]) and not (value.get("native") and alive(value["native"]))
    if stopped:
        event("stopped")
        if value["phase"] == "admitted":
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
        usage_path(budget.directory, invocation, value["bridge"]),
        usage_path(budget.directory, invocation),
    ]
    evidence_paths += [p.with_suffix(".jsonl") for p in evidence_paths[-2:]]
    stamp = []
    for evidence_path in evidence_paths:
        try:
            info = evidence_path.stat()
            stamp.append((info.st_ino, info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            stamp.append(None)
    if cache is not None and cache.get(invocation) == stamp:
        return
    if value["phase"] == "no-inference" or (inbox / (invocation + ".exec-failed")).exists():
        budget.settle_receipt(dict(id=invocation, totals={invocation: "0"}, outcome="launch-failed"))
    elif receipt_path.exists():
        receipt = safe_message(receipt_path)
        if receipt.get("id") != invocation:
            raise ValueError("native receipt invocation mismatch")
        budget.settle_receipt(receipt, note="usage supervisor: durable native receipt")
    budget.recover_usage(invocation, note="usage supervisor: stopped invocation")
    # Missing evidence also retains the admission reserve. Do not keep
    # appending unresolved events on every supervisor pass.
    with budget.locked():
        session = budget.replay()["sessions"].get(invocation)
    if session and session["status"] == "active":
        budget.settle(invocation, None, "stopped; missing usage evidence")
    if cache is not None:
        cache[invocation] = stamp


def ensure_supervisor(directory):
    """Called by outer workers and before contexts; restarting replays saved evidence."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready = directory / "usage-supervisor.json"
    try:
        value = safe_message(ready)
        if alive(value["owner"]) and time.time() - value["heartbeat"] < 10:
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
            if alive(value["owner"]) and time.time() - value["heartbeat"] < 10:
                # Reap duplicate starters which lost the singleton lock.
                import threading

                threading.Thread(target=process.wait, daemon=True).start()
                return
        except (OSError, ValueError, KeyError):
            pass
        if process.poll() not in (None, 0):
            raise RuntimeError("usage supervisor failed to start; see usage-supervisor.log")
        time.sleep(0.05)
    raise RuntimeError("usage supervisor unavailable; no inference admitted")


def run(directory):
    budget = Budget(Path(directory))
    with (budget.directory / "usage-supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        cache = {}
        original_directory = budget.directory.stat().st_ino
        while budget.directory.exists() and budget.directory.stat().st_ino == original_directory:
            try:
                durable(budget.directory / "usage-supervisor.json", dict(owner=identity(), heartbeat=time.time()))
                tick(budget, cache=cache)
            except Exception as error:
                print(f"usage recovery retained for retry: {type(error).__name__}: {error}", flush=True)
            time.sleep(0.5)


if __name__ == "__main__":
    run(sys.argv[1])

#!/usr/bin/env python3
"""Standalone Claude shim, also copied into Bubble. Configuration lives beside it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

if __package__:
    from .usage_accounting import UsageJournal
    from .usage_lifecycle import durable, identity
else:
    from usage_accounting import UsageJournal
    from usage_lifecycle import durable, identity


def atomic(path, value):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def warning(message):
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def request(directory, action, *, wait=True, **fields):
    message_id = uuid.uuid4().hex
    request_path = directory / "inbox" / (message_id + ".request")
    reply_path = directory / "outbox" / (message_id + ".reply")
    atomic(request_path, dict(action=action, reply=wait, **fields))
    if not wait:
        return
    while not reply_path.exists():
        if time.time() - (directory / "outbox/heartbeat").stat().st_mtime > 15:
            raise RuntimeError("API accounting bridge unavailable; cost receipt retained")
        time.sleep(0.1)
    answer = json.loads(reply_path.read_text())
    request_path.unlink(missing_ok=True)
    if answer.get("error"):
        raise RuntimeError(answer["error"])
    return answer


def api_environment(keyfile):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) or key in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE"}:
            env.pop(key)
    env["ANTHROPIC_API_KEY"] = Path(keyfile).read_text().strip()
    return env


def authenticate(command, env):
    result = subprocess.run(
        command + ["auth", "status"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True
    )
    try:
        authenticated = json.loads(result.stdout).get("authMethod") == "api_key"
    except (ValueError, AttributeError):
        authenticated = False
    if result.returncode or not authenticated:
        raise RuntimeError("Claude did not confirm API-key authentication; no session started")


def main():
    config = json.loads((Path(__file__).parent / "config.json").read_text())
    args = []
    for arg in sys.argv[1:]:
        if arg.startswith(("--model=", "--output-format=", "--resume=")):
            args.extend(arg.split("=", 1))
        else:
            args.append(arg)
    env = api_environment(config["key"])
    real = config["command"]
    if real[0] == "claude":
        search = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if Path(p) != Path(__file__).parent)
        executable = shutil.which("claude", path=search)
        if not executable:
            raise RuntimeError("native Claude executable not found")
        real = [executable, *real[1:]]
    if "--help" in args or "--version" in args or args[:2] == ["auth", "status"]:
        return subprocess.call(real + args, env=env)
    if "-p" not in args and "--print" not in args:
        raise RuntimeError("API worker shim supports print mode only")
    if any(arg.split("=", 1)[0] == "--max-budget-usd" for arg in args + real):
        raise RuntimeError("local grants never impose a session spending cap")
    # Authentication status is prompt-free. Do not let a stored subscription login pay for API work.
    try:
        authenticate(real, env)
    except (RuntimeError, OSError):
        request(Path(config["bridge"]), "preflight-failed", reason="Claude API-key authentication failed")
        raise
    model = args[args.index("--model") + 1] if "--model" in args else "default"
    invocation = uuid.uuid4().hex
    bridge = Path(config["bridge"])
    resume = None
    if "-c" in args or "--continue" in args:
        raise RuntimeError("API accounting requires an explicit --resume SESSION_ID instead of --continue")
    for flag in ("-r", "--resume"):
        if flag in args:
            index = args.index(flag)
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise RuntimeError("API accounting requires an explicit provider session ID")
            resume = args[index + 1]
    previous_reason = None
    while True:
        admission = request(bridge, "admit", id=invocation, model=model, resume=resume, process=identity())
        if admission["allowed"]:
            invocation = admission["id"]
            break
        if admission["reason"] != previous_reason:
            warning("tauceti-local-admission: " + admission["reason"])
            previous_reason = admission["reason"]
        return 75  # Funding waits belong to the outer loop, outside the round timeout.
    output_format = "text"
    if "--output-format" in args:
        index = args.index("--output-format")
        output_format = args[index + 1]
        args[index + 1] = "stream-json"
    else:
        args += ["--output-format", "stream-json"]
    journal = None
    journal_healthy = True
    requested_partials = "--include-partial-messages" in args
    if "--verbose" not in args:
        args.append("--verbose")
    totals = {}
    unknown_segments = set()
    final = None
    outcome = "launch-failed"
    rc = 1

    def checkpoint():
        # A native result can precede process exit by a long background-task
        # wait. Persist its cost before forwarding it, so round teardown can
        # recover the receipt even if SIGTERM prevents this shim's finally.
        # This does not settle an active session or release its reservation.
        receipt = dict(
            id=invocation,
            totals=None,
            provisional=True,
            lower_bound_totals=dict(totals),
            outcome="result observed; process has not exited",
        )
        try:
            atomic(bridge / "inbox" / (invocation + ".receipt"), receipt)
        except OSError as error:
            warning(f"tauceti budget: receipt checkpoint pending: {error}")

    process_exited = False
    try:
        usage_dir = config.get("usage_dir")
        if not isinstance(usage_dir, str) or not Path(usage_dir).is_absolute():
            raise ValueError("API accounting requires an absolute durable usage_dir")
        journal = UsageJournal(usage_dir, invocation, model=model)
        if not requested_partials:
            args.append("--include-partial-messages")
        gate = [sys.executable, str(Path(__file__).with_name("usage_lifecycle.py")), str(bridge), invocation]
        proc = subprocess.Popen(
            gate + real + args, env=env, stdout=subprocess.PIPE, text=True, errors="replace", bufsize=1
        )
        outcome = "started"
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    if output_format == "stream-json":
                        try:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                        except (OSError, ValueError):
                            pass
                    continue
                if journal is not None:
                    try:
                        journal.observe(event)
                    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as error:
                        journal_healthy = False
                        warning(f"tauceti usage checkpoint pending: {type(error).__name__}")
                if final is not None and (
                    event.get("type") in {"assistant", "stream_event", "user"}
                    or event.get("type") == "system"
                    and event.get("subtype") == "api_retry"
                ):
                    # Further inference after a result invalidates its complete
                    # cost until the next result supplies updated totals.
                    segment = event.get("session_id") or final.get("session_id") or invocation
                    unknown_segments.add(segment if isinstance(segment, str) else invocation)
                    checkpoint()
                if event.get("type") == "result":
                    final = event
                    segment = event.get("session_id") or invocation
                    if not isinstance(segment, str):
                        segment = invocation
                    value = event.get("total_cost_usd")
                    cost_known = False
                    if value is not None:
                        try:
                            number = Decimal(str(value))
                            cost_known = number.is_finite() and number >= 0
                            if event.get("subtype") == "error_during_execution" and number == 0:
                                cost_known = False
                            if cost_known:
                                totals[segment] = str(number)
                        except ArithmeticError:
                            pass
                    if cost_known:
                        unknown_segments.discard(segment)
                    else:
                        unknown_segments.add(segment)
                    checkpoint()
                if event.get("type") == "system" and event.get("subtype") == "api_retry":
                    if event.get("error_status") == 429:
                        try:
                            delay = max(5, float(event.get("retry_delay_ms") or 5000) / 1000)
                            request(bridge, "cooldown", id=invocation, model=model, seconds=delay, wait=False)
                        except (OSError, RuntimeError, ValueError, TypeError, OverflowError):
                            pass  # Accounting failure must not interrupt the already admitted session.
                if output_format == "stream-json" and (requested_partials or event.get("type") != "stream_event"):
                    try:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass  # Keep draining Claude even if the transcript consumer disappeared.
        finally:
            proc.stdout.close()
        rc = proc.wait()
        process_exited = True
        outcome = f"exit:{rc}"
        exec_failed = (bridge / "inbox" / (invocation + ".exec-failed")).exists()
        kind = (
            "failed-launch" if exec_failed else "successful-exit" if rc == 0 else "signal" if rc < 0 else "failed-exit"
        )
        try:
            launch_identity = json.loads((bridge / "inbox" / (invocation + ".launch")).read_text()).get("native")
            durable(
                bridge / "inbox" / (invocation + ".terminal"),
                dict(id=invocation, kind=kind, returncode=rc, native=launch_identity),
            )
        except (OSError, ValueError) as error:
            warning(f"tauceti lifecycle terminal evidence pending: {type(error).__name__}")
        if exec_failed:
            outcome = "launch-failed"
            totals = {invocation: "0"}
            unknown_segments.clear()
        try:
            if output_format == "json" and final is not None:
                print(json.dumps(final))
            elif output_format == "text" and final is not None:
                print(final.get("result", ""))
        except (OSError, ValueError):
            pass
    except Exception as error:
        if outcome == "launch-failed":
            warning(
                f"tauceti: pre-launch accounting/native executable unavailable; no inference started ({type(error).__name__}: {error})"
            )
            totals = {invocation: "0"}
            unknown_segments.clear()
            rc = 75
        else:
            raise
    finally:
        if journal is not None:
            try:
                journal.flush(closed=process_exited and rc == 0 and final is not None and journal_healthy)
            except OSError as error:
                warning(f"tauceti usage checkpoint pending: {type(error).__name__}")
        authoritative = outcome == "launch-failed" or process_exited and rc == 0
        receipt = dict(
            id=invocation,
            totals=totals if totals and not unknown_segments and authoritative else None,
            provisional=not authoritative,
            lower_bound_totals=dict(totals),
            outcome=outcome,
            **({"finished": time.time()} if process_exited else {}),
        )
        try:
            atomic(bridge / "inbox" / (invocation + ".receipt"), receipt)
        except OSError as exc:
            # A completed native session keeps its result and exit status. The
            # admission reservation/checkpoint remains until costs are recovered;
            # the live bridge may still settle from the request below.
            warning(f"tauceti budget: final receipt pending: {exc}")
        try:
            request(bridge, "settle", **receipt)
        except (OSError, RuntimeError) as exc:
            warning(f"tauceti budget: settlement pending: {exc}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        warning(f"tauceti-local-admission: {error}")
        sys.exit(75)

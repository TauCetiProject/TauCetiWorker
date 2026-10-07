#!/usr/bin/env python3
"""Standalone Claude shim, also copied into Bubble. Configuration lives beside it."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path


def atomic(path, value):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def request(directory, action, **fields):
    message_id = uuid.uuid4().hex
    request_path = directory / (message_id + ".request")
    reply_path = directory / (message_id + ".reply")
    atomic(request_path, dict(action=action, **fields))
    while not reply_path.exists():
        if time.time() - (directory / "heartbeat").stat().st_mtime > 15:
            raise RuntimeError("API accounting bridge unavailable; cost receipt retained")
        time.sleep(0.1)
    answer = json.loads(reply_path.read_text())
    request_path.unlink(missing_ok=True)
    reply_path.unlink(missing_ok=True)
    if answer.get("error"):
        raise RuntimeError(answer["error"])
    return answer


def main():
    config = json.loads((Path(__file__).parent / "config.json").read_text())
    args = sys.argv[1:]
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) or key in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE"}:
            env.pop(key)
    env["ANTHROPIC_API_KEY"] = Path(config["key"]).read_text().strip()
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
    authentication = subprocess.run(
        real + ["auth", "status"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True
    )
    try:
        authenticated = json.loads(authentication.stdout).get("authMethod") == "api_key"
    except (ValueError, AttributeError):
        authenticated = False
    if authentication.returncode or not authenticated:
        raise RuntimeError("Claude did not confirm API-key authentication; no session started")
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
        admission = request(bridge, "admit", id=invocation, model=model, resume=resume)
        if admission["allowed"]:
            break
        if admission["reason"] != previous_reason:
            print("tauceti budget: " + admission["reason"], file=sys.stderr)
            previous_reason = admission["reason"]
        if not config["wait"]:
            return 75
        time.sleep(2 + random.random())
    output_format = "text"
    if "--output-format" in args:
        index = args.index("--output-format")
        output_format = args[index + 1]
        args[index + 1] = "stream-json"
    else:
        args += ["--output-format", "stream-json"]
    if "--verbose" not in args:
        args.append("--verbose")
    totals = {}
    final = None
    cost_known = False
    outcome = "launch-failed"
    rc = 1
    try:
        proc = subprocess.Popen(real + args, env=env, stdout=subprocess.PIPE, text=True, errors="replace", bufsize=1)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if output_format == "stream-json":
                    sys.stdout.write(line)
                    sys.stdout.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result":
                    final = event
                    value = event.get("total_cost_usd")
                    if value is not None:
                        number = Decimal(str(value))
                        cost_known = number.is_finite() and number >= 0
                        if event.get("subtype") == "error_during_execution" and number == 0:
                            cost_known = False
                        if cost_known:
                            totals[event.get("session_id") or invocation] = str(number)
                    else:
                        cost_known = False
                if event.get("type") == "system" and event.get("subtype") == "api_retry":
                    if event.get("error_status") == 429:
                        delay = max(5, float(event.get("retry_delay_ms") or 5000) / 1000)
                        try:
                            request(bridge, "cooldown", model=model, seconds=delay)
                        except (OSError, RuntimeError):
                            pass  # Accounting failure must not interrupt the already admitted session.
        finally:
            proc.stdout.close()
        rc = proc.wait()
        outcome = f"exit:{rc}"
        if output_format == "json" and final is not None:
            print(json.dumps(final))
        elif output_format == "text" and final is not None:
            print(final.get("result", ""))
    finally:
        receipt = dict(id=invocation, totals=totals if cost_known else None, outcome=outcome)
        atomic(bridge / (invocation + ".receipt"), receipt)
        try:
            request(bridge, "settle", **receipt)
        except (OSError, RuntimeError) as exc:
            print(f"tauceti budget: settlement pending: {exc}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"tauceti Claude API: {error}", file=sys.stderr)
        sys.exit(75)

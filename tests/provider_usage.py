#!/usr/bin/env python3
"""Kiro ACP and OpenRouter usage telemetry stay prompt-free and precision-safe."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

usage = tc.usage
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")


# Exercise the real newline-delimited ACP transport with a fake executable. It answers only
# initialize, session/new, and the private usage extension; no session/prompt is accepted or sent.
with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    capture = root / "requests.jsonl"
    executable = root / "kiro-cli"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

capture = os.environ["KIRO_CAPTURE"]
for line in sys.stdin:
    request = json.loads(line)
    with open(capture, "a", encoding="utf-8") as out:
        out.write(json.dumps(request) + "\\n")
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "fs/read_text_file", "params": {}}), flush=True)
        result = {"sessionId": "usage-session"}
    elif method == "_kiro.dev/commands/execute":
        result = {
            "success": True,
            "message": "Plan: KIRO FREE | 1 usage breakdowns",
            "data": {
                "planName": "KIRO FREE",
                "billingCycleReset": "2026-09-01",
                "overagesEnabled": False,
                "usageBreakdowns": [{
                    "resourceType": "CREDIT",
                    "displayName": "Credits",
                    "used": 0.9,
                    "limit": 50.0,
                    "percentage": 1,
                    "hasLimit": True
                }]
            }
        }
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "error": {"message": method}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    if method == "_kiro.dev/commands/execute":
        break
"""
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env.update(PATH=f"{root}:{env.get('PATH', '')}", KIRO_CAPTURE=str(capture), HOME=str(root / "home"))
    result = usage.kiro_usage(burn_rate=2.4, timeout=5, env=env)
    requests = [json.loads(line) for line in capture.read_text().splitlines()]
    methods = [request["method"] for request in requests if "method" in request]

    check(
        "ACP sends only transport/setup/usage methods",
        methods,
        [
            "initialize",
            "session/new",
            "_kiro.dev/commands/execute",
        ],
    )
    check("ACP sends no model prompt", "session/prompt" in methods, False)
    reverse_reply = next(request for request in requests if request.get("id") == 99 and "error" in request)
    check("ACP refuses reverse agent requests", reverse_reply["error"]["code"], -32601)
    check(
        "Kiro usage command is the required tagged object",
        next(request for request in requests if request.get("method") == "_kiro.dev/commands/execute")["params"][
            "command"
        ],
        {"command": "usage", "args": {}},
    )
    credit = result["usage_breakdowns"][0]
    check("fractional Kiro used value is preserved", credit["used"], 0.9)
    check("fractional Kiro limit is preserved", credit["limit"], 50.0)
    check("rounded provider percentage is retained separately", credit["provider_percentage"], 1.0)
    check("actual percentage is recomputed", credit["percentage"], 1.8)
    check("burn-rate estimate is observability only", credit["estimated_rounds_remaining"], 49.1 / 2.4)


# OpenRouter's inference-key and management-key endpoints expose complementary scopes.
calls = []
saved_http = usage._http_json


def fake_http(url, key, *, timeout):
    calls.append((url, key, timeout))
    if url.endswith("/key"):
        return {
            "label": "worker",
            "usage": 3.25,
            "limit": 20.0,
            "limit_remaining": 16.5,
            "limit_reset": "monthly",
        }
    return {"total_credits": 100.5, "total_usage": 40.25}


usage._http_json = fake_http
try:
    result = usage.openrouter_usage(
        burn_rate=2.0,
        timeout=7,
        env={"OPENROUTER_API_KEY": "inference", "OPENROUTER_MANAGEMENT_KEY": "management"},
    )
    management_only = usage.openrouter_usage(env={"OPENROUTER_MANAGEMENT_KEY": "management"})
    key_only = usage.openrouter_usage(env={"OPENROUTER_API_KEY": "inference"})
finally:
    usage._http_json = saved_http

check(
    "OpenRouter key endpoint queried with inference key", calls[0], ("https://openrouter.ai/api/v1/key", "inference", 7)
)
check(
    "OpenRouter credits endpoint queried with management key",
    calls[1],
    ("https://openrouter.ai/api/v1/credits", "management", 7),
)
check("direct key remaining wins over subtraction", result["key"]["remaining"], 16.5)
check("key burn-rate estimate", result["key"]["estimated_rounds_remaining"], 8.25)
check("account fractional remaining", result["account"]["remaining"], 60.25)
check("account burn-rate estimate", result["account"]["estimated_rounds_remaining"], 30.125)

check(
    "missing management scope is explicit", "OPENROUTER_MANAGEMENT_KEY" in key_only["account_unavailable_reason"], True
)

missing = usage.usage_snapshot(providers=["openrouter"], env={})
check("missing OpenRouter credentials are structured", missing["openrouter"]["ok"], False)
check("management key is accepted without an inference key", management_only["account"] is not None, True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

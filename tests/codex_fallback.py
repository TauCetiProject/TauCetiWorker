#!/usr/bin/env python3
"""Default Codex authoring probes Sol safely, caches access, and runs no authoring prompt itself."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

SOL = "gpt-6-sol"
LUNA = "gpt-6-luna"
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def unavailable(status=400, message=None):
    message = message or f"The '{SOL}' model is not supported when using Codex with a ChatGPT account."
    payload = {"status": status, "error": {"type": "invalid_request_error", "message": message}}
    return json.dumps({"type": "turn.failed", "error": {"message": json.dumps(payload)}})


OK = SimpleNamespace(returncode=0, stdout='{"type":"turn.completed","usage":{}}\n', stderr="")
UNAVAILABLE = SimpleNamespace(returncode=1, stdout=unavailable() + "\n", stderr="")
ORDINARY = SimpleNamespace(
    returncode=1,
    stdout=json.dumps({"type": "turn.failed", "error": {"message": "ordinary agent failure"}}) + "\n",
    stderr="",
)
CONTEXT = SimpleNamespace(
    returncode=1,
    stdout=unavailable(400, "This request exceeds the context window.") + "\n",
    stderr="",
)
TRANSIENT = SimpleNamespace(returncode=1, stdout=unavailable(500) + "\n", stderr="")
RAW_FALSE_POSITIVE = SimpleNamespace(returncode=1, stdout="error: model not found\n", stderr="")


def run(sequence, *, repeat=False, explicit=False):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        cache = root / "cache"
        state = root / "state"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {"account_id": "acct-test", "access_token": "secret"}})
        )
        cfg = SimpleNamespace(home=home, quota_cache=cache, state=state)
        profile = tc.AuthoringProfile(
            provider="codex",
            model=SOL,
            effort="high",
            model_source="--author-model" if explicit else "repository default",
            effort_source="repository default",
            fallback_model=None if explicit else LUNA,
        )
        calls = []
        outcomes = list(sequence)
        saved_run = tc.agents.subprocess.run
        saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "must-not-reach-subscription-probe"

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return outcomes.pop(0)

        tc.agents.subprocess.run = fake_run
        try:
            try:
                selected = tc.resolve_codex_model_access(cfg, profile)
                error = None
            except tc.NoProgress as exc:
                selected, error = None, str(exc)
            if repeat and selected is not None:
                selected_again = tc.resolve_codex_model_access(cfg, profile)
            else:
                selected_again = None
        finally:
            tc.agents.subprocess.run = saved_run
            if saved_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = saved_key
        return selected, selected_again, error, calls, outcomes


selected, again, error, calls, remaining = run([OK], repeat=True)
check("successful Sol probe selects Sol", (selected.model, error), (SOL, None))
check("successful Sol access is cached", (again.model, len(calls), len(remaining)), (SOL, 1, 0))
argv, kwargs = calls[0]
check("probe uses the requested Sol model", argv[argv.index("--model") + 1], SOL)
check("probe is JSON + read-only + ephemeral", all(x in argv for x in ("--json", "read-only", "--ephemeral")), True)
check("probe ignores user config and rules", all(x in argv for x in ("--ignore-user-config", "--ignore-rules")), True)
check("probe prompt is trivial, not an authoring prompt", argv[-1], "Reply with exactly OK. Do not use tools.")
check("probe closes stdin", kwargs.get("stdin"), subprocess.DEVNULL)
check("probe strips API-key billing", "OPENAI_API_KEY" in kwargs.get("env", {}), False)

selected, again, error, calls, remaining = run([UNAVAILABLE, UNAVAILABLE], repeat=True)
check("two confirmed entitlement misses select Luna", (selected.model, error), (LUNA, None))
check("Luna decision is cached without a third request", (again.model, len(calls), len(remaining)), (LUNA, 2, 0))
check("both confirmations probe Sol only", [c[0][c[0].index("--model") + 1] for c in calls], [SOL, SOL])

selected, _, error, calls, _ = run([UNAVAILABLE, OK])
check("a successful confirmation keeps Sol", (selected.model, len(calls), error), (SOL, 2, None))

for name, outcome in (
    ("ordinary failure", ORDINARY),
    ("context 400", CONTEXT),
    ("model-looking 5xx", TRANSIENT),
    ("raw transcript false positive", RAW_FALSE_POSITIVE),
):
    selected, _, error, calls, _ = run([outcome])
    check(f"{name} does not downgrade", (selected, len(calls), error is not None), (None, 1, True))

selected, _, error, calls, remaining = run([], explicit=True)
check("explicit model bypasses probe and fallback", (selected.model, len(calls), error), (SOL, 0, None))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

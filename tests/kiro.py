#!/usr/bin/env python3
"""Kiro model selection is exact, entitlement-checked, and API-key deterministic."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

agents = tc.agents
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")


check(
    "model ids are extracted from Kiro's nested JSON",
    agents._kiro_model_ids({"data": {"models": [{"modelId": "gpt-5.6-sol"}, {"model_id": "claude-opus-5"}]}}),
    {"gpt-5.6-sol", "claude-opus-5"},
)

saved_run = agents.subprocess.run
calls = []


def fake_run(argv, **kwargs):
    calls.append((argv, kwargs))
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout=json.dumps({"models": [{"id": "gpt-5.6-sol"}, {"id": "claude-opus-5"}]}),
        stderr="",
    )


agents.subprocess.run = fake_run
try:
    with tempfile.TemporaryDirectory() as root:
        cfg = SimpleNamespace(state=Path(root))
        sol = tc.resolve_authoring_profile("kiro")
        checked = agents.validate_kiro_model_access(cfg, sol)
        check("entitled exact Sol survives validation", checked.model, "gpt-5.6-sol")
        check(
            "access check sends no prompt",
            calls[-1][0],
            ["kiro-cli", "chat", "--list-models", "--format", "json"],
        )

        unavailable = tc.resolve_authoring_profile("kiro", cli_model="gpt-9-auto-magic")
        try:
            agents.validate_kiro_model_access(cfg, unavailable)
            rejected = False
            message = ""
        except tc.NoProgress as error:
            rejected = True
            message = str(error)
        check("unavailable exact model is rejected", rejected, True)
        check("rejection explicitly refuses Auto fallback", "will not fall back to Auto" in message, True)
finally:
    agents.subprocess.run = saved_run

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    env = tc.kiro_process_env(
        {
            "HOME": str(root / "operator"),
            "KIRO_API_KEY": "key",
            "XDG_DATA_HOME": str(root / "browser-login"),
            "ANTHROPIC_API_KEY": "must-not-leak",
            "OPENAI_API_KEY": "must-not-leak",
            "OPENROUTER_API_KEY": "must-not-leak",
            "OPENROUTER_MANAGEMENT_KEY": "must-not-leak",
        },
        private_root=root / "private",
    )
    check("API key gets a private KIRO_HOME", env["KIRO_HOME"], str(root / "private" / "home"))
    check("API key cannot be shadowed by browser auth", env["XDG_DATA_HOME"], str(root / "private" / "data"))
    check(
        "Kiro receives no unrelated provider keys",
        any(key in env for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")),
        False,
    )
    blank_env = tc.kiro_process_env({"HOME": str(root), "KIRO_API_KEY": "   "})
    check("blank Kiro API key is removed", "KIRO_API_KEY" in blank_env, False)

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    home, rounddir = root / "home", root / "round"
    source = tc.kiro_data_dir(home) / "data.sqlite3"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO auth_kv VALUES ('login', 'browser')")
    rounddir.mkdir()
    saved_key = os.environ.get("KIRO_API_KEY")
    try:
        os.environ["KIRO_API_KEY"] = "  api-key  "
        agents._stage_kiro_credentials(SimpleNamespace(home=home), rounddir)
        check("Bubble strips the staged API key", (rounddir / "kiro.key").read_text(), "api-key")
        check("API-key Bubble does not also stage browser auth", (rounddir / "kiro-auth.sqlite3").exists(), False)

        os.environ["KIRO_API_KEY"] = "   "
        (rounddir / "kiro.key").unlink()
        agents._stage_kiro_credentials(SimpleNamespace(home=home), rounddir)
        check("blank API key falls back to browser snapshot", (rounddir / "kiro-auth.sqlite3").exists(), True)
        check("browser snapshot is private", oct((rounddir / "kiro-auth.sqlite3").stat().st_mode & 0o777), "0o600")
    finally:
        if saved_key is None:
            os.environ.pop("KIRO_API_KEY", None)
        else:
            os.environ["KIRO_API_KEY"] = saved_key

saved_review_model = os.environ.get("TAUCETI_REVIEW_KIRO_MODEL")
try:
    os.environ["TAUCETI_REVIEW_KIRO_MODEL"] = "   "
    check("blank review override retains exact Sol default", agents._kiro_review_model("kiro"), "gpt-5.6-sol")
    os.environ["TAUCETI_REVIEW_KIRO_MODEL"] = "auto"
    try:
        agents._kiro_review_model("kiro")
        review_auto_rejected = False
    except tc.Die:
        review_auto_rejected = True
    check("Kiro review cannot select Auto", review_auto_rejected, True)
finally:
    if saved_review_model is None:
        os.environ.pop("TAUCETI_REVIEW_KIRO_MODEL", None)
    else:
        os.environ["TAUCETI_REVIEW_KIRO_MODEL"] = saved_review_model

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

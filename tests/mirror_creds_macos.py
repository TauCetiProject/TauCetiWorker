#!/usr/bin/env python3
"""mirror_creds on macOS must skip the CLAUDE half only, and still mirror CODEX.

macOS keeps Claude Code's credentials in the login Keychain, so there is no source FILE to mirror and
the keychain-first pacer handles it. Codex is different on every platform: it keeps auth.json, and
isolate_home writes its source marker on macOS too, explicitly so this function can re-mirror it.

Returning early for the whole function (the behaviour this test pins against) left an isolated macOS
worker pinned to whatever account was seeded: never re-synced after an operator account switch, and
still holding the operator's real refresh token, which the once-only seed copies verbatim and which
stripping exists to remove.

The platform is simulated, so this runs and means the same thing on every host — the macOS-only path
was previously reachable by no test on Linux CI, which is how the gap survived. Exit 0 = pass.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def setup(tmp):
    """An isolated home with BOTH source markers, as isolate_home leaves it on macOS."""
    real, iso = tmp / "real", tmp / "iso"
    src_claude, iso_claude = real / ".claude", iso / ".claude"
    src_codex, iso_codex = real / ".codex", iso / ".codex"
    for d in (src_claude, iso_claude, src_codex, iso_codex):
        d.mkdir(parents=True)
    (iso_claude / ".tauceti-creds-source").write_text(str(src_claude))
    (iso_codex / ".tauceti-creds-source").write_text(str(src_codex))
    cfg = types.SimpleNamespace(home=iso)
    return cfg, src_claude, iso_claude, src_codex / "auth.json", iso_codex / "auth.json"


def codex_block(tok, rt="R"):
    return {"tokens": {"access_token": tok, "refresh_token": rt, "id_token": "I"}, "last_refresh": "x"}


_saved_platform = tc.quota.sys.platform
_saved_env = {k: __import__("os").environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
import os

for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
    os.environ.pop(k, None)  # the dirs must resolve from cfg.home, not the runner's own environment

tmp = Path(tempfile.mkdtemp(prefix="mirror-macos-"))
try:
    tc.quota.sys.platform = "darwin"

    # 1) The operator switches accounts. An isolated macOS worker must follow, not stay on the seed.
    cfg, src_claude, iso_claude, sx, dx = setup(tmp)
    sx.write_text(json.dumps(codex_block("TOKEN-NEW-ACCOUNT")))
    dx.write_text(json.dumps(codex_block("TOKEN-SEEDED-ACCOUNT")))
    tc.mirror_creds(cfg)
    out = json.loads(dx.read_text())["tokens"]
    check("codex IS mirrored on macOS (follows an account switch)", out["access_token"], "TOKEN-NEW-ACCOUNT")

    # 2) ...and the operator's real refresh token is stripped in the process. The once-only isolate_home
    #    seed copies auth.json verbatim, so before this fix a macOS worker held it indefinitely.
    check("codex: real refresh token replaced by the placeholder", out.get("refresh_token"), tc.CODEX_RT_PLACEHOLDER)
    check("codex: operator's real refresh token is not present", out.get("refresh_token") == "R", False)

    # 3) Claude is still skipped on macOS: the Keychain is the store, and there is no source file. A
    #    stray .credentials.json in the source dir must NOT be mirrored (that path is Linux-only).
    (src_claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "SHOULD-NOT-BE-COPIED", "refreshToken": "R"}})
    )
    tc.mirror_creds(cfg)
    check("claude is NOT mirrored on macOS", (iso_claude / ".credentials.json").exists(), False)

    # 4) Non-isolated (no marker) stays a no-op on macOS, as everywhere: the worker reads the live file.
    plain = tmp / "plain"
    (plain / ".codex").mkdir(parents=True)
    (plain / ".codex" / "auth.json").write_text(json.dumps(codex_block("UNTOUCHED")))
    tc.mirror_creds(types.SimpleNamespace(home=plain))
    check(
        "no marker -> untouched on macOS",
        json.loads((plain / ".codex" / "auth.json").read_text())["tokens"]["refresh_token"],
        "R",
    )
finally:
    tc.quota.sys.platform = _saved_platform
    for k, v in _saved_env.items():
        if v is not None:
            os.environ[k] = v
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""mirror_creds: the worker mirrors the operator's externally-refreshed creds into the isolated home
WITHOUT the operator's real refresh token (so nothing in the worker can rotate the single-use token), and
only when the source is fresher. Claude: the refresh field is dropped; codex: it is replaced by a constant
placeholder (codex-cli >=0.139 won't parse auth.json without the field, but never uses it given a valid
access token). Never refreshes, never blanks a good copy on a torn source read, and follows an operator
account switch even when the new account's token carries an earlier expiry."""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

if sys.platform == "darwin":
    print("[SKIP] mirror_creds is a Linux-only behavior (macOS uses the Keychain)")
    sys.exit(0)

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def claude_block(tok, exp, rt="R"):
    return {"claudeAiOauth": {"accessToken": tok, "expiresAt": exp, "refreshToken": rt, "scopes": ["s"]}}


def codex_block(tok, rt="R"):
    return {"tokens": {"access_token": tok, "refresh_token": rt, "id_token": "I"}, "last_refresh": "x"}


def setup(tmp):
    """Build an isolated home with markers pointing at a real source dir; return (cfg, src_claude, src_codex,
    iso_claude_creds, iso_codex_creds)."""
    real, iso = tmp / "real", tmp / "iso"
    src_claude, iso_claude = real / ".claude", iso / ".claude"
    src_codex, iso_codex = real / ".codex", iso / ".codex"
    for d in (src_claude, iso_claude, src_codex, iso_codex):
        d.mkdir(parents=True)
    (iso_claude / ".tauceti-creds-source").write_text(str(src_claude))
    (iso_codex / ".tauceti-creds-source").write_text(str(src_codex))
    os.environ["CLAUDE_CONFIG_DIR"] = str(iso_claude)  # mirror_creds reads claude_dir(cfg.home) = this
    cfg = types.SimpleNamespace(home=iso)
    return (
        cfg,
        src_claude / ".credentials.json",
        src_codex / "auth.json",
        iso_claude / ".credentials.json",
        iso_codex / "auth.json",
    )


# 1) Fresh source (newer token, later expiry) over a stale dest → copies; refresh token stripped.
tmp = Path(tempfile.mkdtemp())
try:
    cfg, sc, sx, dc, dx = setup(tmp)
    sc.write_text(json.dumps(claude_block("NEW", 200)))
    dc.write_text(json.dumps(claude_block("OLD", 100)))
    sx.write_text(json.dumps(codex_block("CNEW")))
    dx.write_text(json.dumps(codex_block("COLD")))
    tc.mirror_creds(cfg)
    out_c = json.loads(dc.read_text())["claudeAiOauth"]
    check("claude: stale dest gets the fresh access token", out_c["accessToken"], "NEW")
    check("claude: refresh token stripped from the copy", "refreshToken" in out_c, False)
    check("claude: benign fields preserved", out_c.get("scopes"), ["s"])
    out_x = json.loads(dx.read_text())["tokens"]
    check("codex: stale dest gets the fresh access token", out_x["access_token"], "CNEW")
    # codex-cli >=0.139 won't parse an auth.json without `refresh_token`, so the field is present but
    # holds a constant PLACEHOLDER — never the operator's real token "R" (the worker can't rotate it).
    check("codex: refresh token replaced by the placeholder", out_x.get("refresh_token"), tc.CODEX_RT_PLACEHOLDER)
    check("codex: operator's real refresh token never copied", out_x.get("refresh_token") == "R", False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 2) Same access token but dest still carries a refresh token (the once-only isolate_home seed) → re-write
#    to strip it, even though the token is unchanged.
tmp = Path(tempfile.mkdtemp())
try:
    cfg, sc, sx, dc, dx = setup(tmp)
    sc.write_text(json.dumps(claude_block("SAME", 100)))
    dc.write_text(json.dumps(claude_block("SAME", 100)))  # identical, refresh token present
    sx.write_text(json.dumps(codex_block("CSAME")))
    dx.write_text(json.dumps(codex_block("CSAME")))
    tc.mirror_creds(cfg)
    check(
        "claude: unchanged token still gets stripped",
        "refreshToken" in json.loads(dc.read_text())["claudeAiOauth"],
        False,
    )
    check(
        "codex: unchanged token's real refresh replaced by placeholder",
        json.loads(dx.read_text())["tokens"].get("refresh_token"),
        tc.CODEX_RT_PLACEHOLDER,
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 3) Operator account SWITCH: the new account's token has an EARLIER expiry than the copy we hold (each
#    account refreshes independently, so absolute expiries are unrelated). The switch MUST be mirrored —
#    a stale expiry guard used to skip this, wedging the worker on the prior account until the two accounts'
#    expiries happened to cross. src is authoritative; trust the changed token.
tmp = Path(tempfile.mkdtemp())
try:
    cfg, sc, sx, dc, dx = setup(tmp)
    sc.write_text(json.dumps(claude_block("SWITCHED", 50)))  # different account, earlier expiry
    dc.write_text(json.dumps(claude_block("PRIOR", 100, rt=None)))
    tc.mirror_creds(cfg)
    out_c = json.loads(dc.read_text())["claudeAiOauth"]
    check("claude: account switch is mirrored despite earlier expiry", out_c["accessToken"], "SWITCHED")
    check("claude: switched copy still has no refresh token", "refreshToken" in out_c, False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 4) Unreadable/garbage source → skip; dest untouched (never blank a good copy).
tmp = Path(tempfile.mkdtemp())
try:
    cfg, sc, sx, dc, dx = setup(tmp)
    sc.write_text("{ this is not json")
    dc.write_text(json.dumps(claude_block("KEEP", 100, rt=None)))
    tc.mirror_creds(cfg)
    check(
        "claude: torn source read leaves the good dest in place",
        json.loads(dc.read_text())["claudeAiOauth"]["accessToken"],
        "KEEP",
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 5) Not isolated (no marker) → no-op even if a sibling file exists.
tmp = Path(tempfile.mkdtemp())
try:
    iso_claude = tmp / "iso" / ".claude"
    iso_claude.mkdir(parents=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(iso_claude)
    (iso_claude / ".credentials.json").write_text(json.dumps(claude_block("LIVE", 100)))
    tc.mirror_creds(types.SimpleNamespace(home=tmp / "iso"))
    check(
        "no marker ⇒ mirror is a no-op",
        json.loads((iso_claude / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"],
        "LIVE",
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)

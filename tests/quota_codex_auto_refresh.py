#!/usr/bin/env python3
"""With --auto-refresh, the worker renews the operator's Codex access token instead of stalling on 401.

The Codex twin of quota_auto_refresh.py, and it runs on macOS as well: Codex keeps its credential in a
file on every platform. The cases are the same boundary — a refresh token is single-use, so every path
where it might not be ours to spend (no opt-in, a placeholder-only mirror, an inspecting caller, a
credential store that is not the file) must leave it alone.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import oauth  # noqa: E402
from tauceti_worker.quota import CODEX_REFRESH_SKEW_S, CODEX_RT_PLACEHOLDER  # noqa: E402

fails = 0
DAY = 86400


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def jwt(expires_in):
    claims = json.dumps({"exp": int(time.time() + expires_in)}).encode()
    return f"header.{base64.urlsafe_b64encode(claims).decode().rstrip('=')}.signature"


# Both windows half elapsed and barely used, so the pace curve says "launchable" and the verdict under
# test is the credential, not the pacing.
USAGE_OK = {
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {"used_percent": 5, "limit_window_seconds": 18000, "reset_after_seconds": 9000},
        "secondary_window": {"used_percent": 5, "limit_window_seconds": 7 * DAY, "reset_after_seconds": 3 * DAY},
    }
}
EXPIRED = "codex token expired; refresh left to the operator"


def auth(access, refresh="operator-refresh"):
    return json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": access, "refresh_token": refresh}})


def tokens(path):
    return json.loads(path.read_text())["tokens"]


def setup(tmp, *, expires_in=60, refresh="operator-refresh", opt_in=True):
    """An isolated worker: it reads a MIRROR of the operator's auth.json, with a marker naming the
    original. Returns (quota, source path, mirror path, the access token both start with)."""
    src, dst = tmp / "real" / ".codex", tmp / "iso" / ".codex"
    for d in (src, dst):
        d.mkdir(parents=True)
    (dst / ".tauceti-creds-source").write_text(str(src))
    for key in ("CLAUDE_CONFIG_DIR", "TAUCETI_AUTO_REFRESH", "TAUCETI_REFRESH_MIRROR"):
        os.environ.pop(key, None)
    os.environ["CODEX_HOME"] = str(dst)
    if opt_in:
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    stale = jwt(expires_in)
    (src / "auth.json").write_text(auth(stale, refresh))
    (dst / "auth.json").write_text(auth(stale, CODEX_RT_PLACEHOLDER))
    cfg = types.SimpleNamespace(home=tmp / "iso", quota_cache=tmp / "cache")
    return tc.Quota(cfg), src / "auth.json", dst / "auth.json", stale


def rotates_to(access):
    return patch.object(oauth, "_post_json", return_value=(200, {"access_token": access, "refresh_token": "rt2"}))


def usage_seen(*answers):
    """Record the bearer token each usage read was made with; answer the scripted sequence, then 200."""
    seen, script = [], list(answers)

    def fake(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        return script.pop(0) if script else (200, USAGE_OK, None)

    return seen, patch.object(tc.quota, "_http_get_json", side_effect=fake)


# 1) A credential inside the skew is rotated at the SOURCE, and the fresh token reaches both the
#    isolated worker's mirror and the usage request made in the same call.
tmp = Path(tempfile.mkdtemp())
try:
    fresh = jwt(10 * DAY)
    quota, src, mirror, stale = setup(tmp, expires_in=CODEX_REFRESH_SKEW_S - 60)
    seen, usage = usage_seen()
    with rotates_to(fresh) as post, usage:
        prov = quota.codex(renew=True)
    check("a credential inside the skew is rotated", post.call_count, 1)
    check("the exchange spends the source's refresh token", post.call_args.args[1]["refresh_token"], "operator-refresh")
    check(
        "the rotation writes the operator's source file", tokens(src), {"access_token": fresh, "refresh_token": "rt2"}
    )
    check(
        "the isolated worker's mirror picks it up, placeholder kept",
        tokens(mirror),
        {"access_token": fresh, "refresh_token": CODEX_RT_PLACEHOLDER},
    )
    check("the markers sit beside the source", (src.parent / ".auth.json.refresh.last-success").exists(), True)
    check("...and not beside the mirror", list(mirror.parent.glob(".auth.json.refresh.*")), [])
    check("the same call reads usage with the fresh token", seen, [f"Bearer {fresh}"])
    check("the provider is usable again", prov.available, True)

    # The success marker binds a forced rotation too, so a 401 that arrives right after another worker
    # rotated does not retire the token that worker was just issued.
    seen, usage = usage_seen((401, {}, None))
    with patch.object(oauth, "_post_json") as post, usage:
        prov = quota.codex(refresh=True, renew=True)
    check("a 401 inside the success cooldown does not rotate again", post.call_count, 0)
    check("...and is reported as the credential it is", prov.error, EXPIRED)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 2) A credential outside the skew is left alone — and not even locked, since renewal is offered on
#    every loop poll.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp, expires_in=CODEX_REFRESH_SKEW_S + 3600)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.codex(renew=True)
    check("a live credential is not rotated", post.call_count, 0)
    check("no lock file is created for a live credential", list(src.parent.glob("*.lock")), [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 3) Renewal is opt-in per caller: an inspecting read never rotates, a selection about to launch does.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp, expires_in=-60)
    seen, usage = usage_seen((401, {}, None), (401, {}, None))
    with patch.object(oauth, "_post_json") as post, usage:
        plain = quota.codex()
        tc.Quota(quota.cfg).choose("codex", refresh=True)
    check("an inspecting read never rotates", post.call_count, 0)
    check("...even when the endpoint refuses the token", plain.error, EXPIRED)
    check("the credential file is untouched", tokens(src)["access_token"], stale)
    fresh = jwt(10 * DAY)
    with rotates_to(fresh) as post, usage:
        chosen, _snap = tc.Quota(quota.cfg).choose("codex", refresh=True, renew=True)
    check("choose(renew=True) reaches the codex credential", (post.call_count, chosen), (1, "codex"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 4) The Docker shape: the source is itself a placeholder-only mirror, maintained by the dedicated
#    single-writer refresher. There is nothing here the worker may spend.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp, refresh=CODEX_RT_PLACEHOLDER)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.codex(renew=True)
    check("a placeholder-only source is never rotated", post.call_count, 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 5) The default. Without the opt-in the worker never touches the operator's refresh token, even on a
#    renewing call with an expiring credential.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp, opt_in=False)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.codex(renew=True)
    check("without --auto-refresh nothing rotates", post.call_count, 0)
    check("the stale token is still used as-is", seen, [f"Bearer {stale}"])
    check("the credential file is untouched", tokens(src), {"access_token": stale, "refresh_token": "operator-refresh"})
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 6) Under a keyring or ephemeral store auth.json is not the credential codex uses, and a leftover file
#    may share its refresh token with the store that is.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp)
    (mirror.parent / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    with patch.object(oauth, "_post_json") as post:
        quota.codex(renew=True)
    check("a non-file credential store is never rotated", post.call_count, 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 7) The stored expiry says the token is live and the endpoint says otherwise. Believe the endpoint:
#    rotate once, then re-read rather than reporting a 401 we can fix.
tmp = Path(tempfile.mkdtemp())
try:
    rescued = jwt(10 * DAY)
    quota, src, mirror, stale = setup(tmp, expires_in=5 * DAY)
    seen, usage = usage_seen((401, {}, None))
    with rotates_to(rescued) as post, usage:
        prov = quota.codex(renew=True)
    check("a 401 forces exactly one rotation", post.call_count, 1)
    check("the retry uses the rotated token", seen, [f"Bearer {stale}", f"Bearer {rescued}"])
    check("the rescued read decides the verdict", prov.available, True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 8) A 401 that rotation cannot fix (the refresh token is dead too) is tried once and stays the
#    operator-facing hard block.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp, expires_in=5 * DAY)
    seen, usage = usage_seen((401, {}, None))
    with patch.object(oauth, "_post_json", return_value=(400, None)) as post, usage:
        prov = quota.codex(renew=True)
    check("an unrecoverable 401 is tried once", (post.call_count, len(seen)), (1, 1))
    check("...and stays a hard block with the existing error", (prov.available, prov.error), (False, EXPIRED))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)

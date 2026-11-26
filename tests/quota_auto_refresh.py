#!/usr/bin/env python3
"""With --auto-refresh, the worker renews its Claude access token instead of stalling on HTTP 401.

An unattended `tauceti work --loop` has nobody to re-run `claude` for it, so a token expiry ends the
run: every poll reads 401 and sleeps. An operator who has told us the credential file is exclusively
this worker's can opt into renewing it.

The opt-in is the point of most of these cases. A refresh token is single-use, so every path where the
token might not be ours — no opt-in, a stripped worker mirror, an inspecting caller — must leave it
alone, or the process that shares the file is left holding a credential the server has retired.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
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

if sys.platform == "darwin":
    print("[SKIP] auto-refresh is Linux-only (macOS keeps the credential in the login Keychain)")
    sys.exit(0)

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def iso(delta_s):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


# Both windows nearly elapsed and barely used, so the pace curve says "launchable" and the verdict under
# test is the credential, not the pacing.
USAGE_OK = {
    "five_hour": {"utilization": 5, "resets_at": iso(600)},
    "seven_day": {"utilization": 5, "resets_at": iso(3600)},
}


def creds(access, expires_at, refresh="operator-refresh"):
    block = {"accessToken": access, "expiresAt": expires_at, "scopes": ["s"]}
    if refresh is not None:
        block["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": block})


def setup(tmp, *, expired=True, refresh="operator-refresh", opt_in=True):
    """An isolated worker: it reads a MIRROR of the operator's credential, with a marker naming the
    original. Returns (quota, source credential path, mirror credential path)."""
    real, iso = tmp / "real", tmp / "iso"
    src, dst = real / ".claude", iso / ".claude"
    for d in (src, dst):
        d.mkdir(parents=True)
    (dst / ".tauceti-creds-source").write_text(str(src))
    os.environ["CLAUDE_CONFIG_DIR"] = str(dst)
    os.environ.pop("TAUCETI_AUTO_REFRESH", None)
    if opt_in:
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    expiry = (time.time() - 60 if expired else time.time() + 86400) * 1000
    (src / ".credentials.json").write_text(creds("stale-access", expiry, refresh))
    (dst / ".credentials.json").write_text(creds("stale-access", expiry, None))  # mirrors carry no token
    cfg = types.SimpleNamespace(home=iso, quota_cache=tmp / "cache")
    return tc.Quota(cfg), src / ".credentials.json", dst / ".credentials.json"


def rotates_to(access, expires_in=7 * 86400):
    """A successful exchange. The lifetime has to exceed any expiry already on disk, or the core rejects
    the response as regressing the token."""
    return patch.object(
        oauth,
        "_post_json",
        return_value=(200, {"access_token": access, "refresh_token": "rt2", "expires_in": expires_in}),
    )


def usage_seen():
    """Record the bearer token each usage read was made with, answering 200 every time."""
    seen = []

    def fake(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        return 200, USAGE_OK, None

    return seen, patch.object(tc.quota, "_http_get_json", side_effect=fake)


# 1) An expired credential is rotated at the SOURCE, and the fresh token reaches both the isolated
#    worker's mirror and the usage request made in the same call.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp)
    seen, usage = usage_seen()
    with rotates_to("fresh-access") as post, usage:
        prov = quota.claude(renew=True)
    check("an expired credential is rotated", post.call_count, 1)
    check(
        "the rotation writes the operator's source file",
        json.loads(src.read_text())["claudeAiOauth"]["accessToken"],
        "fresh-access",
    )
    check(
        "the isolated worker's mirror picks it up",
        json.loads(mirror.read_text())["claudeAiOauth"]["accessToken"],
        "fresh-access",
    )
    check(
        "the mirror still carries no refresh token",
        "refreshToken" in json.loads(mirror.read_text())["claudeAiOauth"],
        False,
    )
    check("the same call reads usage with the fresh token", seen, ["Bearer fresh-access"])
    check("the provider is usable again", prov.available, True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 2) A credential nowhere near expiry is left alone — and not even locked, since renewal is offered on
#    every loop poll.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp, expired=False)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.claude(renew=True)
    check("a live credential is not rotated", post.call_count, 0)
    check("no lock file is created for a live credential", list(src.parent.glob("*.lock")), [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 2b) Renewal is opt-in per caller. `tauceti status` and the dashboard read the same verdict, and must
#     not consume the operator's single-use refresh token or rewrite their credential file to do it.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp)  # expired, so a renewing caller WOULD rotate here
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.claude()
        tc.Quota(quota.cfg).choose(None)
    check("an inspecting read never rotates", post.call_count, 0)
    check(
        "the credential file is untouched", json.loads(src.read_text())["claudeAiOauth"]["accessToken"], "stale-access"
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 3) The Docker shape: the source is itself a stripped mirror, maintained by the dedicated single-writer
#    refresher. The worker must not race it, so there is nothing here it may spend.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp, refresh=None)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.claude(renew=True)
    check("a credential with no refresh token is never rotated", post.call_count, 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 4) The default. Without the opt-in the worker never touches the operator's refresh token, even on a
#    renewing call with an expired credential: it reports Claude unavailable, which is recoverable.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp, opt_in=False)
    seen, usage = usage_seen()
    with patch.object(oauth, "_post_json") as post, usage:
        quota.claude(renew=True)
    check("without --auto-refresh nothing rotates", post.call_count, 0)
    check("the stale token is still used as-is", seen, ["Bearer stale-access"])
    check(
        "the credential file is untouched", json.loads(src.read_text())["claudeAiOauth"]["accessToken"], "stale-access"
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 5) The stored expiry says the token is live and the endpoint says otherwise (clock skew, or a
#    server-side revocation). Believe the endpoint: rotate once, then re-read rather than reporting a
#    401 we can fix.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp, expired=False)
    answers = [(401, {}, None), (200, USAGE_OK, None)]
    seen = []

    def fake(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        return answers.pop(0)

    with rotates_to("rescued-access") as post, patch.object(tc.quota, "_http_get_json", side_effect=fake):
        prov = quota.claude(renew=True)
    check("a 401 forces exactly one rotation", post.call_count, 1)
    check("the retry uses the rotated token", seen, ["Bearer stale-access", "Bearer rescued-access"])
    check("the rescued read decides the verdict", prov.available, True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 6) A 401 that rotation cannot fix (the refresh token is dead too) is reported once, with an error that
#    tells the operator what to do rather than blaming an absent refresher.
tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror = setup(tmp, expired=False)
    with (
        patch.object(oauth, "_post_json", return_value=(400, None)),
        patch.object(tc.quota, "_http_get_json", return_value=(401, {}, None)),
    ):
        prov = quota.claude(renew=True)
    check("an unrecoverable 401 stays a hard block", prov.available, False)
    check(
        "the error names the status and the fix",
        prov.error,
        "claude usage HTTP 401 (access token expired or rejected; log in again)",
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)

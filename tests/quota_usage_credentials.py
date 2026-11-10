#!/usr/bin/env python3
"""$TAUCETI_CLAUDE_USAGE_CREDENTIALS: usage read through a second login, agents left on their own token.

A `claude setup-token` token cannot read the usage endpoint, so a worker whose agents run on one names
an ordinary login for the pacer. The cases pin the two halves of that: every usage read, renewal and
shared record follows the usage login, and nothing here reads, rewrites or republishes the credential
the agents launch from. The login is a plain file, so each case runs as Linux and as macOS.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import loop, oauth, quota_wait  # noqa: E402

VARIABLE = "TAUCETI_CLAUDE_USAGE_CREDENTIALS"
fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def iso(delta_s):
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


USAGE_OK = {
    "five_hour": {"utilization": 5, "resets_at": iso(600)},
    "seven_day": {"utilization": 5, "resets_at": iso(3600)},
}


def login(access, expires_in_s=86400, refresh="login-refresh"):
    block = {"accessToken": access, "expiresAt": int((time.time() + expires_in_s) * 1000)}
    if refresh is not None:
        block["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": block})


def setup(tmp, *, worker="w1", opt_in=False, **usage):
    """An isolated worker whose agents run on a setup-token (a credential with no refresh token,
    mirrored from the directory the marker names), plus a usage login outside both.
    Returns (quota, usage file, [agent credential files])."""
    token_dir, mirror_dir, usage_dir = tmp / "token", tmp / worker / ".claude", tmp / "usage"
    for d in (token_dir, mirror_dir, usage_dir):
        d.mkdir(parents=True, exist_ok=True)
    (mirror_dir / ".tauceti-creds-source").write_text(str(token_dir))
    for d in (token_dir, mirror_dir):
        if not (d / ".credentials.json").exists():
            (d / ".credentials.json").write_text(login("setup-token", 365 * 86400, None))
    usage_file = usage_dir / ".credentials.json"
    if not usage_file.exists():
        usage_file.write_text(login("usage-access", **usage))
    os.environ["CLAUDE_CONFIG_DIR"] = str(mirror_dir)
    os.environ[VARIABLE] = str(usage_dir)
    os.environ.pop("TAUCETI_AUTO_REFRESH", None)
    os.environ.pop("TAUCETI_REFRESH_MIRROR", None)
    if opt_in:
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    cfg = types.SimpleNamespace(home=tmp / worker, quota_cache=tmp / worker / "cache")
    return tc.Quota(cfg), usage_file, [token_dir / ".credentials.json", mirror_dir / ".credentials.json"]


def usage_seen(answers=None):
    seen = []

    def fake(url, headers, timeout=15):
        seen.append(headers["Authorization"])
        return answers.pop(0) if answers else (200, USAGE_OK, None)

    return seen, patch.object(tc.quota, "_http_get_json", side_effect=fake)


def rotates_to(access):
    return patch.object(
        oauth,
        "_post_json",
        return_value=(200, {"access_token": access, "refresh_token": "rt2", "expires_in": 7 * 86400}),
    )


def access(path):
    return json.loads(path.read_text())["claudeAiOauth"]["accessToken"]


def on(platform):
    """The named platform, with a Keychain that fails the case if anything consults it."""
    return (
        patch.object(sys, "platform", platform),
        patch.object(tc.quota, "_claude_keychain_creds", side_effect=AssertionError("Keychain read")),
    )


# 0) Unset, nothing changes: the pacer measures the credential source the agents launch from.
tmp = Path(tempfile.mkdtemp())
try:
    quota, usage_file, agent_files = setup(tmp)
    os.environ.pop(VARIABLE)
    check("unset: no usage login", tc.quota.claude_usage_credentials(), None)
    with patch.object(sys, "platform", "linux"):
        check("unset: the measured source is the agents'", quota._claude_creds_source(), tmp / "token")
    os.environ[VARIABLE] = str(usage_file)
    check("a credentials file is taken as given", tc.quota.claude_usage_credentials(), usage_file)
    os.environ[VARIABLE] = str(usage_file.parent)
    check("a config directory stands for its credentials file", tc.quota.claude_usage_credentials(), usage_file)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


def cases():
    # 1) Usage is read with the login, shared beside it, and the agents' token is neither sent to
    #    the usage endpoint nor rewritten.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, usage_file, agent_files = setup(tmp)
        before = [f.read_bytes() for f in agent_files]
        seen, usage = usage_seen()
        with usage, patch.object(oauth, "_post_json") as post:
            prov = quota.claude(refresh=True, renew=True)
            sibling, _, _ = setup(tmp, worker="w2")
            second = sibling.claude(refresh=True, renew=True)
        check("usage is read with the login's token", seen, ["Bearer usage-access"])
        check("both workers get a verdict from that one read", (prov.available, second.available), (True, True))
        check(
            "observations live beside the usage login",
            quota._usage_dir("claude"),
            tmp / "usage/.tauceti-quota/usage",
        )
        check("without --auto-refresh nothing rotates", post.call_count, 0)
        check("the agents' credential files are untouched", [f.read_bytes() for f in agent_files], before)
        # A model-limit refusal is filed under the credential the next usage read will carry.
        quota.note_model_refusal("claude", quota.launch_credential("claude"))
        with usage:
            check("a model refusal reaches the next verdict", quota.claude().model_refusal, True)
        observed = quota_wait.observe(quota.cfg, "claude", {})
        check(
            "local wake checks watch the usage login",
            observed and observed.credential,
            quota._fingerprint("usage-access"),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 2) --auto-refresh rotates the usage login in place and publishes it nowhere else.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, usage_file, agent_files = setup(tmp, opt_in=True, expires_in_s=-60)
        agent_mirror = tmp / "published.json"
        os.environ["TAUCETI_REFRESH_MIRROR"] = str(agent_mirror)
        before = [f.read_bytes() for f in agent_files]
        seen, usage = usage_seen()
        with rotates_to("fresh-usage") as post, usage:
            quota.claude()
            check("an inspecting read never rotates", post.call_count, 0)
            seen.clear()
            prov = quota.claude(renew=True)
        check("an expired usage login is rotated once", post.call_count, 1)
        check("the rotation lands in the usage login", access(usage_file), "fresh-usage")
        check("the same call reads usage with the fresh token", seen, ["Bearer fresh-usage"])
        check("the provider is usable", prov.available, True)
        check("the agents' credential files are untouched", [f.read_bytes() for f in agent_files], before)
        check("the agents' published mirror is not written", agent_mirror.exists(), False)
    finally:
        os.environ.pop("TAUCETI_REFRESH_MIRROR", None)
        shutil.rmtree(tmp, ignore_errors=True)

    # 3) A 401 because a sibling rotated the shared login after this worker read it: the token now
    #    in the file answers, and no refresh token is spent.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, usage_file, agent_files = setup(tmp, opt_in=True)
        seen = []

        def sibling_rotated(url, headers, timeout=15):
            seen.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer usage-access":
                usage_file.write_text(login("sibling-access"))
                return 401, {}, None
            return 200, USAGE_OK, None

        with (
            patch.object(oauth, "_post_json") as post,
            patch.object(tc.quota, "_http_get_json", side_effect=sibling_rotated),
        ):
            prov = quota.claude(renew=True)
        check("the retry uses the sibling's token", seen, ["Bearer usage-access", "Bearer sibling-access"])
        check("no rotation is spent on it", post.call_count, 0)
        check("the provider is usable", prov.available, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4) A 401 with the file unchanged is this login being refused: rotate it once and re-read.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, usage_file, agent_files = setup(tmp, opt_in=True)
        seen, usage = usage_seen([(401, {}, None), (200, USAGE_OK, None)])
        with rotates_to("rescued-usage") as post, usage:
            prov = quota.claude(renew=True)
        check("a refused login is rotated once", post.call_count, 1)
        check("the retry uses the rotated token", seen, ["Bearer usage-access", "Bearer rescued-usage"])
        check("the agents' token is still the setup-token", [access(f) for f in agent_files], ["setup-token"] * 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) A named login that is missing blocks, by name. The agents' token is not tried in its place,
    #    because the endpoint's refusal of it would read as a credential problem it is not.
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, usage_file, agent_files = setup(tmp)
        usage_file.unlink()
        seen, usage = usage_seen()
        with usage:
            prov = quota.claude(renew=True)
        check("a missing usage login makes no request", seen, [])
        check(
            "the error names the file and the variable",
            prov.error,
            f"no {usage_file} (the usage login $TAUCETI_CLAUDE_USAGE_CREDENTIALS names)",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 6) The loop's advice after a refusal points at the usage login, not at the agents' credential.
    refused = tc.quota.Provider("claude", False, None, error="claude usage HTTP 401 (access token expired)")
    os.environ[VARIABLE] = "/logins/usage/.credentials.json"
    check(
        "the 401 hint names the usage login",
        loop._credential_hint("claude", refused),
        ". Sign in again to the usage login at /logins/usage/.credentials.json, or --auto-refresh to let an"
        " unattended worker rotate it (see docs/quota.md)",
    )


for platform in ("linux", "darwin"):
    print(f"--- {platform}")
    plat, keychain = on(platform)
    with plat, keychain:
        cases()

# 7) During a quota wait on macOS the pacer pins the Keychain identity it last read, so that local
#    checks do not poll the Keychain. A usage login is a file its siblings rotate: it is re-read.
tmp = Path(tempfile.mkdtemp())
try:
    quota, usage_file, agent_files = setup(tmp)
    wait = quota_wait.QuotaWait(quota.cfg, "claude")
    wait.identities = {"claude": {"accessToken": "read-earlier"}}
    wait.pending = (set(), set())
    with patch.object(sys, "platform", "darwin"):
        wait.prepare(quota)
        check("a usage login is not pinned across a wait", "_claude_creds" in vars(quota), False)
        os.environ.pop(VARIABLE)
        wait.prepare(quota)
        check("the Keychain identity still is", "_claude_creds" in vars(quota), True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

for name in (VARIABLE, "CLAUDE_CONFIG_DIR", "TAUCETI_AUTO_REFRESH"):
    os.environ.pop(name, None)
print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)

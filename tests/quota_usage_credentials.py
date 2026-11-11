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

# 0b) A `~/…` value means the operator's home, also after isolation moves $HOME off macOS.
tmp = Path(tempfile.mkdtemp())
saved_env = dict(os.environ)
try:
    real = tmp / "real"
    (real / "usage").mkdir(parents=True)
    (real / "usage" / ".credentials.json").write_text(login("usage-access"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TAUCETI_DATA_HOME"):
        os.environ.pop(name, None)
    os.environ.update({"HOME": str(real), VARIABLE: "~/usage"})
    with (
        patch.object(sys, "platform", "linux"),
        patch.object(tc.agents, "_worker_iso_home", lambda wid, _base=None: tmp / "iso"),
        patch.object(tc.agents, "share_build_caches", lambda wid, home: {}),
    ):
        tc.agents.isolate_home("worker1")
    check("isolation moved $HOME", os.environ["HOME"], str(tmp / "iso"))
    check(
        "the usage login still resolves in the operator's home",
        tc.quota.claude_usage_credentials(),
        real / "usage" / ".credentials.json",
    )
finally:
    os.environ.clear()
    os.environ.update(saved_env)
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

    # 5b) Two usage logins kept as files in one directory are two accounts to the cooldowns, unless the
    #     OAuth block names the account.
    tmp = Path(tempfile.mkdtemp())
    try:
        logins = tmp / "logins"
        logins.mkdir()
        (logins / "alice.json").write_text(login("alice-access"))
        (logins / "bob.json").write_text(login("bob-access"))
        alice, _, _ = setup(tmp, worker="alice")
        os.environ[VARIABLE] = str(logins / "alice.json")
        seen, usage = usage_seen([(429, {}, 3600)])
        with usage:
            limited = alice.claude(refresh=True, renew=True)
        bob, _, _ = setup(tmp, worker="bob")
        os.environ[VARIABLE] = str(logins / "bob.json")
        with usage:
            other = bob.claude(refresh=True, renew=True)
        check("alice is rate limited", limited.rate_limited, True)
        check("bob's login is read on its own", seen, ["Bearer alice-access", "Bearer bob-access"])
        check("bob is not held by alice's cooldown", other.available, True)
        check("the key is the file", bob._claude_account_key(), f"creds:{logins / 'bob.json'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5c) A usage login behind a directory this worker cannot enter reads as a missing login, and the
    #     selection still reaches its other providers.
    tmp = Path(tempfile.mkdtemp())
    locked = tmp / "locked"
    try:
        quota, usage_file, agent_files = setup(tmp)
        (locked / "usage").mkdir(parents=True)
        os.environ[VARIABLE] = str(locked / "usage")
        locked.chmod(0)
        seen, usage = usage_seen()
        with usage:
            agent, provs = quota.choose("claude", renew=True)
        check("an unreadable usage login makes no request", seen, [])
        check("claude reads as unavailable", (agent, provs["claude"].available), (None, False))
    finally:
        locked.chmod(0o700)
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

# 8) Off macOS the agents launch from the file in $CLAUDE_CONFIG_DIR, so a healthy usage login does not
#    admit work while that file is missing or holds a setup-token past its expiry.
tmp = Path(tempfile.mkdtemp())
try:
    quota, usage_file, agent_files = setup(tmp)
    seen, usage = usage_seen()
    with patch.object(sys, "platform", "linux"), usage:
        check("a live setup-token is admitted", quota.claude(renew=True).available, True)
        for f in agent_files:
            f.write_text(login("setup-token", -60, None))
        seen.clear()
        expired = quota.claude(renew=True)
        check("an expired setup-token is not", expired.available, False)
        check("no usage request is made for it", seen, [])
        check("the error names the agents' file", str(agent_files[1]) in (expired.error or ""), True)
        # An isolated copy never holds a refresh token, so only an unisolated worker's own login renews.
        (agent_files[1].parent / ".tauceti-creds-source").unlink()
        agent_files[1].write_text(login("agent-login", -60))
        check("an expired login that can renew itself is", quota.claude(renew=True).available, True)
        for f in agent_files:
            f.unlink()
        check("a missing agents' file is not", quota.claude(renew=True).available, False)
        # A native agent given the token in $CLAUDE_CODE_OAUTH_TOKEN does not read the file at all.
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "env-setup-token"}):
            _argv, agent_env = tc.agents.host_agent_argv("prompt", "claude")
            check(
                "the native agent receives the environment token",
                agent_env.get("CLAUDE_CODE_OAUTH_TOKEN"),
                "env-setup-token",
            )
            check("an environment token with no file is admitted", quota.claude(renew=True).available, True)
            agent_files[1].write_text(login("setup-token", -60, None))
            check("so is one beside a stale file", quota.claude(renew=True).available, True)
            # A --bubble round runs the agent in the container with only the mounted file.
            inner = tc.agents.bubble_work_cmd(tc.agents.agent_inner_cmd("claude"))
            check("the bubble agent is not given the environment token", "CLAUDE_CODE_OAUTH_TOKEN" in inner, False)
            with patch.dict(os.environ, {"TAUCETI_CLAUDE_ENV_TOKEN_ROUTE": "0"}):
                check(
                    "a file-only launch beside a stale file is not admitted", quota.claude(renew=True).available, False
                )
                agent_files[1].unlink()
                check("nor one with no file", quota.claude(renew=True).available, False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 9) `work` records whether its phases' Claude launches inherit the environment token, so its loop
#    children and the pacer agree on it.
reaches = tc.cli.claude_env_token_reaches_agents
check("host phases inherit it", reaches([], bubble=False), True)
check("bubble phases do not", reaches([], bubble=True), False)
check("overlap-scan stays on the host under --bubble", reaches(["overlap-scan"], bubble=True), True)
check("so does progress", reaches(["progress"], bubble=True), True)
check("one bubble phase among them is enough", reaches(["overlap-scan", "review"], bubble=True), False)
check("security-audit's private home does not", reaches(["security-audit"], bubble=False), False)
saved_env = dict(os.environ)
try:
    for bubble, only, expect in ((True, [], "0"), (False, [], "1"), (True, ["overlap-scan"], "1")):
        os.environ.pop("TAUCETI_CLAUDE_ENV_TOKEN_ROUTE", None)
        args = types.SimpleNamespace(bubble=bubble, author_model=None, author_effort="high")
        try:
            tc.cli.cmd_work(args, only=only, agent="auto", one_round=False)
        except tc.Die:
            pass
        check(f"--bubble={bubble} --only {only} is recorded", os.environ.get("TAUCETI_CLAUDE_ENV_TOKEN_ROUTE"), expect)
finally:
    os.environ.clear()
    os.environ.update(saved_env)

for name in (VARIABLE, "CLAUDE_CONFIG_DIR", "TAUCETI_AUTO_REFRESH"):
    os.environ.pop(name, None)
print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)

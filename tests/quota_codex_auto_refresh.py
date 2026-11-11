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
import signal
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


def jwt(expires_in, plan="plus"):
    claims = {"exp": int(time.time() + expires_in)}
    if plan is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_plan_type": plan}
    claims = json.dumps(claims).encode()
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


def setup(tmp, *, expires_in=60, refresh="operator-refresh", opt_in=True, plan="plus"):
    """An isolated worker: it reads a MIRROR of the operator's auth.json, with a marker naming the
    original. Returns (quota, source path, mirror path, the access token both start with)."""
    src, dst = tmp / "real" / ".codex", tmp / "iso" / ".codex"
    for d in (src, dst):
        d.mkdir(parents=True)
    (dst / ".tauceti-creds-source").write_text(str(src))
    for key in ("CLAUDE_CONFIG_DIR", "TAUCETI_AUTO_REFRESH", "TAUCETI_REFRESH_MIRROR", "TAUCETI_CODEX_REFRESH_PLANS"):
        os.environ.pop(key, None)
    os.environ["CODEX_HOME"] = str(dst)
    if opt_in:
        os.environ["TAUCETI_AUTO_REFRESH"] = "1"
    stale = jwt(expires_in, plan)
    (src / "auth.json").write_text(auth(stale, refresh))
    (dst / "auth.json").write_text(auth(stale, CODEX_RT_PLACEHOLDER))
    # Codex's administrator layers, redirected into the temporary tree.
    etc = tmp / "etc" / "codex"
    etc.mkdir(parents=True)
    tc.quota.CODEX_SYSTEM_CONFIG = etc / "config.toml"
    tc.quota.CODEX_REQUIREMENT_FILES = (etc / "requirements.toml", etc / "managed_config.toml")
    tc.quota.CODEX_MANAGED_PREFERENCES = tmp / "Managed Preferences"
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


def renews_with(tmp, files, *, expires_in=60, first=(200, USAGE_OK, None), plan="plus", named=None):
    """Write `files` (path relative to tmp -> text), then make one renewing read. Returns how many
    token exchanges it made."""
    quota, src, mirror, stale = setup(tmp, expires_in=expires_in, plan=plan)
    if named is not None:
        os.environ["TAUCETI_CODEX_REFRESH_PLANS"] = named
    for rel, text in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    seen, usage = usage_seen(first)
    with rotates_to(jwt(10 * DAY)) as post, usage:
        quota.codex(renew=True)
    return post.call_count


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

# 6b) An isolated worker spends the SOURCE's refresh token, so the source's own config must be file-backed
#     too; the isolated directory has no config of its own and defaults to file.
for name, body in (
    ("keyring", 'cli_auth_credentials_store = "keyring"\n'),
    ("auto", 'cli_auth_credentials_store = "auto"\n'),
    ("malformed", "cli_auth_credentials_store = [\n"),
):
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp)
        (src.parent / "config.toml").write_text(body)
        seen, usage = usage_seen()
        with rotates_to(jwt(10 * DAY)) as post, usage:
            quota.codex(renew=True)
        check(f"a source store that is {name} is never rotated", post.call_count, 0)
        check(f"...and its refresh token is kept ({name})", tokens(src)["refresh_token"], "operator-refresh")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

tmp = Path(tempfile.mkdtemp())
try:
    quota, src, mirror, stale = setup(tmp)
    (src.parent / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    with rotates_to(jwt(10 * DAY)) as post, usage_seen()[1]:
        quota.codex(renew=True)
    check("a source store that is file is rotated", post.call_count, 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 6c) Codex reads /etc/codex/config.toml below the user layer, and requirements.toml and the legacy
#     managed_config.toml above it; a forced macOS managed preference can set the store too. With the
#     user layers silent, an administrator's non-file store is the one codex uses.
KEYRING = 'cli_auth_credentials_store = "keyring"\n'
FILE = 'cli_auth_credentials_store = "file"\n'
SOURCE, WORKER = "real/.codex/config.toml", "iso/.codex/config.toml"
for name, files, expires_in, first, want in (
    ("system keyring", {"etc/codex/config.toml": KEYRING}, 60, (200, USAGE_OK, None), 0),
    ("system keyring, 401 path", {"etc/codex/config.toml": KEYRING}, 5 * DAY, (401, {}, None), 0),
    ("system auto", {"etc/codex/config.toml": 'cli_auth_credentials_store = "auto"\n'}, 60, (200, USAGE_OK, None), 0),
    (
        "unreadable system config",
        {"etc/codex/config.toml": "cli_auth_credentials_store = [\n"},
        60,
        (200, USAGE_OK, None),
        0,
    ),
    (
        "system keyring under a source-only file override",
        {"etc/codex/config.toml": KEYRING, SOURCE: FILE},
        60,
        (200, USAGE_OK, None),
        0,
    ),
    (
        "system keyring under file overrides in both user layers",
        {"etc/codex/config.toml": KEYRING, SOURCE: FILE, WORKER: FILE},
        60,
        (200, USAGE_OK, None),
        1,
    ),
    (
        "requirements keyring",
        {"etc/codex/requirements.toml": KEYRING, SOURCE: FILE, WORKER: FILE},
        60,
        (200, USAGE_OK, None),
        0,
    ),
    (
        "managed_config keyring",
        {"etc/codex/managed_config.toml": KEYRING, SOURCE: FILE, WORKER: FILE},
        60,
        (200, USAGE_OK, None),
        0,
    ),
    ("system file", {"etc/codex/config.toml": FILE}, 60, (200, USAGE_OK, None), 1),
    ("a managed preference", {"Managed Preferences/com.openai.codex.plist": ""}, 60, (200, USAGE_OK, None), 0),
    (
        "a per-user managed preference",
        {"Managed Preferences/someone/com.openai.codex.plist": ""},
        60,
        (200, USAGE_OK, None),
        0,
    ),
):
    tmp = Path(tempfile.mkdtemp())
    try:
        check(f"{name}: exchanges", renews_with(tmp, files, expires_in=expires_in, first=first), want)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# 6e) An unreadable managed-preference directory, at either level, cannot show that no policy applies.
for name, locked in (("root", "Managed Preferences"), ("per-user directory", "Managed Preferences/someone")):
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp)
        (tmp / "Managed Preferences/someone").mkdir(parents=True)
        (tmp / "Managed Preferences/someone/com.openai.codex.plist").write_text("")
        (tmp / locked).chmod(0)

        def denied(path, locked=tmp / locked, real_scandir=os.scandir):
            # Mode 0 does not stop root from listing, so deny it explicitly as well.
            if Path(path) == locked:
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        with rotates_to(jwt(10 * DAY)) as post, usage_seen()[1], patch.object(os, "scandir", denied):
            quota.codex(renew=True)
        check(f"an unreadable managed-preference {name} blocks renewal", post.call_count, 0)
    finally:
        for d in ("Managed Preferences", "Managed Preferences/someone"):
            if (tmp / d).exists():
                (tmp / d).chmod(0o755)
        shutil.rmtree(tmp, ignore_errors=True)

# 6f) Codex fetches a cloud policy, which can require a non-file store over any local setting, for
#     business, education and enterprise workspaces. Renewal runs only for a plan outside that set.
for plan, want in (("plus", 1), ("team", 1), ("business", 0), ("hc", 0), ("edu_plus", 0), ("someday", 0), (None, 0)):
    tmp = Path(tempfile.mkdtemp())
    try:
        quota, src, mirror, stale = setup(tmp, plan=plan)
        for d in (src.parent, mirror.parent):
            (d / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        with rotates_to(jwt(10 * DAY)) as post, usage_seen()[1]:
            quota.codex(renew=True)
        check(f"plan {plan!r} under explicit file stores: exchanges", post.call_count, want)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# 6g) $TAUCETI_CODEX_REFRESH_PLANS names further plans the operator asserts are file-backed. Only the
#     exact strings named are added; everything else above still applies, and a malformed value
#     disables Codex renewal with a log line rather than widening it.
def named(tmp, value, plan, *, source_store="file"):
    quota, src, mirror, stale = setup(tmp, plan=plan)
    os.environ["TAUCETI_CODEX_REFRESH_PLANS"] = value
    (src.parent / "config.toml").write_text(f'cli_auth_credentials_store = "{source_store}"\n')
    logged = []
    with (
        rotates_to(jwt(10 * DAY)) as post,
        usage_seen()[1],
        patch.object(tc.quota, "log", side_effect=logged.append),
    ):
        quota.codex(renew=True)
    os.environ.pop("TAUCETI_CODEX_REFRESH_PLANS", None)
    return post.call_count, logged


for name, value, plan, store, want in (
    ("a named plan renews", "edu_pro", "edu_pro", "file", 1),
    ("one of several named plans renews", "business,edu_pro", "edu_pro", "file", 1),
    ("an unnamed plan is still refused", "edu_pro", "business", "file", 0),
    ("a plan is matched exactly", "edu", "edu_pro", "file", 0),
    ("a named plan still needs a file store", "edu_pro", "edu_pro", "keyring", 0),
    ("the default plans still renew beside a named one", "edu_pro", "plus", "file", 1),
    ("an empty value adds nothing", "", "edu_pro", "file", 0),
):
    tmp = Path(tempfile.mkdtemp())
    try:
        check(f"{name}: exchanges", named(tmp, value, plan, source_store=store)[0], want)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

for value in ("*", "edu_pro,", "edu pro", "EDU_PRO", "edu_pro;business"):
    tmp = Path(tempfile.mkdtemp())
    try:
        count, logged = named(tmp, value, "plus")
        check(f"malformed {value!r} renews nothing, not even a default plan", count, 0)
        check(
            f"...and says so ({value!r})",
            any("TAUCETI_CODEX_REFRESH_PLANS" in line for line in logged),
            True,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

check(
    "the setting parses to the named plans", tc.quota.codex_refresh_plans("business,edu_pro"), {"business", "edu_pro"}
)
for value in ("*", ",", "a b"):
    try:
        tc.quota.codex_refresh_plans(value)
        raised = False
    except ValueError:
        raised = True
    check(f"parsing {value!r} raises", raised, True)


# 6h) Every command checks the setting at startup, beside the other configuration lints.
class Reached(Exception):
    pass


def startup(value):
    with (
        patch.dict(os.environ, {"TAUCETI_CODEX_REFRESH_PLANS": value}),
        patch.object(tc.cli, "resolve_pace", side_effect=Reached),
    ):
        try:
            tc.cli.main(["usage"])
        except tc.Die as e:
            return str(e)
        except Reached:
            return "reached"


check("a well-formed setting passes the startup check", startup("edu_pro"), "reached")
check("a malformed setting stops the command", "TAUCETI_CODEX_REFRESH_PLANS" in startup("edu pro"), True)


# 6i) A `config_server` in a local requirements file makes Codex fetch configuration from that server
#     for any plan, and its defaults can choose the store; nothing is renewed while one is enrolled.
SERVER = '[config_server]\nbase_url = "https://config.invalid"\n'
OK, UNAUTHORIZED = (200, USAGE_OK, None), (401, {}, None)
for name, files, kwargs, want in (
    ("requirements.toml", {"etc/codex/requirements.toml": SERVER}, {}, 0),
    (
        "requirements.toml, 401 path",
        {"etc/codex/requirements.toml": SERVER},
        {"expires_in": 5 * DAY, "first": UNAUTHORIZED},
        0,
    ),
    ("managed_config.toml", {"etc/codex/managed_config.toml": SERVER}, {}, 0),
    ("a named plan", {"etc/codex/requirements.toml": SERVER}, {"plan": "edu_pro", "named": "edu_pro"}, 0),
    ("control: a named plan without one", {}, {"plan": "edu_pro", "named": "edu_pro"}, 1),
    ("control: other requirements", {"etc/codex/requirements.toml": "allowed_login_methods = []\n"}, {}, 1),
):
    tmp = Path(tempfile.mkdtemp())
    try:
        check(f"config server, {name}: exchanges", renews_with(tmp, files, **kwargs), want)
    finally:
        os.environ.pop("TAUCETI_CODEX_REFRESH_PLANS", None)
        shutil.rmtree(tmp, ignore_errors=True)


# 6d) A stop signal that lands while the endpoint answers must not unwind before the answer is
#     written: the endpoint has already retired the old refresh token. The stop is honoured after.
class Stopped(KeyboardInterrupt):
    pass


def stop(_signum, _frame):
    raise Stopped


for sig in (signal.SIGTERM, signal.SIGINT):
    tmp = Path(tempfile.mkdtemp())
    previous = signal.signal(sig, stop)
    try:
        quota, src, mirror, stale = setup(tmp)
        fresh = jwt(10 * DAY)

        def answer(url, payload, timeout=15, sig=sig, fresh=fresh):
            signal.raise_signal(sig)
            return 200, {"access_token": fresh, "refresh_token": "rt2"}

        stopped = False
        with patch.object(oauth, "_post_json", side_effect=answer), usage_seen()[1]:
            try:
                quota.codex(renew=True)
            except Stopped:
                stopped = True
        check(f"{sig.name} during the exchange still stops the caller", stopped, True)
        check(
            f"...after the rotated token is saved ({sig.name})",
            tokens(src),
            {"access_token": fresh, "refresh_token": "rt2"},
        )
        check(f"...and the handler is restored ({sig.name})", signal.getsignal(sig), stop)
    finally:
        signal.signal(sig, previous)
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

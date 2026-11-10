#!/usr/bin/env python3
"""A macOS worker whose agents run on a setup-token file never consults the login Keychain.

With $TAUCETI_CLAUDE_USAGE_CREDENTIALS set, the agents' credential is `.credentials.json` in
$CLAUDE_CONFIG_DIR and the pacer's is the usage login, both plain files. The cases follow one isolated
worker through seeding, a usage read, a replaced token and a Bubble launch. `security` is a stub holding
what a real login Keychain would hold beside such a worker, the operator's own sign-in, so a read of it
shows both as a recorded call and as the wrong credential.

The platform is simulated, so this runs and means the same thing on every host. Exit 0 = pass.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

agents = tc.agents
VARIABLE = "TAUCETI_CLAUDE_USAGE_CREDENTIALS"
fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


YEAR_MS = int((time.time() + 365 * 86400) * 1000)
OPERATOR = {"claudeAiOauth": {"accessToken": "OPERATOR", "refreshToken": "OPERATOR-R", "expiresAt": YEAR_MS}}
USAGE = {"claudeAiOauth": {"accessToken": "USAGE", "refreshToken": "USAGE-R", "expiresAt": YEAR_MS}}


def setup_token(access):
    return {"claudeAiOauth": {"accessToken": access, "expiresAt": YEAR_MS, "scopes": ["user:inference"]}}


def oauth(path):
    return json.loads(path.read_text())["claudeAiOauth"]


class Keychain:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *a, **k):
        if cmd[0] != "security":
            raise AssertionError(f"unexpected subprocess: {cmd}")
        self.calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(OPERATOR), stderr="")


tmp = Path(tempfile.mkdtemp(prefix="macos-token-"))
real, token_dir, usage_dir, iso = tmp / "real", tmp / "token", tmp / "usage", tmp / "iso"
for d in (real, token_dir, usage_dir):
    d.mkdir()
token_file, worker_file = token_dir / ".credentials.json", iso / ".claude" / ".credentials.json"
token_file.write_text(json.dumps(setup_token("TOKEN-1")))
(usage_dir / ".credentials.json").write_text(json.dumps(USAGE))

saved_env = dict(os.environ)
saved = tc.subprocess.run, sys.platform, agents._worker_iso_home
keychain = Keychain()
try:
    for k in ("CODEX_HOME", "TAUCETI_DATA_HOME", "TAUCETI_AUTO_REFRESH", "TAUCETI_REFRESH_MIRROR"):
        os.environ.pop(k, None)
    os.environ.update(
        {"HOME": str(real), "USER": "alice", "CLAUDE_CONFIG_DIR": str(token_dir), VARIABLE: str(usage_dir)}
    )
    tc.subprocess.run, sys.platform = keychain, "darwin"
    agents._worker_iso_home = lambda wid, _base=None: iso

    # 1. Isolation seeds the worker's copy from the token directory and leaves $HOME alone.
    agents.isolate_home("worker1")
    check("the worker's config directory is its own", os.environ["CLAUDE_CONFIG_DIR"], str(iso / ".claude"))
    check("$HOME does not move", os.environ["HOME"], str(real))
    check("the copy is seeded with the token", oauth(worker_file), setup_token("TOKEN-1")["claudeAiOauth"])
    check(
        "the marker names the token directory", (iso / ".claude" / ".tauceti-creds-source").read_text(), str(token_dir)
    )

    # 2. The pacer reads the usage login, and may renew it: it is not a Keychain credential.
    cfg = types.SimpleNamespace(home=real, state=iso / "state", quota_cache=iso / "cache")
    check("usage is read with the usage login", tc.Quota(cfg)._claude_creds(), (USAGE["claudeAiOauth"], False))

    # 3. A replaced token reaches the worker's copy, which a Bubble launch and the host `claude` read.
    token_file.write_text(json.dumps(setup_token("TOKEN-2")))
    tc.mirror_creds(cfg)
    check("a replaced token is mirrored on macOS", oauth(worker_file), setup_token("TOKEN-2")["claudeAiOauth"])

    # 4. The usage login is what makes the file the store. Without one the Keychain is, as before.
    token_file.write_text(json.dumps(setup_token("TOKEN-3")))
    usage_value = os.environ.pop(VARIABLE)
    tc.mirror_creds(cfg)
    os.environ[VARIABLE] = usage_value
    check("no usage login: the Claude file is not mirrored", oauth(worker_file)["accessToken"], "TOKEN-2")
    tc.mirror_creds(cfg)
    check("usage login back: mirrored again", oauth(worker_file)["accessToken"], "TOKEN-3")

    # 5. Bubble is seeded from the worker's copy, in the private directory macOS launches already use.
    seed = agents._stage_claude_creds_for_bubble(cfg)
    check("bubble is seeded with the token", oauth(seed / ".credentials.json"), setup_token("TOKEN-3")["claudeAiOauth"])
    check("the seed is outside the worker's config directory", seed.parent, cfg.state)
    check("the seed file is 0600", oct(os.stat(seed / ".credentials.json").st_mode & 0o777), "0o600")
    shutil.rmtree(seed)

    # 6. A missing token file stops the launch and names the file, not a Keychain to unlock.
    worker_file.unlink()
    message = ""
    try:
        agents._stage_claude_creds_for_bubble(cfg)
    except tc.Die as e:
        message = str(e)
    check("a missing token file stops the bubble launch", str(worker_file) in message, True)
    check("the message does not send the operator to the Keychain", "Keychain" in message, False)

    check("the login Keychain was never read", keychain.calls, [])
finally:
    tc.subprocess.run, sys.platform, agents._worker_iso_home = saved
    os.environ.clear()
    os.environ.update(saved_env)
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""macOS Keychain support. The pacer reads Claude Code's OAuth blob from the login Keychain (read-only,
keychain-first, marked from_keychain so the 401 path never refreshes a token shared with the operator's
claude). For bubble rounds, where the in-container claude needs a .credentials.json, the credential is
staged from the Keychain in a private, transient CLAUDE_CONFIG_DIR without touching the host file."""

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

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got}")
    if not ok:
        print(f"      expected: {expect}")
        fails += 1


OAUTH = {"claudeAiOauth": {"accessToken": "KC", "refreshToken": "R", "expiresAt": 1}}  # expiresAt in the past
FRESH = {"claudeAiOauth": {"accessToken": "F", "refreshToken": "R", "expiresAt": 9999999999999}}  # far future


class FakeRun:
    """Stand-in for subprocess.run over `security`. Records the commands it saw and replays a scripted
    (returncode, stdout) per call, so we can assert the -a→service-only fallback and the unlock retry."""

    def __init__(self, results):
        self.results, self.calls, self.i = results, [], 0

    def __call__(self, cmd, *a, **k):
        self.calls.append(cmd)
        rc, out = self.results[min(self.i, len(self.results) - 1)]
        self.i += 1
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")


orig_run, orig_platform, orig_user = tc.subprocess.run, sys.platform, os.environ.get("USER")
os.environ["USER"] = "alice"
os.environ.pop("CLAUDE_CONFIG_DIR", None)
tc.sys.platform = "darwin"
try:
    # 1. The plain `-a $USER -w` hit parses to the same dict as the file would.
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    check("keychain read parses the OAuth blob", tc._claude_keychain_creds(), OAUTH)
    check(
        "keychain read uses -s/-a/-w",
        tc.subprocess.run.calls[0],
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", "alice", "-w"],
    )

    # 2. errSecItemNotFound (44) for the -a search falls back to the service-only search.
    fr = FakeRun([(44, ""), (0, json.dumps(OAUTH))])
    tc.subprocess.run = fr
    check("falls back to service-only search", tc._claude_keychain_creds(), OAUTH)
    check(
        "fallback drops -a", fr.calls[1], ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
    )

    # 3. A locked Keychain (36) is treated as 'no creds', not an error (the pacer read is non-interactive).
    tc.subprocess.run = FakeRun([(36, "")])
    check("locked keychain → None", tc._claude_keychain_creds(), None)

    # 4. Not found at all → None.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    check("item absent → None", tc._claude_keychain_creds(), None)

    # 5. macOS reads the Keychain FIRST (authoritative) and marks creds from_keychain, so the 401 path
    #    never refreshes (rotating the shared token would log out the operator's claude).
    tmp = Path(tempfile.mkdtemp())
    cfg = types.SimpleNamespace(home=tmp, quota_cache=tmp)
    q = tc.Quota(cfg)
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("pacer reads keychain creds", oauth, OAUTH["claudeAiOauth"])
    check("keychain creds are from_keychain", from_kc, True)

    # 6. The Keychain wins over a .credentials.json on macOS (a file there is only a mirror / stale export),
    #    so a credentials file we materialize for the bubble never makes the pacer refresh a shared token.
    (tmp / ".claude").mkdir()
    (tmp / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "FILE"}}))
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("keychain wins over a file on macOS", oauth, OAUTH["claudeAiOauth"])
    check("keychain-win creds are from_keychain", from_kc, True)

    # 7. An empty/unreadable Keychain falls back to the file, but on macOS that file is still a Keychain
    #    mirror sharing the one refresh token, so it stays non-refreshable (from_keychain) too.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    oauth, from_kc = q._claude_creds()
    check("empty keychain falls back to the file", oauth, {"accessToken": "FILE"})
    check("macOS file fallback is non-refreshable too", from_kc, True)

    # 8. Interactive read (for bubble seeding): a locked Keychain runs `security unlock-keychain`, then
    #    the retry succeeds. (-a→36, service-only→36, unlock, -a→blob.)
    fr = FakeRun([(36, ""), (36, ""), (0, ""), (0, json.dumps(FRESH))])
    tc.subprocess.run = fr
    check("interactive read unlocks then reads", tc._claude_keychain_creds_interactive(), FRESH)
    check("interactive read runs unlock-keychain", fr.calls[2], ["security", "unlock-keychain"])

    # 9. Bubble seeding on macOS with no file: stage the Keychain blob privately, 0700 dir + 0600 file.
    seed_home = Path(tempfile.mkdtemp())
    cfg2 = types.SimpleNamespace(home=seed_home, state=seed_home / "state")
    orphan = cfg2.state / "bubble-claude-seed-orphan"
    orphan.mkdir(parents=True)
    (orphan / ".credentials.json").write_text("old secret")
    tc.subprocess.run = FakeRun([(0, json.dumps(FRESH))])
    private = tc._stage_claude_creds_for_bubble(cfg2)
    target = private / ".credentials.json"
    check("removes a seed orphaned by a prior SIGKILL", orphan.exists(), False)
    check("does not create a host credentials file", (seed_home / ".claude" / ".credentials.json").exists(), False)
    check("does not create the host config directory", (seed_home / ".claude").exists(), False)
    check("stages the keychain blob in the private dir", json.loads(target.read_text()), FRESH)
    check("private config dir is 0700", oct(os.stat(private).st_mode & 0o777), "0o700")
    check("staged file is 0600", oct(os.stat(target).st_mode & 0o777), "0o600")
    shutil.rmtree(private)

    # 10. A pre-existing host file is operator-owned: Keychain wins for the seed, but the file is untouched.
    host_target = seed_home / ".claude" / ".credentials.json"
    host_target.parent.mkdir()
    host_target.write_text(json.dumps(OAUTH))
    tc.subprocess.run = FakeRun([(0, json.dumps(FRESH))])
    private2 = tc._stage_claude_creds_for_bubble(cfg2)
    check(
        "private seed prefers the authoritative keychain",
        json.loads((private2 / ".credentials.json").read_text()),
        FRESH,
    )
    check("existing host file is not overwritten", json.loads(host_target.read_text()), OAUTH)
    shutil.rmtree(private2)

    # 11. Keychain unreadable but a host file exists: copy it privately without modifying the source.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    private3 = tc._stage_claude_creds_for_bubble(cfg2)
    check(
        "keychain unreadable copies the existing file", json.loads((private3 / ".credentials.json").read_text()), OAUTH
    )
    check("file fallback leaves the host source untouched", json.loads(host_target.read_text()), OAUTH)
    shutil.rmtree(private3)

    # 12. Off darwin: a no-op (the file is the store there) — never reads a Keychain.
    tc.sys.platform = "linux"
    fr = FakeRun([(0, json.dumps(FRESH))])
    tc.subprocess.run = fr
    staged = tc._stage_claude_creds_for_bubble(
        types.SimpleNamespace(home=Path(tempfile.mkdtemp()), state=Path(tempfile.mkdtemp()))
    )
    check("ensure is a no-op off darwin", fr.calls, [])
    check("off-darwin returns no private seed", staged, None)
    tc.sys.platform = "darwin"

    # 13. macOS, no file, and the Keychain has nothing → a clear Die rather than a silent empty seed.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    raised = False
    try:
        missing_home = Path(tempfile.mkdtemp())
        tc._stage_claude_creds_for_bubble(types.SimpleNamespace(home=missing_home, state=missing_home / "state"))
    except tc.Die:
        raised = True
    check("ensure raises Die when no creds anywhere", raised, True)

    # 14. The launch path overrides CLAUDE_CONFIG_DIR only in Bubble's env and removes the private seed
    #     after the subprocess exits. The host env and its credential file stay byte-for-byte unchanged.
    class Cleanup:
        def __init__(self):
            self.steps = []

        def add_cleanup(self, fn):
            self.steps.append(fn)

    launch_home = Path(tempfile.mkdtemp())
    launch_cfgdir = launch_home / "operator-claude"
    launch_cfgdir.mkdir()
    launch_host_file = launch_cfgdir / ".credentials.json"
    launch_host_file.write_text(json.dumps(OAUTH))
    launch_cfg = types.SimpleNamespace(
        home=launch_home,
        state=launch_home / "state",
        wid="seed-test",
        logdir=launch_home / "logs",
    )
    cleanup = Cleanup()
    w = types.SimpleNamespace(cfg=launch_cfg, rc=cleanup)
    opts = types.SimpleNamespace(work_model="claude")
    old_cfgdir = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(launch_cfgdir)
    old_ensure_home = tc.agents.ensure_bubble_home
    old_pop = tc.agents._bubble_pop
    old_keychain = tc.agents._claude_keychain_creds_interactive
    old_subprocess_run = tc.agents.subprocess.run
    seen = {}

    def fake_bubble_run(_argv, *, env):
        private_dir = Path(env["CLAUDE_CONFIG_DIR"])
        seen["private_dir"] = private_dir
        seen["blob"] = json.loads((private_dir / ".credentials.json").read_text())
        return types.SimpleNamespace(returncode=0)

    try:
        tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
        tc.agents._bubble_pop = lambda _cfg, _env: None
        tc.agents._claude_keychain_creds_interactive = lambda: FRESH
        tc.agents.subprocess.run = fake_bubble_run
        rc = tc.run_in_bubble(w, "review", "PROMPT", opts, inner_cmd="true", cred_model="claude")
        check("bubble launch succeeds with a private seed", rc, 0)
        check("bubble subprocess receives current Keychain creds", seen["blob"], FRESH)
        check("bubble subprocess does not receive the host config dir", seen["private_dir"] != launch_cfgdir, True)
        check("private seed is removed after bubble exits", seen["private_dir"].exists(), False)
        check("process CLAUDE_CONFIG_DIR is unchanged", os.environ["CLAUDE_CONFIG_DIR"], str(launch_cfgdir))
        check("bubble launch leaves the host file unchanged", json.loads(launch_host_file.read_text()), OAUTH)
        for cleanup_step in reversed(cleanup.steps):
            cleanup_step()  # signal/atexit cleanups remain idempotent after prompt normal cleanup
        check("registered seed cleanup is idempotent", seen["private_dir"].exists(), False)

        # An exception from Bubble must propagate without leaking the private credential directory.
        failed_cleanup = Cleanup()
        failed_w = types.SimpleNamespace(cfg=launch_cfg, rc=failed_cleanup)
        failed_seen = {}

        def failing_bubble_run(_argv, *, env):
            failed_seen["private_dir"] = Path(env["CLAUDE_CONFIG_DIR"])
            check(
                "exception path stages creds before launch",
                (failed_seen["private_dir"] / ".credentials.json").exists(),
                True,
            )
            raise RuntimeError("bubble launch failed")

        tc.agents.subprocess.run = failing_bubble_run
        propagated = False
        try:
            tc.run_in_bubble(failed_w, "review", "PROMPT", opts, inner_cmd="true", cred_model="claude")
        except RuntimeError:
            propagated = True
        check("bubble exception propagates", propagated, True)
        check("bubble exception removes the private seed", failed_seen["private_dir"].exists(), False)

        # Off macOS the launch must keep using the configured credential store; no private override.
        tc.sys.platform = "linux"
        linux_cleanup = Cleanup()
        linux_w = types.SimpleNamespace(cfg=launch_cfg, rc=linux_cleanup)
        linux_seen = {}

        def linux_bubble_run(_argv, *, env):
            linux_seen["config_dir"] = env["CLAUDE_CONFIG_DIR"]
            return types.SimpleNamespace(returncode=0)

        tc.agents.subprocess.run = linux_bubble_run
        linux_rc = tc.run_in_bubble(linux_w, "review", "PROMPT", opts, inner_cmd="true", cred_model="claude")
        check("Linux bubble launch succeeds", linux_rc, 0)
        check("Linux bubble env keeps the configured store", linux_seen["config_dir"], str(launch_cfgdir))
        tc.sys.platform = "darwin"
    finally:
        tc.agents.ensure_bubble_home = old_ensure_home
        tc.agents._bubble_pop = old_pop
        tc.agents._claude_keychain_creds_interactive = old_keychain
        tc.agents.subprocess.run = old_subprocess_run
        if old_cfgdir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_cfgdir
finally:
    tc.subprocess.run, tc.sys.platform = orig_run, orig_platform
    if orig_user is None:
        os.environ.pop("USER", None)
    else:
        os.environ["USER"] = orig_user

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

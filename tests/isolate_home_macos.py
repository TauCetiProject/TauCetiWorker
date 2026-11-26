#!/usr/bin/env python3
"""macOS isolation must not move $HOME.

Claude Code and gh both keep their credentials in the login Keychain, which `security` resolves through
$HOME. Repointing $HOME therefore made both unreachable: the pacer found no Claude creds and parked
every non-default worker in a 300s sleep for ever (kim-em/TauCetiWorker#135, Jeremy Kahn), and gh lost
its token (Bryan's report). It isolated nothing in exchange, because the Keychain is one per-login-user
store. So on macOS the isolation is $CLAUDE_CONFIG_DIR + $CODEX_HOME and $HOME is left alone; off macOS
$HOME still moves and the same two variables ride along.

This pins both platforms with a faked sys.platform and a faked worker home, touching no real Keychain.
Exit 0 = all assertions hold; 1 = a mismatch.
"""

import _isolated_state  # noqa: F401  (first: see tests/_isolated_state.py)

# isort: split
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

agents = tc.agents
codex_dir = tc.quota.codex_dir
claude_dir = tc.quota.claude_dir
fails = 0

ISOLATION_VARS = (
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "TAUCETI_KIRO_HOME",
    "TAUCETI_KIRO_DATA_DIR",
    "TAUCETI_KIRO_PROCESS_HOME",
    "TAUCETI_KIRO_XDG_DATA_HOME",
    "TAUCETI_DATA_HOME",
    "GH_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
)


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def isolate(platform, real, iso, inherited=None):
    """Run isolate_home for `platform` with a faked worker home, returning the resulting env.

    `inherited` is the environment a loop child would start with, so the early return is reachable:
    clearing everything first (as this helper used to) can only ever exercise a cold start."""
    saved_env = {k: os.environ.get(k) for k in ISOLATION_VARS}
    saved_platform, saved_iso_home = agents.sys.platform, agents._worker_iso_home
    try:
        for k in ISOLATION_VARS:
            os.environ.pop(k, None)
        os.environ.update(inherited or {})
        os.environ["HOME"] = str(real)
        agents.sys.platform = platform
        agents._worker_iso_home = lambda wid, _base=None: Path(iso)
        agents.isolate_home("worker1")
        return {k: os.environ.get(k) for k in ISOLATION_VARS}
    finally:
        agents.sys.platform, agents._worker_iso_home = saved_platform, saved_iso_home
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def seeded_real_home(root):
    """A plausible operator home: a codex credential to isolate, and a gh config to redirect to."""
    real = Path(root) / "real"
    (real / ".codex").mkdir(parents=True)
    (real / ".codex" / "auth.json").write_text('{"tokens": {"access_token": "operator"}}')
    (real / ".claude").mkdir(parents=True)
    (real / ".config" / "gh").mkdir(parents=True)
    # The test switches sys.platform after constructing this fixture, so seed
    # both platform-default locations with valid SQLite stores.
    for kiro in (
        real / ".local" / "share" / "kiro-cli",
        real / "Library" / "Application Support" / "kiro-cli",
    ):
        kiro.mkdir(parents=True)
        with sqlite3.connect(kiro / "data.sqlite3") as db:
            db.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO auth_kv VALUES ('login', 'operator')")
    return real


# --- macOS: $HOME stays put, the two config vars carry the isolation ------------------------------
with tempfile.TemporaryDirectory() as root:
    real, iso = seeded_real_home(root), Path(root) / "iso"
    env = isolate("darwin", real, iso)

    check("macOS leaves $HOME at the operator's", env["HOME"], str(real))
    check("macOS isolates CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"], str(iso / ".claude"))
    check("macOS isolates CODEX_HOME", env["CODEX_HOME"], str(iso / ".codex"))
    check("macOS isolates KIRO_HOME", env["TAUCETI_KIRO_HOME"], str(iso / ".kiro"))
    check(
        "macOS isolates Kiro native data",
        env["TAUCETI_KIRO_DATA_DIR"],
        str(iso / "Library" / "Application Support" / "kiro-cli"),
    )
    check("macOS redirects HOME only for Kiro", env["TAUCETI_KIRO_PROCESS_HOME"], str(iso))
    check("data root is exported", env["TAUCETI_DATA_HOME"], str(iso))
    check("codex credential is copied in", (iso / ".codex" / "auth.json").exists(), True)
    check("codex source marker recorded", (iso / ".codex" / ".tauceti-creds-source").read_text(), str(real / ".codex"))
    check(
        "Kiro credential database is snapshotted",
        (iso / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3").exists(),
        True,
    )

    # The redirect is what makes the isolation real: with $HOME still the operator's, a bare
    # <home>/.codex would resolve to the operator's own credential rather than the worker's copy.
    saved = {k: os.environ.get(k) for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR")}
    os.environ.update(CODEX_HOME=env["CODEX_HOME"], CLAUDE_CONFIG_DIR=env["CLAUDE_CONFIG_DIR"])
    try:
        check("codex_dir follows the redirect", codex_dir(real), iso / ".codex")
        check("claude_dir follows CLAUDE_CONFIG_DIR", claude_dir(real), iso / ".claude")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

# --- a loop child inherits isolation and must not redo it -----------------------------------------
with tempfile.TemporaryDirectory() as root:
    real, iso = seeded_real_home(root), Path(root) / "iso"
    child_env = isolate("darwin", real, iso)
    (iso / ".codex" / "auth.json").write_text("REPLACED-BY-A-REFRESH")

    env = isolate("darwin", real, iso, inherited={k: v for k, v in child_env.items() if v})
    check("re-isolation does not re-copy creds", (iso / ".codex" / "auth.json").read_text(), "REPLACED-BY-A-REFRESH")
    # The early return still reasserts the redirects: a child that lost one must not silently fall
    # back to the operator's account.
    check("early return reasserts CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"], str(iso / ".claude"))
    check("early return reasserts CODEX_HOME", env["CODEX_HOME"], str(iso / ".codex"))
    check("early return reasserts KIRO_HOME", env["TAUCETI_KIRO_HOME"], str(iso / ".kiro"))
    check(
        "early return reasserts Kiro native data",
        env["TAUCETI_KIRO_DATA_DIR"],
        str(iso / "Library" / "Application Support" / "kiro-cli"),
    )

# --- the sentinel, not any single redirect, decides "already isolated" ----------------------------
with tempfile.TemporaryDirectory() as root:
    real, iso = seeded_real_home(root), Path(root) / "iso"
    # An operator who exports exactly the Claude path we would have chosen, with no isolation done.
    # Returning here would leave codex on the operator's credential and create nothing.
    env = isolate(
        "darwin", real, iso, inherited={"CLAUDE_CONFIG_DIR": str(iso / ".claude"), "CODEX_HOME": "/somewhere/of/my/own"}
    )
    check("a pre-exported claude dir is not isolation", env["CODEX_HOME"], str(iso / ".codex"))
    # Codex's finding was that returning early creates neither directory, so that is what to assert:
    # a copied credential would only prove it for whichever source dir happened to be populated.
    check("setup really ran", (iso / ".claude").is_dir() and (iso / ".codex").is_dir(), True)

# --- Linux: unchanged, $HOME still moves ----------------------------------------------------------
with tempfile.TemporaryDirectory() as root:
    real, iso = seeded_real_home(root), Path(root) / "iso"
    env = isolate("linux", real, iso)

    check("Linux still moves $HOME", env["HOME"], str(iso))
    check("Linux isolates CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"], str(iso / ".claude"))
    check("Linux isolates CODEX_HOME", env["CODEX_HOME"], str(iso / ".codex"))
    check("Linux isolates KIRO_HOME", env["TAUCETI_KIRO_HOME"], str(iso / ".kiro"))
    check(
        "Linux isolates Kiro data",
        env["TAUCETI_KIRO_DATA_DIR"],
        str(iso / ".local" / "share" / "kiro-cli"),
    )
    check("Linux exports the same data root", env["TAUCETI_DATA_HOME"], str(iso))
    # gh and git config are redirected back at the operator's, since the moved $HOME has neither.
    check("Linux redirects GH_CONFIG_DIR", env["GH_CONFIG_DIR"], str(real / ".config" / "gh"))

# --- a custom $CODEX_HOME is the source we seed FROM ----------------------------------------------
# isolate_home repoints $CODEX_HOME at the worker's copy, so it must read the operator's ACTIVE codex
# login to make that copy, not the literal <home>/.codex. Seeding from the wrong directory and then
# repointing leaves the worker authenticated nowhere: the isolated dir has no auth.json and the
# operator's real one is no longer on the path anything looks at.
with tempfile.TemporaryDirectory() as root:
    real, iso = seeded_real_home(root), Path(root) / "iso"
    custom = Path(root) / "elsewhere" / "codex"
    custom.mkdir(parents=True)
    custom.joinpath("auth.json").write_text('{"tokens": {"access_token": "the-one-actually-in-use"}}')
    # The login the operator is really using lives only in the custom dir.
    (real / ".codex" / "auth.json").unlink()

    env = isolate("darwin", real, iso, inherited={"CODEX_HOME": str(custom)})

    check("CODEX_HOME is repointed at the worker copy", env["CODEX_HOME"], str(iso / ".codex"))
    check("the worker copy is not empty", (iso / ".codex" / "auth.json").exists(), True)
    check(
        "seeded from the custom dir",
        (iso / ".codex" / "auth.json").read_text(),
        '{"tokens": {"access_token": "the-one-actually-in-use"}}',
    )
    check("marker names the custom dir", (iso / ".codex" / ".tauceti-creds-source").read_text(), str(custom))

# --- the worker's data follows the WORKER, not $HOME ----------------------------------------------
# This is the migration hazard: the review store holds an outbox of unpublished records and the
# scoreboard/thread ids, so a path that moves means abandoned reviews and duplicate comments. Under
# the old behaviour $HOME *was* the isolation root, so pinning the data root to it reproduces the
# exact same paths on both platforms and nothing migrates.
with tempfile.TemporaryDirectory() as root:
    iso = Path(root) / "iso"
    saved = {k: os.environ.get(k) for k in ("TAUCETI_DATA_HOME", "HOME", "CLAIM_GITDIR_BASE")}
    try:
        os.environ.update(TAUCETI_DATA_HOME=str(iso), HOME=str(Path(root) / "real"))
        os.environ.pop("CLAIM_GITDIR_BASE", None)
        cfg = tc.Config.resolve("worker1")
        check("store follows the data root", cfg.store_dir.is_relative_to(iso), True)
        check("bubble home follows the data root", agents.bubble_home(cfg).is_relative_to(iso), True)
        check("claim scratch is per worker", Path(os.environ["CLAIM_GITDIR_BASE"]).is_relative_to(iso), True)
        check("login home is untouched", cfg.home, Path(root) / "real")

        # An unisolated worker (the 'default' id) has no sentinel: its data stays under the login
        # home, exactly where it has always been.
        os.environ.pop("TAUCETI_DATA_HOME", None)
        plain = tc.Config.resolve("default")
        check("unisolated data stays at $HOME", plain.data_home, Path(root) / "real")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

print("\nFAIL" if fails else "\nall macOS isolation checks passed")
sys.exit(1 if fails else 0)

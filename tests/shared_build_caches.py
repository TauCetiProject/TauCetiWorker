#!/usr/bin/env python3
"""How the Lean build caches are shared between workers, and how they are deliberately not.

The per-worker $HOME that isolate_home() creates for CREDENTIALS also made Mathlib's `.ltar` cache and
the elan toolchain directory per-worker, so on a five-worker fleet half of one week's 10.2 GB of
`.ltar` traffic was a file another worker on the same disk already had, and 22 toolchain installs
covered 6 distinct toolchains. Two different fixes, because the two caches are written differently:

  * toolchains are shared outright (`ELAN_HOME`), since an install takes a lock and lands by rename;
  * Mathlib's cache is NOT, because `lake exe cache get` takes no lock and older checkouts write
    fixed-name temporaries, so two workers in one directory can leave a corrupt `.ltar` under a name
    every later run trusts. It is pooled by hardlinking COMPLETE files instead, before the agent runs.
  * Lake's own store stays per-worker: it is written throughout a build, not once at install.

These assertions pin that split, the sentinel path a round child takes, and the pool exchange itself
(promote then hydrate, transient files never pooled, an existing name never redefined).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import build_caches

fails = 0

LOGIN_HOME = Path("/home/pretend-operator")
CACHE_VARS = ("ELAN_HOME", "MATHLIB_CACHE_DIR", "LAKE_CACHE_DIR")
ARTIFACT_VARS = ("LAKE_ARTIFACT_CACHE", "LAKE_RESTORE_ARTIFACTS")


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def clear(env):
    for var in (*CACHE_VARS, *ARTIFACT_VARS, "TAUCETI_DATA_HOME", "TAUCETI_MATHLIB_POOL", "XDG_CACHE_HOME"):
        env.pop(var, None)


def main():
    env = tc.agents.os.environ
    saved = {
        k: env.get(k)
        for k in (
            *CACHE_VARS,
            *ARTIFACT_VARS,
            "TAUCETI_DATA_HOME",
            "TAUCETI_MATHLIB_POOL",
            "XDG_CACHE_HOME",
            "TAUCETI_WORKER_ID",
            "CLAIM_GITDIR_BASE",
            "HOME",
        )
    }
    orig_host_home, orig_platform = tc.agents._host_home, tc.agents.sys.platform
    tc.agents._host_home = lambda: LOGIN_HOME
    data_home = Path("/srv/tauceti/state/worker1/home")
    try:
        # --- default Lake artifact policy ---------------------------------------------------------
        clear(env)
        tc.Config.resolve("worker1", home=LOGIN_HOME)
        check("Lake's local artifact cache is enabled by default", env["LAKE_ARTIFACT_CACHE"] == "1")
        check("cached artifacts are restored to the build directory by default", env["LAKE_RESTORE_ARTIFACTS"] == "1")

        clear(env)
        env["LAKE_ARTIFACT_CACHE"] = "0"
        env["LAKE_RESTORE_ARTIFACTS"] = "0"
        tc.Config.resolve("worker1", home=LOGIN_HOME)
        check("an explicit artifact-cache override wins", env["LAKE_ARTIFACT_CACHE"] == "0")
        check("an explicit restore override wins", env["LAKE_RESTORE_ARTIFACTS"] == "0")

        # --- what is shared, and what is not -------------------------------------------------------
        clear(env)
        resolved = tc.agents.share_build_caches("worker1", data_home)
        check("toolchains are pooled at the login user's ~/.elan", env["ELAN_HOME"] == "/home/pretend-operator/.elan")
        check("the Mathlib download target stays per-worker", Path(env["MATHLIB_CACHE_DIR"]).is_relative_to(data_home))
        check("Lake's own store stays per-worker", Path(env["LAKE_CACHE_DIR"]).is_relative_to(data_home))
        check("returns the resolved mapping (for the log line)", resolved == {v: env[v] for v in CACHE_VARS})

        # An operator's own values win — the opt-out for a host with caches elsewhere, and for tests.
        clear(env)
        env["ELAN_HOME"] = "/mnt/big/elan"
        env["MATHLIB_CACHE_DIR"] = "/mnt/big/mathlib"
        tc.agents.share_build_caches("worker1", data_home)
        check("operator ELAN_HOME preserved", env["ELAN_HOME"] == "/mnt/big/elan")
        check("operator MATHLIB_CACHE_DIR preserved", env["MATHLIB_CACHE_DIR"] == "/mnt/big/mathlib")

        # The pool follows the same order Mathlib's own tool uses, so it is the directory the
        # operator's interactive `cache get` already fills.
        check(
            "pool honours XDG_CACHE_HOME",
            build_caches.mathlib_pool(LOGIN_HOME, {"XDG_CACHE_HOME": "/xdg"}) == Path("/xdg/mathlib"),
        )
        check(
            "pool falls back to the login home",
            build_caches.mathlib_pool(LOGIN_HOME, {}) == LOGIN_HOME / ".cache" / "mathlib",
        )
        check(
            "an explicit pool override wins",
            build_caches.mathlib_pool(LOGIN_HOME, {"TAUCETI_MATHLIB_POOL": "/p"}) == Path("/p"),
        )

        # --- the already-isolated path, which is what a round child runs ---------------------------
        clear(env)
        tc.agents.sys.platform = "linux"
        home = tc.agents._worker_iso_home("worker1")
        env["TAUCETI_DATA_HOME"] = str(home)
        env["HOME"] = str(home)
        check("already-isolated path returns the same home", tc.agents.isolate_home("worker1") == home)
        check("already-isolated path pools the toolchains", env.get("ELAN_HOME") == str(LOGIN_HOME / ".elan"))
        check(
            "already-isolated path keeps the download target private",
            Path(env["MATHLIB_CACHE_DIR"]).is_relative_to(home),
        )
    finally:
        tc.agents._host_home, tc.agents.sys.platform = orig_host_home, orig_platform
        for k, v in saved.items():
            env.pop(k, None) if v is None else env.__setitem__(k, v)

    # --- the pool exchange -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        private, pool = root / "worker/mathlib", root / "pool/mathlib"
        private.mkdir(parents=True)
        pool.mkdir(parents=True)
        (private / "mine.ltar").write_bytes(b"mine")
        (private / "half.ltar.7.part").write_bytes(b"incomplete")  # a download in flight
        (private / "curl.cfg").write_bytes(b"url = ...")  # another process's file list
        (private / "curl-7.88.1").write_bytes(b"ELF")  # the tool's own static curl
        (pool / "theirs.ltar").write_bytes(b"theirs")
        promoted, hydrated = build_caches.sync_pool(private, pool)
        check("a finished artifact is promoted", (pool / "mine.ltar").exists())
        check("the pool's artifact is hydrated", (private / "theirs.ltar").exists())
        check("counts are reported", (promoted, hydrated) == (1, 1))
        for scratch in ("half.ltar.7.part", "curl.cfg", "curl-7.88.1"):
            check(f"{scratch} is never pooled", not (pool / scratch).exists())
        check(
            "pooling is by hardlink, not copy",
            (pool / "mine.ltar").stat().st_ino == (private / "mine.ltar").stat().st_ino,
        )
        # An existing name is never redefined: that is what stops one worker from replacing an
        # artifact another worker or the operator is already using, and it makes concurrent syncs safe.
        (private / "theirs.ltar").unlink()
        (private / "theirs.ltar").write_bytes(b"different bytes, same name")
        build_caches.sync_pool(private, pool)
        check("an existing pool entry is left alone", (pool / "theirs.ltar").read_bytes() == b"theirs")
        # A worker whose own copy differs keeps its own; the pool does not overwrite it either.
        check("the worker keeps its own divergent copy", (private / "theirs.ltar").read_bytes() != b"theirs")
        # Idempotent: a second sync of an unchanged pair does nothing.
        check("a repeated sync is a no-op", build_caches.sync_pool(private, pool) == (0, 0))
        # Different filesystems cannot be hardlinked; report nothing rather than silently copying.
        check("a cross-device pair links nothing", build_caches.link_into(private, Path("/proc/self")) == (0, 0))

    # --- Lake artifact cache lifetime --------------------------------------------------------------
    # Canonical main's toolchain pin, not a transient PR checkout, is the generation boundary.  The
    # first observation is deliberately non-destructive; only a later change clears the owned store.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkout, state, data_home = root / "checkout", root / "state", root / "home"
        checkout.mkdir()
        cache = data_home / ".cache" / "lake"
        cache.mkdir(parents=True)
        artifact = cache / "artifacts" / "old.olean"
        artifact.parent.mkdir()
        artifact.write_bytes(b"old")
        toolchain = checkout / "lean-toolchain"
        toolchain.write_text("leanprover/lean4:v4.34.0-rc1\n")
        cfg = types.SimpleNamespace(checkout=checkout, state=state, data_home=data_home)
        saved_lake = env.get("LAKE_CACHE_DIR")
        env["LAKE_CACHE_DIR"] = str(cache)
        messages = []
        original_log = tc.agents.log
        tc.agents.log = lambda message: messages.append(str(message))
        try:
            tc.agents.clean_lake_cache_after_toolchain_bump(cfg)
            check("first toolchain observation preserves an established Lake cache", artifact.exists())
            marker = state / "cache" / "lake-cache-toolchain.json"
            check("first observation records the canonical-main toolchain", marker.exists())

            tc.agents.clean_lake_cache_after_toolchain_bump(cfg)
            check("the same toolchain leaves the cache alone", artifact.exists())

            toolchain.write_text("leanprover/lean4:v4.34.0\n")
            tc.agents.clean_lake_cache_after_toolchain_bump(cfg)
            check("a canonical-main toolchain bump clears the owned Lake cache", not cache.exists())
            check("the bump cleanup is visible in the worker log", any("main toolchain changed" in m for m in messages))

            cache.mkdir(parents=True)
            current = cache / "current.olean"
            current.write_bytes(b"current")
            tc.agents.clean_lake_cache_after_toolchain_bump(cfg)
            check("the new toolchain is recorded, so cleanup happens only once", current.exists())

            external = root / "operator-cache"
            external.mkdir()
            keep = external / "keep.olean"
            keep.write_bytes(b"operator")
            env["LAKE_CACHE_DIR"] = str(external)
            toolchain.write_text("leanprover/lean4:v4.35.0\n")
            tc.agents.clean_lake_cache_after_toolchain_bump(cfg)
            check("an operator-managed Lake cache is never automatically deleted", keep.exists())
        finally:
            tc.agents.log = original_log
            if saved_lake is None:
                env.pop("LAKE_CACHE_DIR", None)
            else:
                env["LAKE_CACHE_DIR"] = saved_lake

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Rounds in per-round worktrees of one host clone save their source before anything is removed.

Local git fixtures only (no GitHub, no agents). Exit 0 = all assertions hold; 1 = a mismatch.
"""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import round_worktrees as rw  # noqa: E402
from tauceti_worker import work_recovery as wr  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def git(repo, *args, check_rc=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check_rc and p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


def identity(repo):
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(repo, "config", key, value)


def worker(root, wid):
    state = root / "workers" / wid
    state.mkdir(parents=True, exist_ok=True)
    (state / "round.lock").touch()
    return SimpleNamespace(wid=wid, state=state, checkout=state / "checkout", data_home=root / "home" / wid)


def records(cfg):
    base = cfg.state / "recovery"
    return [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []


def saved_commits(cfg):
    found = set()
    for record in records(cfg):
        manifest = json.loads((record / "manifest.json").read_text())
        found.update(manifest.get("unpublished") or [])
        found.update((manifest.get("tips") or {}).values())
    return found


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["TAUCETI_ACTIVITY_DIR"] = str(root / "activity")
        os.environ["TAUCETI_HOST_REPOSITORIES"] = str(root / "host")
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        identity(seed)
        (seed / ".gitignore").write_text(".lake/\n")
        (seed / "A.lean").write_text("theorem a : True := trivial\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        origin = root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
        agents = tc.agents
        agents.sync_mathlib_pool = lambda cfg: None
        agents.CHECKOUT_URL = str(origin)
        main_oid = git(origin, "rev-parse", "main").strip()

        # 1. An old permanent checkout is saved, then deleted; its .lake survives.
        one = worker(root, "w1")
        subprocess.run(["git", "clone", "-q", str(origin), str(one.checkout)], check=True)
        identity(one.checkout)
        (one.checkout / "A.lean").write_text("theorem legacy : True := trivial\n")
        git(one.checkout, "commit", "-qam", "unpublished legacy work")
        legacy = git(one.checkout, "rev-parse", "HEAD").strip()
        (one.checkout / ".lake/build").mkdir(parents=True)
        (one.checkout / ".lake/build/A.olean").write_text("warm build")
        os.environ["TAUCETI_ROUND_WORKTREES"] = "1"
        # A live round holds its worker's round lock, as RoundContext does.
        live = (one.state / "round.lock").open("a+")
        fcntl.flock(live, fcntl.LOCK_EX)
        check("first round prepares", agents.prepare_checkout(one, work="fix", pr=1))
        check("the checkout is now a worktree of the host clone", rw.owns(one.checkout))
        check("detached at origin/main", git(one.checkout, "rev-parse", "HEAD").strip() == main_oid)
        check("no branch is checked out", not git(one.checkout, "symbolic-ref", "-q", "HEAD", check_rc=False).strip())
        check("the old checkout's commit was saved first", legacy in saved_commits(one))
        check("warm .lake came along", (one.checkout / ".lake/build/A.olean").read_text() == "warm build")

        # 2. A commit made and then reset away is saved at round end; the worktree is removed.
        identity(one.checkout)
        (one.checkout / "A.lean").write_text("theorem lost : True := trivial\n")
        git(one.checkout, "commit", "-qam", "commit then reset away")
        lost = git(one.checkout, "rev-parse", "HEAD").strip()
        git(one.checkout, "reset", "-q", "--hard", main_oid)
        rw.release(one)
        check("round end removed the worktree", not one.checkout.exists())
        check("the reset-away commit was saved", lost in saved_commits(one))
        check(".lake waits between rounds", (rw.parked_lake(one) / "build/A.olean").is_file())
        check("the next round prepares again", agents.prepare_checkout(one, work="fix", pr=1))
        check(".lake is back in the new worktree", (one.checkout / ".lake/build/A.olean").is_file())

        # 3. Shared config that would act on every round is cleared; hooks point nowhere.
        clone = rw.host_clone()
        subprocess.run(["git", f"--git-dir={clone}", "config", "core.fsmonitor", "/tmp/evil"], check=True)
        (clone / "hooks").mkdir(exist_ok=True)
        (clone / "hooks/pre-commit").write_text("#!/bin/sh\nexit 1\n")
        subprocess.run(["git", f"--git-dir={clone}", "config", "remote.origin.url", "/tmp/a-fork"], check=True)
        subprocess.run(["git", f"--git-dir={clone}", "config", "gpg.program", "/tmp/evil"], check=True)
        planted = rw.root() / "no-hooks"
        planted.chmod(0o700)
        (planted / "post-checkout").write_text("#!/bin/sh\nexit 1\n")
        two = worker(root, "w2")
        crashed = (two.state / "round.lock").open("a+")
        fcntl.flock(crashed, fcntl.LOCK_EX)
        check("a second worker prepares its own worktree", agents.prepare_checkout(two, work="fix", pr=2))
        check("another live round's worktree is untouched", rw.owns(one.checkout))
        config = subprocess.run(["git", f"--git-dir={clone}", "config", "--list"], capture_output=True, text=True)
        check("fsmonitor was removed", "core.fsmonitor" not in config.stdout)
        check("hooks were removed", not (clone / "hooks/pre-commit").exists())
        check("the hooks target is empty again", not any(planted.iterdir()))
        check("gpg.program was removed", "gpg.program" not in config.stdout)
        check("origin is canonical again", f"remote.origin.url={origin}" in config.stdout)
        check("hooksPath points at an empty directory", "core.hookspath=" in config.stdout.lower())
        trees = [
            ln for ln in git(one.checkout, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")
        ]
        check("one host clone, two worktrees", len(trees) == 3)

        # 4. A crashed round's worktree is saved and removed once its round lock is free.
        identity(two.checkout)
        (two.checkout / "Crash.lean").write_text("unsaved work of a crashed round\n")
        if Path("/proc/locks").exists():
            rw.release(one)
            agents.prepare_checkout(one, work="fix", pr=1)
            check("a held round lock protects a live round's worktree", two.checkout.exists())
            crashed.close()  # the round dies without its cleanup
            rw.release(one)
            check("the next round prepares", agents.prepare_checkout(one, work="fix", pr=1))
            check("a crashed round's worktree is removed", not two.checkout.exists())
            check(
                "its unsaved file was saved first",
                any("Crash.lean" in json.loads((r / "manifest.json").read_text())["untracked"] for r in records(two)),
            )

        # 5. Recovery links from the host clone between rounds.
        rw.release(one)
        check("recovery links from the host clone between rounds", rw.recovery_source(one) == clone)

        # 6. Turning the flag off again returns to an ordinary checkout, keeping .lake.
        check("prepare again", agents.prepare_checkout(one, work="fix", pr=1))
        os.environ["TAUCETI_ROUND_WORKTREES"] = "0"
        check("flag off prepares", agents.prepare_checkout(one, work="fix", pr=1))
        check("an ordinary clone again", wr.checkout_kind(one.checkout) == "standalone")
        check(".lake kept across the switch", (one.checkout / ".lake/build/A.olean").is_file())
        live.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

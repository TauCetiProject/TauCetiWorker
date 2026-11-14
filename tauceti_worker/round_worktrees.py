"""Run each round in its own worktree of one host clone (opt-in per host).

With `TAUCETI_ROUND_WORKTREES=1`, a host keeps one bare clone of TauCeti (one object store and one
set of refs) and every round works in a linked worktree of it at the worker's ordinary checkout
path, `cfg.checkout`:

- `prepare()` saves any leftover worktree through the recovery guard (history-aware for worktrees,
  so a commit an agent made and then reset away is saved), fetches the host clone under the host
  lock, and adds a fresh worktree with a detached HEAD at `origin/main`. Nothing resets a shared
  `main`.
- `release()` runs when the round's context exits: it saves the worktree through the cleanup save
  path and removes it only when that save succeeded. A tree it cannot save is left for the next
  `prepare()`, which retains it whole if the guard cannot save it either.
- A crashed round's worktree is removed by the next `prepare()` on the host once no process holds
  that worker's round lock or uses the tree, again only after a successful save.
- An old permanent checkout is deleted on its worker's first round, after the same save; one that
  cannot be saved is moved aside whole by the existing retention path.

`.lake` (the worker's warm build state and Lake's dependency checkouts) is moved beside the worktree
between rounds instead of being deleted with it, so rounds keep today's warm builds.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
from pathlib import Path

from .config import log

FETCH_INTERVAL = 60
# Shared-config keys that could run code or redirect traffic for every round on the host. An agent
# writing `git config` in its worktree writes the shared file, so they are cleared at every round.
# core.hooksPath is not listed: it is set to an empty directory at every round instead.
UNSAFE_CONFIG = (
    r"^core\.(fsmonitor|sshcommand|pager|editor|askpass|gitproxy|worktree|attributesfile|excludesfile)$",
    r"^(include|includeif)\.",
    r"^filter\.",
    r"^diff\..*\.(textconv|command)$",
    r"^merge\..*\.driver$",
    r"^credential\.",
    r"^alias\.",
    r"^(uploadpack|protocol|url|http)\.",
)


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("TAUCETI_ROUND_WORKTREES", "").strip().lower() in {"1", "true", "yes", "on"}


def root() -> Path:
    """The host's repositories root: beside, never inside, the per-worker state directories."""
    from .config import worker_state_root

    return Path(os.environ.get("TAUCETI_HOST_REPOSITORIES") or worker_state_root().parent / "host-repositories")


def host_clone() -> Path:
    return root() / "TauCeti.git"


def is_clone(path: Path) -> bool:
    """A local Git repository to link from: a checkout, a worktree or the bare host clone."""
    return (path / ".git").exists() or (path / "HEAD").is_file() and (path / "objects").is_dir()


def recovery_source(cfg) -> Path:
    """What recovery hard-links its private clones from: the host clone once rounds use worktrees,
    since a worker's worktree exists only during its round."""
    if enabled() and is_clone(host_clone()) and not (cfg.checkout / ".git").exists():
        return host_clone()
    return cfg.checkout


def parked_lake(cfg) -> Path:
    """Where a worker's `.lake` waits between rounds (same filesystem, so a rename)."""
    return cfg.checkout.with_name(cfg.checkout.name + ".lake-between-rounds")


@contextlib.contextmanager
def host_lock():
    """Serialize clone, fetch, worktree add/remove/prune and config repair on the host clone."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root() / "TauCeti.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _git(*args, check=True, timeout=600):
    result = subprocess.run(
        ["git", f"--git-dir={host_clone()}", *map(str, args)], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result


def owns(path: Path) -> bool:
    """Whether `path` is a linked worktree of this host's clone."""
    from .work_recovery import _common_dir, checkout_kind

    if checkout_kind(path) != "worktree":
        return False
    try:
        return _common_dir(path).resolve() == host_clone().resolve()
    except Exception:  # noqa: BLE001 - an unreadable worktree is not ours to manage
        return False


def _ensure_clone(url):
    clone = host_clone()
    if (clone / "HEAD").is_file():
        return
    staging = clone.with_name(clone.name + f".tmp-{os.getpid()}")
    subprocess.run(["git", "clone", "--bare", "--quiet", "--", url, str(staging)], check=True, timeout=1800)
    git = ["git", f"--git-dir={staging}"]
    subprocess.run([*git, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], check=True)
    subprocess.run([*git, "fetch", "--quiet", "--prune", "origin"], check=True, timeout=1800)
    # No local branches: a branch can be checked out in only one worktree at a time.
    heads = subprocess.run(
        [*git, "for-each-ref", "--format=%(refname)", "refs/heads"], capture_output=True, text=True, check=True
    ).stdout.split()
    for ref in heads:
        subprocess.run([*git, "update-ref", "-d", ref], check=True)
    os.rename(staging, clone)
    log(f"worktrees: created the host clone {clone}")


def harden():
    """Clear shared-config keys and hooks that would act on every round; point hooks at nothing."""
    clone = host_clone()
    listed = _git("config", "--file", clone / "config", "--name-only", "--list", check=False).stdout.split()
    import re

    for key in sorted(set(listed)):
        if any(re.search(pattern, key.lower()) for pattern in UNSAFE_CONFIG):
            _git("config", "--file", clone / "config", "--unset-all", key, check=False)
            log(f"worktrees: removed shared Git config {key}")
    empty = root() / "no-hooks"
    empty.mkdir(exist_ok=True, mode=0o500)
    _git("config", "--file", clone / "config", "core.hooksPath", empty)
    hooks = clone / "hooks"
    if hooks.is_dir():
        for hook in hooks.iterdir():
            if not hook.name.endswith(".sample"):
                hook.unlink(missing_ok=True)


def _fetch():
    marker = host_clone() / "FETCH_HEAD"
    with contextlib.suppress(FileNotFoundError):
        if time.time() - marker.stat().st_mtime < FETCH_INTERVAL:
            return  # another worker's round fetched just now
    _git("fetch", "--quiet", "--prune", "origin")


def _lock_held(path: Path) -> bool:
    """Whether any process holds a lock on `path`, from /proc/locks; unknown counts as held."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return True
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[5].count(":") == 2:
            major, minor, inode = fields[5].split(":")
            with contextlib.suppress(ValueError):
                if int(inode) == info.st_ino and (int(major, 16), int(minor, 16)) == (
                    os.major(info.st_dev),
                    os.minor(info.st_dev),
                ):
                    return True
    return False


def _worktrees():
    paths = []
    for line in _git("worktree", "list", "--porcelain", check=False).stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            if path.resolve() != host_clone().resolve():
                paths.append(path)
    return paths


def _park(cfg, tree: Path):
    lake, parked = tree / ".lake", parked_lake(cfg)
    if lake.is_dir() and not lake.is_symlink():
        if parked.exists():
            _discard(parked)
        os.rename(lake, parked)


def _unpark(cfg, tree: Path):
    parked = parked_lake(cfg)
    if parked.is_dir() and not (tree / ".lake").exists():
        os.rename(parked, tree / ".lake")


def _discard(path: Path):
    from . import retention

    report = retention.fresh_report()
    retention.remove(path, path.parent, report, check_users=True)
    for error in report["errors"]:
        log(f"worktrees: deletion deferred: {error}")


def _remove(tree: Path, branch: str | None):
    _git("worktree", "remove", "--force", tree)
    if branch:
        # The round's PR branch lives in the shared refs; its source was saved above.
        _git("update-ref", "-d", branch, check=False)


def _save(cfg, tree: Path):
    """The cleanup save path: a durable record of anything unpublished, never a move or reset."""
    from .work_recovery import save_for_cleanup

    decision = save_for_cleanup(cfg, tree)
    if not decision.ok:
        log(f"worktrees: kept {tree}: {decision.reason}")
    elif decision.record is not None:
        log(f"worktrees: saved {tree} in {decision.record} before removing it")
    return decision.ok


def _branch(tree: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def sweep(current: Path):
    """Remove worktrees of crashed rounds: no lease, no user, and a successful save first."""
    from types import SimpleNamespace

    from .work_recovery import live_users

    _git("worktree", "prune", check=False)
    for tree in _worktrees():
        if tree.resolve() == current.resolve() or not tree.exists():
            continue
        state = tree.parent
        # Only a worker's own checkout path has a round lock beside it; anything else (a retained
        # tree, an unknown layout) is the retention collector's to judge.
        if not (state / "round.lock").is_file() or _lock_held(state / "round.lock") or live_users(tree):
            continue
        owner = SimpleNamespace(wid=state.name, state=state, checkout=tree, data_home=state / "home")
        if _save(owner, tree):
            branch = _branch(tree)
            with contextlib.suppress(OSError):
                _park(owner, tree)
            _remove(tree, branch)
            log(f"worktrees: removed the crashed round's worktree {tree}")


def prepare(cfg, url) -> bool:
    """Leave `cfg.checkout` a fresh worktree of the host clone, detached at `origin/main`."""
    from . import work_recovery

    co = cfg.checkout
    kind = work_recovery.checkout_kind(co) if co.exists() else None
    if kind == "standalone" or (kind == "worktree" and not owns(co)):
        # An old permanent checkout: save it, then delete it (legacy data is not migrated).
        if work_recovery.save_for_cleanup(cfg, co).ok:
            with contextlib.suppress(OSError):
                _park(cfg, co)
            _discard(co)
            if co.exists():
                log(f"worktrees: could not delete the old checkout {co}; retrying next round")
                return False
            log(f"worktrees: deleted the old permanent checkout {co} after saving it")
        elif not work_recovery.retain(cfg, co, "an old checkout could not be saved before deletion").ok:
            return False
    elif kind == "worktree":
        # A worktree that outlived its round (a crash): save it, history included, then replace it.
        decision = work_recovery.guard(cfg, co)
        if not decision.ok:
            log(f"worktrees: preparation refused: {decision.reason}")
            return False
    elif co.exists():
        if not work_recovery.retain(cfg, co, "the checkout has no git metadata").ok:
            return False
    try:
        with host_lock():
            _ensure_clone(url)
            harden()
            _fetch()
            if co.exists() and owns(co):
                branch = _branch(co)
                with contextlib.suppress(OSError):
                    _park(cfg, co)
                _remove(co, branch)
            sweep(co)
            co.parent.mkdir(parents=True, exist_ok=True)
            _git("worktree", "add", "--quiet", "--detach", co, "refs/remotes/origin/main")
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or ""
        log(f"worktrees: preparation failed: {error} {detail.strip()[-300:]}")
        return False
    _unpark(cfg, co)
    return True


def release(cfg) -> None:
    """At round end: save the worktree, then remove it. Never raises."""
    co = getattr(cfg, "checkout", None)
    try:
        if not enabled() or co is None or not co.exists() or not owns(co):
            return
        if not _save(cfg, co):
            return
        branch = _branch(co)
        _park(cfg, co)
        with host_lock():
            _remove(co, branch)
    except Exception as error:  # noqa: BLE001 - round cleanup must not fail the round
        log(f"worktrees: release deferred ({type(error).__name__}: {error}); the next round saves it")


def retire(cfg) -> bool:
    """With the flag turned off again: save and remove this worker's worktree, keeping `.lake`.

    Returns False when the worktree could not be saved; it is then left for the retention path.
    """
    co = cfg.checkout
    try:
        if not owns(co):
            return True
        if not _save(cfg, co):
            return False
        branch = _branch(co)
        _park(cfg, co)
        with host_lock():
            _remove(co, branch)
        log(f"worktrees: retired {co}; this worker uses an ordinary checkout again")
        return True
    except (OSError, subprocess.SubprocessError) as error:
        log(f"worktrees: could not retire {co}: {error}")
        return False


def restore_parked(cfg) -> None:
    """Give an ordinary checkout back the `.lake` its worker's last worktree round left."""
    with contextlib.suppress(OSError):
        _unpark(cfg, cfg.checkout)

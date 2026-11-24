"""Run each round in its own worktree of one host clone (on unless a host opts out).

Unless `TAUCETI_ROUND_WORKTREES=0`, a host keeps one bare clone of TauCeti (one object store and one
set of refs) and every round works in a linked worktree of it at the worker's ordinary checkout
path, `cfg.checkout`:

- `prepare()` removes any leftover worktree, fetches the host clone under the host lock, and adds a
  fresh worktree with a detached HEAD at `origin/main`. Nothing resets a shared `main`.
- `release()` runs when the round's context exits and removes the worktree.
- A crashed round's worktree is removed by the next `prepare()` on the host once no process holds
  that worker's round lock or uses the tree.
- An old permanent checkout is deleted on its worker's first round.

Rounds are all-or-nothing: whatever an agent pushed (through git-safe-push) is on the remote, and
anything unpushed is discarded with the worktree.

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
from .excerpt import ends

# Every checkout this process prepared (worker and source checkouts), released at round end.
PREPARED = []
# The host clone each prepared checkout is a worktree of. A source checkout is prepared while its
# source is active, so `host_clone()` names another repository's clone by the time the round ends.
CLONES: dict[Path, Path] = {}
FETCH_INTERVAL = 60
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
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
    r"^diff\.external$",
    r"^gpg\.",
    r"^sequence\.editor$",
    # Per-worktree config files would escape this check; keep them disabled.
    r"^extensions\.worktreeconfig$",
)


def enabled(env=None) -> bool:
    """Per-round worktrees are the default; `TAUCETI_ROUND_WORKTREES=0` (or false/no/off) opts a host out."""
    env = os.environ if env is None else env
    return env.get("TAUCETI_ROUND_WORKTREES", "").strip().lower() not in {"0", "false", "no", "off"}


def root() -> Path:
    """The host's repositories root: beside, never inside, the per-worker state directories."""
    from .config import worker_state_root

    return Path(os.environ.get("TAUCETI_HOST_REPOSITORIES") or worker_state_root().parent / "host-repositories")


def host_clone(url=None) -> Path:
    """One bare clone per repository identity (GitHub and Forgejo sources never share one)."""
    import hashlib

    from .agents import checkout_url

    identity = hashlib.sha256((url or checkout_url()).strip().rstrip("/").removesuffix(".git").encode())
    return root() / f"TauCeti-{identity.hexdigest()[:12]}.git"


def is_clone(path: Path) -> bool:
    """A local Git repository to link from: a checkout, a worktree or the bare host clone."""
    return (path / ".git").exists() or (path / "HEAD").is_file() and (path / "objects").is_dir()


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


def owns(path: Path, clone: Path | None = None) -> bool:
    """Whether `path` is a linked worktree of this host's clone (or of `clone`)."""
    from .live_use import checkout_kind, common_dir

    if checkout_kind(path) != "worktree":
        return False
    try:
        return common_dir(path).resolve() == (clone or host_clone()).resolve()
    except Exception:  # noqa: BLE001 - an unreadable worktree is not ours to manage
        return False


def _ensure_clone(url):
    clone = host_clone()
    if (clone / "HEAD").is_file():
        return
    staging = clone.with_name(clone.name + f".tmp-{os.getpid()}")
    subprocess.run(["git", "clone", "--bare", "--quiet", "--", url, str(staging)], check=True, timeout=1800)
    git = ["git", f"--git-dir={staging}"]
    subprocess.run([*git, "config", "remote.origin.fetch", FETCH_REFSPEC], check=True)
    subprocess.run([*git, "fetch", "--quiet", "--prune", "origin"], check=True, timeout=1800)
    # No local branches: a branch can be checked out in only one worktree at a time.
    heads = subprocess.run(
        [*git, "for-each-ref", "--format=%(refname)", "refs/heads"], capture_output=True, text=True, check=True
    ).stdout.split()
    for ref in heads:
        subprocess.run([*git, "update-ref", "-d", ref], check=True)
    os.rename(staging, clone)
    log(f"worktrees: created the host clone {clone}")


def harden(url):
    """Clear shared-config keys and hooks that would act on every round; point hooks at nothing.

    `origin` is reset to the canonical repository and fetch mapping, so an agent that repoints it
    cannot change every later round's `origin/main`.
    """
    clone = host_clone()
    _git("config", "--file", clone / "config", "remote.origin.url", url)
    _git("config", "--file", clone / "config", "--replace-all", "remote.origin.fetch", FETCH_REFSPEC)
    listed = _git("config", "--file", clone / "config", "--name-only", "--list", check=False).stdout.split()
    import re

    for key in sorted(set(listed)):
        if any(re.search(pattern, key.lower()) for pattern in UNSAFE_CONFIG):
            _git("config", "--file", clone / "config", "--unset-all", key, check=False)
            log(f"worktrees: removed shared Git config {key}")
    empty = root() / "no-hooks"
    if empty.is_symlink() or empty.is_file():
        empty.unlink()
    elif empty.is_dir() and any(empty.iterdir()):
        import shutil

        empty.chmod(0o700)
        shutil.rmtree(empty)
    empty.mkdir(exist_ok=True, mode=0o500)
    empty.chmod(0o500)
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


def _discard(path: Path, why: str = "a leftover tree") -> bool:
    from .agents import discard_checkout

    return discard_checkout(path, why)


class InUse(OSError):
    """A process still uses the worktree, or a mount lies inside it: it is not removed."""


def _check_unused(tree: Path) -> None:
    """Raise ``InUse`` while a live process uses ``tree`` or a mount lies inside it."""
    from .live_use import live_users
    from .paths import canonical_candidate
    from .retention import mount_points

    users = live_users(tree)
    mounts = mount_points()
    target = canonical_candidate(tree)
    if users or mounts is None or any(point == target or point.is_relative_to(target) for point in mounts):
        raise InUse(f"{tree} is in use ({', '.join(users) or 'a mount inside it, or mounts unobservable'})")


def _remove(tree: Path, common: Path | None = None):
    """Remove a worktree and anything unpushed in it. Branches stay: another round may own one.

    Never while a live process uses the tree or a mount lies inside it (``InUse``)."""
    _check_unused(tree)
    git = ["git", f"--git-dir={common or host_clone()}"]
    subprocess.run([*git, "worktree", "remove", "--force", str(tree)], check=True, capture_output=True, text=True)


def _round_lock(tree: Path) -> Path | None:
    """The round lock of the worker whose checkout this is: beside it, or above a source checkout
    (`state/sources/<hash>/checkout`). None for any other layout."""
    for parent in list(tree.parents)[:4]:
        if (parent / "round.lock").is_file():
            return parent / "round.lock"
    return None


def sweep(current: Path):
    """Remove worktrees of crashed rounds, holding their worker's round lock."""
    from types import SimpleNamespace

    from .live_use import live_users

    _git("worktree", "prune", check=False)
    for tree in _worktrees():
        if tree.resolve() == current.resolve() or not tree.exists():
            continue
        lock = _round_lock(tree)
        if lock is None:
            continue  # an unknown layout: not ours to remove
        with lock.open("a+") as stream:
            try:
                # Held through the removal, so no new round can take this checkout now.
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # a live round
            if live_users(tree):
                continue
            state = lock.parent
            owner = SimpleNamespace(wid=state.name, state=state, checkout=tree, data_home=state / "home")
            try:
                _check_unused(tree)
                with contextlib.suppress(OSError):
                    _park(owner, tree)
                _remove(tree)
            except InUse as error:
                log(f"worktrees: kept the crashed round's worktree for now: {error}")
                continue
            log(f"worktrees: removed the crashed round's worktree {tree}")


def prepare(cfg, url) -> bool:
    """Leave `cfg.checkout` a fresh worktree of the host clone, detached at `origin/main`."""
    from .live_use import checkout_kind

    co = cfg.checkout
    kind = checkout_kind(co) if co.exists() or co.is_symlink() else None
    if kind == "standalone" or (kind == "worktree" and not owns(co)):
        # An old permanent checkout, or a worktree of another clone: delete it (never migrated),
        # unless other linked worktrees still use its Git store.
        from .live_use import dependent_worktrees

        dependents = dependent_worktrees(co)
        if dependents:
            log(f"worktrees: kept the old checkout {co}: linked worktrees use it ({', '.join(dependents)})")
            return False
        with contextlib.suppress(OSError):
            _park(cfg, co)
        if not _discard(co, "an old checkout replaced by per-round worktrees"):
            return False
    elif kind is None and (co.exists() or co.is_symlink()):
        if not _discard(co, "the checkout has no git metadata"):
            return False
    try:
        with host_lock():
            _ensure_clone(url)
            harden(url)
            _fetch()
            if co.exists() and owns(co):
                _check_unused(co)
                with contextlib.suppress(OSError):
                    _park(cfg, co)
                _remove(co)
            sweep(co)
            co.parent.mkdir(parents=True, exist_ok=True)
            base = os.environ.get("TAUCETI_BASE_BRANCH", "main")
            _git("worktree", "add", "--quiet", "--detach", co, f"refs/remotes/origin/{base}")
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or ""
        log(f"worktrees: preparation failed: {error} {ends(detail, 300)}")
        return False
    _unpark(cfg, co)
    PREPARED.append(cfg)
    CLONES[Path(co).resolve()] = host_clone()
    return True


def release(cfg) -> None:
    """At round end: remove each worktree this round prepared. Never raises."""
    state = Path(getattr(cfg, "state", "/nonexistent")).resolve()

    def own(other):
        # Only this worker's checkouts: its own, or a source checkout under its state.
        return other is cfg or Path(other.checkout).resolve().is_relative_to(state)

    configs = [cfg, *(c for c in PREPARED if c is not cfg and own(c))]
    PREPARED[:] = [c for c in PREPARED if c is not cfg and not own(c)]
    seen = set()
    for current in configs:
        co = getattr(current, "checkout", None)
        if co is None or co in seen:
            continue
        seen.add(co)
        # The clone it was prepared from: a source checkout is not a worktree of the default clone,
        # and one left behind is swept by another worker's round while holding this worker's lock.
        clone = CLONES.pop(Path(co).resolve(), None)
        try:
            if not enabled() or not co.exists() or not owns(co, clone):
                continue
            _check_unused(co)
            _park(current, co)
            with host_lock():
                _remove(co, clone)
        except Exception as error:  # noqa: BLE001 - round cleanup must not fail the round
            log(f"worktrees: release deferred ({type(error).__name__}: {error}); the next round removes it")


def retire(cfg) -> bool:
    """With the flag turned off again: remove this worker's worktree, keeping `.lake`.

    Any linked worktree counts, whatever host root it came from: its own common directory is the
    evidence.
    """
    from .live_use import checkout_kind, common_dir

    co = cfg.checkout
    try:
        if not co.exists() or checkout_kind(co) != "worktree":
            return True
        common = common_dir(co)
        _check_unused(co)
        _park(cfg, co)
        with host_lock():
            _remove(co, common)
        log(f"worktrees: retired {co}; this worker uses an ordinary checkout again")
        return True
    except (OSError, subprocess.SubprocessError) as error:
        log(f"worktrees: could not retire {co}: {error}")
        return False


def restore_parked(cfg) -> None:
    """Give an ordinary checkout back the `.lake` its worker's last worktree round left."""
    with contextlib.suppress(OSError):
        _unpark(cfg, cfg.checkout)


def local_branch(cfg, pr) -> str:
    """A PR branch name no other worker's worktree can hold (branches are shared by worktrees)."""
    return f"tauceti/{cfg.wid}/pr-{pr}"

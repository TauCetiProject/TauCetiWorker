"""Run each round in a worktree of one host clone (on unless a host opts out).

Unless `TAUCETI_ROUND_WORKTREES=0`, a host keeps one bare clone of TauCeti (one object store and one
set of refs) and every round works in a linked worktree of it at the worker's ordinary checkout
path, `cfg.checkout`. Each worker keeps its worktree (one per host clone it uses) across rounds:

- `prepare()` fetches the host clone under the host lock, then, outside the lock, resets the
  worker's worktree to a detached HEAD at `origin/main`: it clears a killed round's Git state,
  forces a checkout (only changed files are written) and deletes every untracked file except
  `.lake`. A first round, or a worktree that cannot be reset, gets a fresh worktree under the lock.
  Nothing resets a shared `main`.
- `release()` runs when the round's context exits. The worktree stays for the worker's next round.
- An old permanent checkout is deleted on its worker's first round.

Rounds are all-or-nothing: whatever an agent pushed (through git-safe-push) is on the remote, and
anything unpushed is discarded when the worker's next round resets the worktree.

`TAUCETI_PERSISTENT_WORKTREES=0` returns to a fresh worktree per round: `release()` removes it and
parks `.lake` beside it, and `prepare()` removes crashed rounds' worktrees on the host once no
process holds that worker's round lock or uses the tree.
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
    # A sparse checkout would let a reset skip files; persistent worktrees must get every file.
    r"^core\.sparsecheckout(cone)?$",
    r"^index\.sparse$",
)


def persistent(env=None) -> bool:
    """Workers keep their worktrees across rounds unless `TAUCETI_PERSISTENT_WORKTREES=0`."""
    env = os.environ if env is None else env
    return env.get("TAUCETI_PERSISTENT_WORKTREES", "").strip().lower() not in {"0", "false", "no", "off"}


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
    """Remove worktrees of crashed rounds, holding their worker's round lock.

    With persistent worktrees every worker's tree outlives its rounds, so only Git's records of
    trees that no longer exist (a retired worker's, deleted by retention) are pruned.
    """
    from types import SimpleNamespace

    from .live_use import live_users

    _git("worktree", "prune", check=False)
    if persistent():
        return
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


# Per-worktree Git state a killed round can leave behind; a forced checkout would keep it.
STALE_STATE = (
    "index.lock",
    "HEAD.lock",
    "MERGE_HEAD",
    "MERGE_MSG",
    "MERGE_MODE",
    "AUTO_MERGE",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "BISECT_EXPECTED_REV",
    "BISECT_TERMS",
    "BISECT_NAMES",
    "BISECT_ANCESTORS_OK",
    "ORIG_HEAD",
    "MERGE_AUTOSTASH",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "info/sparse-checkout",
)
# In the worker's state, beside the checkout's other records: when each checkout was last reset.
RESETS = "worktree-resets.json"


def _admin(tree: Path) -> Path:
    """The worktree's own directory in the host clone (`worktrees/<id>`), registered to this tree.

    Both directions must agree: `tree/.git` names the directory, and the directory's `gitdir`
    names `tree/.git`. A `.git` repointed at another worker's registration is refused.
    """
    if tree.is_symlink() or (tree / ".git").is_symlink() or not (tree / ".git").is_file():
        raise OSError(f"{tree} is not a plain linked worktree")
    result = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    admin = Path(result.stdout.strip())
    if admin.parent.name != "worktrees" or admin.parent.parent.resolve() != host_clone().resolve():
        raise OSError(f"{tree} is not a linked worktree of {host_clone()} ({admin})")
    try:
        backlink = Path((admin / "gitdir").read_text().strip())
    except OSError as error:
        raise OSError(f"{admin} has no gitdir: {error}") from None
    if os.path.realpath(backlink) != os.path.realpath(tree / ".git"):
        raise OSError(f"{admin} is registered to {backlink}, not {tree}")
    return admin


def _flagged(tree: Path) -> bool:
    """Whether the index marks any file skip-worktree or assume-unchanged (a checkout skips those)."""
    result = subprocess.run(["git", "-C", str(tree), "ls-files", "-v", "-z"], capture_output=True, timeout=600)
    if result.returncode:
        return True
    return any(entry[:1] == b"S" or entry[:1].islower() for entry in result.stdout.split(b"\0") if entry)


def _scrub(tree: Path, since_ns: int | None) -> None:
    """Remove nested Git directories, and every file changed since the last reset.

    A file whose inode changed after `since_ns` (ctime, which no one can set back) is deleted, so
    the checkout writes it afresh whatever the index claims about it. `.lake` is the worker's
    build state and is kept; symlinks are removed, never followed.
    """
    import shutil

    for base, dirs, files in os.walk(tree, followlinks=False):
        here = Path(base)
        if here == tree:
            dirs[:] = [d for d in dirs if d != ".lake"]
            files = [f for f in files if f != ".git"]
        for name in [d for d in dirs if d == ".git" or (here / d).is_symlink()]:
            path = here / name
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
            dirs.remove(name)
        for name in files:
            path = here / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if name == ".git" or since_ns is None or info.st_ctime_ns > since_ns:
                path.unlink()


def _reset(tree: Path, commit: str, resets: Path) -> None:
    """Make an idle worktree a clean detached checkout of `commit`, keeping `.lake`.

    Outside the host lock: a worktree's HEAD, index and files are its own, and the commit was
    fetched under the lock. The caller holds the worker's round lock, so no other round of this
    worker can be using it, and `_check_unused` covers anything else.

    Nothing of the previous round is trusted: its Git state goes, files it changed are deleted
    before the checkout, and an index with skipped files (or a tree with no reset on record) is
    rebuilt from `commit`, writing every file.
    """
    import shutil

    from .runtime_status import read_json

    _check_unused(tree)
    admin = _admin(tree)
    lake = tree / ".lake"
    if lake.is_symlink() or (lake.exists() and not lake.is_dir()):
        lake.unlink()  # Only a real directory is the worker's build state; never write through a link.
    for name in STALE_STATE:
        path = admin / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    since = read_json(resets).get(str(tree))
    since = since if isinstance(since, int) else None
    rebuild = since is None or _flagged(tree)
    _scrub(tree, since)
    git = ["git", "-C", str(tree)]
    if rebuild:
        (admin / "index").unlink(missing_ok=True)
        steps = (("update-ref", "--no-deref", "HEAD", commit), ("reset", "-q", "--hard", commit))
    else:
        steps = (("checkout", "-q", "-f", "--detach", commit),)
    for args in (*steps, ("clean", "-ffdxq", "-e", "/.lake")):
        result = subprocess.run([*git, *args], capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    _record_reset(tree, resets)


def _record_reset(tree: Path, resets: Path) -> None:
    from .runtime_status import atomic_json, read_json

    rows = read_json(resets)
    rows = rows if isinstance(rows, dict) else {}
    rows[str(tree)] = time.time_ns()
    atomic_json(resets, rows)


def prepare(cfg, url) -> bool:
    """Leave `cfg.checkout` a clean worktree of the host clone, detached at `origin/main`."""
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
    base = os.environ.get("TAUCETI_BASE_BRANCH", "main")
    asked = time.monotonic()
    waited = 0.0
    reused = False

    resets = Path(cfg.state) / RESETS

    def add():
        if co.exists() or co.is_symlink():
            _check_unused(co)
            with contextlib.suppress(OSError):
                _park(cfg, co)
            try:
                if not owns(co):
                    raise OSError("not a worktree of this clone")
                _remove(co)
            except (OSError, subprocess.SubprocessError) as error:
                # A damaged registration `worktree remove` cannot find: delete the tree itself.
                if not _discard(co, f"a worktree that could not be removed ({error})"):
                    raise OSError(f"could not delete {co}") from error
            _git("worktree", "prune", check=False)
        co.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "--quiet", "--detach", co, f"refs/remotes/origin/{base}")
        if persistent():
            _record_reset(co, resets)

    try:
        with host_lock():
            waited = time.monotonic() - asked
            _ensure_clone(url)
            harden(url)
            _fetch()
            sweep(co)
            commit = _git("rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}").stdout.strip()
            reuse = persistent() and co.exists() and owns(co)
            if not reuse:
                add()
        if reuse:
            try:
                _reset(co, commit, resets)
                reused = True
            except InUse:
                raise
            except (OSError, subprocess.SubprocessError) as error:
                detail = getattr(error, "stderr", "") or ""
                log(f"worktrees: could not reset {co} ({error} {ends(detail, 300)}); adding a fresh worktree")
                again = time.monotonic()
                with host_lock():
                    waited += time.monotonic() - again
                    add()
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or ""
        log(f"worktrees: preparation failed: {error} {ends(detail, 300)}")
        return False
    _unpark(cfg, co)
    log(
        f"worktrees: {'reset' if reused else 'added'} {co.name} at {commit[:12]} "
        f"(host lock wait {waited:.1f}s, total {time.monotonic() - asked:.1f}s)"
    )
    PREPARED.append(cfg)
    CLONES[Path(co).resolve()] = host_clone()
    return True


def release(cfg) -> None:
    """At round end: forget each worktree this round prepared; without persistence, remove it. Never raises."""
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
            if not enabled() or persistent() or not co.exists() or not owns(co, clone):
                continue  # A persistent worktree waits, with its `.lake`, for this worker's next round.
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

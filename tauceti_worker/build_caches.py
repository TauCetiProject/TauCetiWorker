"""tauceti_worker.build_caches — pooling the Lean build caches across workers.

Mathlib's `.ltar` cache and the elan toolchain directory hold artifacts every worker fetches
identically, but the per-worker `$HOME` that isolate_home() creates for CREDENTIALS made both
per-worker too. Measured on a five-worker fleet: of one week's 10.2 GB of `.ltar` traffic, 5.2 GB was
a file another worker on the same disk already had, and 22 toolchain installs covered 6 distinct
toolchains.

The two caches are pooled differently, because they are written differently.

`elan` is pooled by pointing every worker at one `ELAN_HOME`. Installing a toolchain takes a
per-toolchain lock, unpacks into a temporary directory and renames it into place, so two workers
racing for the same toolchain is safe. `LAKE_CACHE_DIR` is deliberately kept per-worker even so: it
normally lives under the toolchain directory, it is written during builds rather than once at
install, and nothing here has established that concurrent writers are safe.

Mathlib's `.ltar` cache is NOT pooled by pointing workers at one directory, which was this module's
first design and was wrong. `lake exe cache get` takes no lock, and until every checkout runs a
Mathlib new enough to carry per-process temporary names (leanprover-community/mathlib4#42752), two
concurrent runs in one directory share `curl.cfg` and share each `<hash>.ltar.part`. The bad outcome
is not a failed round: it is a corrupt `.ltar` left under a name the cache trusts forever after, for
every worker AND the operator, until someone deletes it by hand. That risk peaks exactly when the
pool would pay off most — just after a Mathlib bump, when several workers fetch the same 8,600 files
within minutes of each other.

So each worker keeps downloading into its own directory, and the pool is a hardlink farm that only
ever gains COMPLETE files. Before each round, `sync_pool` promotes the worker's finished `.ltar`s
into the pool and hydrates the worker's directory with everything the pool has that it lacks. The
worker then downloads only what nobody on the machine has, having written nothing another process
can see mid-write: a hardlink is atomic, and a name that exists is never overwritten. Disk is
unchanged by all this, since a link is not a copy.
"""

from __future__ import annotations

import os
from pathlib import Path

# Aggregate work is bounded by how many names differ between pool and worker, which after the first
# sync is a few thousand at most. The first sync of an established worker is the expensive one.
__all__ = ["elan_pool", "hydrate_only", "link_into", "mathlib_pool", "sync_pool", "transient", "walk"]


def transient(rel: Path) -> bool:
    """Is this a live `cache get`'s scratch file rather than a finished artifact?

    A `.part` is a download in flight, a `curl*.cfg` is the file list handed to curl, and
    `curl-<version>` is the statically linked curl the tool downloads for itself when the system one
    is too old. None is content a pool should hold: the first two belong to whichever process is
    running right now, and pooling a half-written one under a final name is the exact failure this
    module exists to avoid."""
    name = rel.name
    return name.endswith(".part") or (name.startswith("curl") and (name.endswith(".cfg") or "-" in name))


def walk(root: Path, skip=None):
    """Every poolable path under *root*, relative to it. Symlinks are never descended.

    *skip* is an optional extra predicate on the relative path, for callers with their own notion of
    what does not belong in a pool — the toolchain migration uses it to leave Lake's mutable
    per-toolchain store alone. It must be applied to LINKING as well as to deleting: pooling a
    mutable file shares its inode, so a later in-place write reaches every worker holding the link."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames + filenames):
            p = here / name
            rel = p.relative_to(root)
            if (p.is_symlink() or p.is_file()) and not transient(rel) and not (skip and skip(rel)):
                yield rel


def same_device(a: Path, b: Path) -> bool:
    """Do *a* and *b* live on one filesystem? Walks up to the nearest existing ancestor of each."""

    def dev(p: Path) -> int:
        for candidate in (p, *p.parents):
            try:
                if candidate.exists():
                    return candidate.stat().st_dev
            except OSError:
                return -1
        return -1

    return dev(a) == dev(b) != -1


def link_into(src: Path, dst: Path, skip=None) -> tuple[int, int]:
    """Hardlink every finished file under *src* into *dst*, never replacing what is already there.

    Returns (linked, already present). An existing name is left strictly alone: that is what keeps
    one worker from redefining an artifact another worker or the operator is already using, and it
    makes concurrent callers safe, since `link` either creates the name or fails with EEXIST. A
    symlink is recreated rather than followed, so the target is not dragged in under the wrong name.
    """
    linked = present = 0
    if not src.is_dir() or not same_device(src, dst):
        return (0, 0)
    for rel in walk(src, skip):
        source, target = src / rel, dst / rel
        if target.exists() or target.is_symlink():
            present += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
            else:
                os.link(source, target)
            linked += 1
        except FileExistsError:  # a concurrent sync won the race, which is the outcome we wanted
            present += 1
        except OSError:
            # A file that vanished mid-walk (a `cache clean`, say) or a permission fault is not worth
            # failing a round over: the worker simply downloads that artifact again.
            continue
    return (linked, present)


def mathlib_pool(host_home: Path, env: dict[str, str] | None = None) -> Path:
    """Where the machine keeps its shared `.ltar`s.

    Explicit overrides remain external/operator-owned. The default lives inside TauCeti's
    owned build-cache pool so its collector can reclaim it without touching interactive caches.
    """
    env = os.environ if env is None else env
    explicit = env.get("TAUCETI_MATHLIB_POOL") or env.get("MATHLIB_CACHE_DIR")
    if explicit:
        return Path(explicit)
    managed = Path(env.get("TAUCETI_BUILD_CACHE_ROOT") or host_home / ".cache/tauceti/builds")
    return managed / "mathlib"


def elan_pool(host_home: Path, env: dict[str, str] | None = None) -> Path:
    """Where the machine keeps its shared toolchains: an explicit `ELAN_HOME`, else the login user's."""
    env = os.environ if env is None else env
    return Path(env["ELAN_HOME"]) if env.get("ELAN_HOME") else host_home / ".elan"


def hydrate_only(pool: Path, private: Path) -> tuple[int, int]:
    """Give *private* a link to everything *pool* holds that it lacks. See `sync_pool`."""
    private.mkdir(parents=True, exist_ok=True)
    return link_into(pool, private)


def sync_pool(private: Path, pool: Path) -> tuple[int, int]:
    """Exchange finished artifacts between one worker's cache and the machine pool.

    Promote first, then hydrate: the worker's own downloads reach the pool before it takes the pool's,
    so a name it just fetched is not immediately re-linked from someone else's copy. Returns
    (promoted, hydrated). Both directions only ADD names, so this is safe to run while other workers
    are syncing; it must not run while THIS worker's agent is downloading, which is why the round
    calls it before starting the agent rather than alongside it."""
    if not private.is_dir():
        private.mkdir(parents=True, exist_ok=True)
    pool.mkdir(parents=True, exist_ok=True)
    promoted, _ = link_into(private, pool)
    hydrated, _ = link_into(pool, private)
    return (promoted, hydrated)

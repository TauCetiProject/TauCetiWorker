"""Bound idle repair pools to recent, reusable Lake snapshots, and give every batch a pool of its own.

Each task's artifact pool (``lake-cache``, with the Mathlib cache and download views beside it) is
its batch's own, ``<cache root>/batches/<batch id>`` (decision 202 a): PR code running in the build
sandbox can write the pool, so a pool shared across batches would let one batch's code feed the
outputs another batch's final verification restores. A batch pool starts empty and is filled only
by the worker's fetches from the public and repair caches and by the batch's own builds. The worker
records every repair-cache revision restored into it (``seeds.jsonl``, beside the pool and out of
the sandbox's reach), which the attestation reports, so the controller can refuse outputs seeded
from another batch's unlanded tip. A finished batch's pool is removed by collection.

The root lease covers attempts and background publishers. Collection never runs with a task
worktree present, including attempts started before cache leases were introduced. Parked batch
trees do not hold it back: they keep their own links to (or copies of) their outputs, and Lake
promotes a collected output back into the pool from the tree.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from tauceti_worker.expanded_cache import HASH, atomic_json

MAX_BYTES = 32 * 2**30
MAX_AGE = 7 * 86400
SNAPSHOTS = 2
ARTIFACT = re.compile(r"[0-9a-f]{16}(?:\.(?:ltar|olean(?:\.server|\.private)?|ilean|ir(?:\.sig)?|c|bc|o))?")
REVISION = re.compile(r"[0-9a-f]{40}")


@contextlib.contextmanager
def cache_lease(root: Path, *, exclusive: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("repair root must not be a symlink")
    fd = os.open(root / "cache.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


BATCHES = "batches"  # under a cache root: one pool root per batch, named by its controller ID
BATCH_MARKER = "batch-pool.json"  # in a batch pool root: {"batch_id"}
SEEDS = "seeds.jsonl"  # in a batch pool root: the repair-cache revisions restored into its pool


def batch_pool(base: Path, batch_id: str | int) -> Path:
    """The pool root of ``batch_id`` under the cache root ``base``."""
    if not re.fullmatch(r"[1-9][0-9]*", str(batch_id)):
        raise ValueError("batch pools require a controller batch ID")
    return base.absolute() / BATCHES / str(batch_id)


def task_cache(root: Path, task_id: str | int, batch_id: str | int | None = None) -> Path:
    """Bind a task once; changing the fleet default cannot move a running attempt.

    A new binding is the pool of the task's batch (``batch_pool``) under ``TAUCETI_QUEUE_CACHE_ROOT``,
    or under the worker's own root when none is configured. Callers that only look a binding up may
    omit ``batch_id``; creating one without it is refused, so no task falls back to a shared pool."""
    if not re.fullmatch(r"[1-9][0-9]*", str(task_id)):
        raise ValueError("cache bindings require a controller task ID")
    binding = root / "tasks" / str(task_id) / "cache-root.json"
    if binding.is_symlink():
        raise ValueError("symlink task cache binding")
    if binding.exists():
        return Path(_record(binding)["root"])
    if (root / "worktrees" / f"task-{task_id}").exists():
        # Existing attempts predate bindings and still use their private pool.
        selected = root.absolute()
    else:
        if batch_id is None:
            raise ValueError(f"task {task_id} has no cache binding and no batch to bind it to")
        configured = os.environ.get("TAUCETI_QUEUE_CACHE_ROOT")
        selected = batch_pool(Path(configured) if configured else root, batch_id)
        _mark_batch_pool(selected, str(batch_id))
    binding.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(binding, {"root": str(selected)})
    register(selected, root)
    return selected


def _mark_batch_pool(pool_root: Path, batch_id: str) -> None:
    for directory in (pool_root.parent, pool_root):
        if directory.is_symlink():
            raise ValueError(f"symlink batch pool directory: {directory}")
    pool_root.mkdir(parents=True, exist_ok=True)
    marker = pool_root / BATCH_MARKER
    if marker.is_symlink():
        raise ValueError("symlink batch pool marker")
    if marker.exists():
        if str(_record(marker).get("batch_id")) != batch_id:
            raise ValueError(f"{pool_root} is another batch's pool")
        return
    atomic_json(marker, {"batch_id": batch_id})


def pool_batch(pool_root: Path) -> str | None:
    """The batch whose own pool ``pool_root`` is, or None for a shared (pre-202) pool."""
    marker = pool_root / BATCH_MARKER
    if pool_root.parent.name != BATCHES or marker.is_symlink() or not marker.is_file():
        return None
    batch_id = str(_record(marker).get("batch_id", ""))
    return batch_id if batch_id == pool_root.name and re.fullmatch(r"[1-9][0-9]*", batch_id) else None


def _pool_root(env: dict) -> Path | None:
    return Path(env["LAKE_CACHE_DIR"]).parent if env.get("LAKE_CACHE_DIR") else None


def seeded_packages(worktree: Path, env: dict) -> bool:
    """Whether ``worktree``'s dependencies are the worker's read-only seed packages: the one target the
    worker recorded linking, inside a configured read-only tree. Only then does an online fetch load no
    Lake configuration or executable that candidate code could have written."""
    from tauceti_queue import repair_sandbox

    recorded = repair_sandbox.linked_packages(worktree)
    return len(recorded) == 1 and any(
        recorded[0].is_relative_to(tree) for tree in repair_sandbox._read_only_trees({**os.environ, **env})
    )


def record_seed(env: dict, service: str, revision: str, *, packages: str) -> None:
    """Note an online fetch into the task's batch pool: ``service``'s outputs for ``revision`` were (or may
    have been) restored, with ``packages`` ``"seed"`` (read-only seed packages) or ``"private"`` (candidate-
    writable ones, whose configurations and Mathlib's cache executable the fetch loaded). A fetch with no
    single revision (Mathlib's ``lake exe cache get``) is recorded with an empty one.

    Only the worker writes the ledger: it sits beside the pool, which the sandbox binds, never inside it."""
    pool_root = _pool_root(env)
    if pool_root is None or pool_batch(pool_root) is None or not (revision == "" or REVISION.fullmatch(revision)):
        return
    ledger = pool_root / SEEDS
    row = {"service": service, "revision": revision, "packages": packages, "at": round(time.time(), 3)}
    fd = os.open(ledger, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def pool_identity(env: dict) -> dict:
    """What the attestation says about the pool the verification restored from: ``{"scope": "batch",
    "batch_id", "seeds": [{"service", "revision", "packages"}, ...]}`` for a batch pool (seeds in first-restored
    order, without repeats), ``{"scope": "shared"}`` for any other, ``{"scope": "none"}`` without one."""
    pool_root = _pool_root(env)
    if pool_root is None:
        return {"scope": "none"}
    try:
        batch_id = pool_batch(pool_root)
        if batch_id is None:
            return {"scope": "shared"}
        seeds: list[dict] = []
        ledger = pool_root / SEEDS
        if ledger.is_symlink():
            raise ValueError("symlink seed ledger")
        for line in ledger.read_text().splitlines() if ledger.exists() else ():
            row = json.loads(line)
            seed = {
                "service": str(row["service"]),
                "revision": str(row["revision"]),
                "packages": str(row.get("packages", "private")),
            }
            if seed not in seeds:
                seeds.append(seed)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"scope": "unknown", "error": str(error)}
    return {"scope": "batch", "batch_id": batch_id, "seeds": seeds}


def register(pool_root: Path, worker_root: Path):
    if pool_root == worker_root:
        return
    with cache_lease(pool_root):
        directory = pool_root / "cache-users"
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():
            raise ValueError("symlink cache participant directory")
        key = hashlib.sha256(str(worker_root.absolute()).encode()).hexdigest()
        path = directory / (key + ".json")
        if not path.exists():
            atomic_json(path, {"root": str(worker_root.absolute())})


def participants(root: Path) -> list[Path]:
    return [root, *(Path(_record(p)["root"]) for p in _files(root / "cache-users", "*.json"))]


def _files(directory: Path, pattern: str):
    if directory.is_symlink():
        raise ValueError(f"symlink cache directory: {directory}")
    paths = list(directory.glob(pattern))
    checked = {directory}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unsafe cache entry: {path}")
        parent = path.parent
        while parent not in checked:
            if parent.is_symlink():
                raise ValueError(f"symlink cache directory: {parent}")
            checked.add(parent)
            parent = parent.parent
    return paths


def _record(path: Path) -> dict:
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise ValueError(f"invalid Lake cache record: {path}")
    return record


def _artifacts(data) -> set[str]:
    if isinstance(data, str):
        if not ARTIFACT.fullmatch(data):
            raise ValueError(f"invalid artifact name: {data!r}")
        return {data}
    if isinstance(data, list):
        return set().union(*(_artifacts(value) for value in data))
    if isinstance(data, dict):
        # Lake's native description has several independent output facets; `m` is a flag.
        if set(data) - {"o", "i", "rs", "r", "c", "b", "l", "m"}:
            raise ValueError("unknown Lake output facets")
        return set().union(*(_artifacts(value) for key, value in data.items() if key != "m"))
    raise ValueError("invalid Lake artifact description")


def _transfer(view: Path, pool: Path, env: dict) -> None:
    from tauceti_queue import repair_sandbox
    from tauceti_worker.expanded_cache import transfer

    if repair_sandbox.disabled(env):
        transfer(view, pool)
        return
    common = Path(os.path.commonpath([view.resolve(), pool.resolve()]))
    code = "from pathlib import Path\nfrom tauceti_worker.expanded_cache import transfer\ntransfer(Path(sys.argv[1]), Path(sys.argv[2]))"
    # Writable: only the pool and the downloads directory; the users' registrations and every other
    # entry beside them are read-only, so a link planted in the pool cannot redirect a write there.
    writable = {p.resolve().relative_to(common).parts[0] for p in (view, pool) if p.resolve() != common}
    argv = repair_sandbox.sandboxed_python(
        code, [str(view.resolve()), str(pool.resolve())], common, env, writable=tuple(writable)
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=1800, check=False)
    if done.returncode:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise OSError(f"cache transfer exited {done.returncode}: {detail[-1] if detail else 'no output'}")


def _symlink_in(directory: Path) -> Path | None:
    """The first symlink at or below ``directory``, never following one."""
    if directory.is_symlink():
        return directory
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in (*directories, *files):
            if (Path(parent) / name).is_symlink():
                return Path(parent) / name
    return None


def _mapping(path: Path) -> dict[str, set[str]]:
    lines = path.read_text().splitlines()
    if not lines or not isinstance(json.loads(lines[0]), str):
        raise ValueError(f"invalid Lake snapshot header: {path}")
    entries = {}
    for line in lines[1:]:
        row = json.loads(line)
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not HASH.fullmatch(row[0]):
            raise ValueError(f"invalid Lake snapshot entry: {path}")
        entries[row[0]] = _artifacts(row[1])
    return entries


def _uses_cache(worker_root: Path, task_id: str, cache_root: Path | None) -> bool:
    if cache_root is None or worker_root == cache_root:
        return True
    binding = worker_root / "tasks" / task_id / "cache-root.json"
    # Pre-migration attempts have no binding and use their own private pool.
    return binding.exists() and Path(_record(binding)["root"]) == cache_root


def _busy(store, root: Path, now: float, *, cache_root: Path | None = None) -> bool:
    trees = root / "worktrees"
    if trees.is_symlink():
        return True
    for tree in trees.iterdir() if trees.exists() else ():
        if re.fullmatch(r"batch-[1-9][0-9]*", tree.name):
            # A parked batch tree (repair_cleanup) runs nothing; a task moves it to its own path, under
            # the cache lease, before using it.
            continue
        match = re.fullmatch(r"task-([1-9][0-9]*)(?:-[A-Za-z0-9_.-]+)?", tree.name)
        if not match or _uses_cache(root, match[1], cache_root):
            return True
    tasks = root / "tasks"
    if tasks.is_symlink():
        return True
    claimed = (
        {
            str(row[0])
            for row in store.execute("SELECT task_id FROM claims WHERE released_at IS NULL AND expires_at>?", (now,))
        }
        if store is not None
        else set()
    )
    if any((tasks / task_id).exists() and _uses_cache(root, task_id, cache_root) for task_id in claimed):
        return True
    for publication in _files(tasks, "*/logs/**/publish.json"):
        if not _uses_cache(root, publication.relative_to(tasks).parts[0], cache_root):
            continue
        record = _record(publication)
        if record.get("state") == "running" and now - float(record.get("started_at", now)) < 120:
            return True
    return False


def _collect(root: Path, *, apply: bool, now: float, max_bytes: int, max_age: float) -> dict:
    pool = root / "lake-cache"
    if pool.is_symlink():
        raise ValueError("symlink Lake pool")
    artifacts = {p.name: p for p in _files(pool / "artifacts", "*")}
    if any(not ARTIFACT.fullmatch(name.removesuffix(".tmp")) for name in artifacts):
        raise ValueError("unrecognized Lake artifact; refusing partial collection")
    sizes = {name: path.stat().st_size for name, path in artifacts.items()}
    mappings = _files(pool / "revisions", "*/*.jsonl")
    candidates = [(p.stat().st_mtime, p.parent.name, p, False) for p in mappings]
    # Previous successful publishers did not save their new maps under revisions/. Treat
    # their manifests as snapshots too, so deploying retention cannot discard new outputs.
    publications = [p for source in participants(root) for p in _files(source / "tasks", "*/logs/**/publish.json")]
    for publication in publications:
        record = _record(publication)
        mapping = publication.with_name("repair-outputs.jsonl")
        if record.get("published") and REVISION.fullmatch(str(record.get("revision", ""))) and mapping.exists():
            if mapping.is_symlink():
                raise ValueError("symlink publication manifest")
            candidates.append((publication.stat().st_mtime, "TauCeti", mapping, True))
    outputs = _files(pool / "outputs", "*/*.json")
    output_index = {(p.parent.name, p.stem): p for p in outputs}
    keep_maps, keep_outputs, referenced = set(), set(), set()
    counts: dict[str, int] = {}
    seen: dict[tuple, bool] = {}
    for stamp, scope, mapping, published in sorted(candidates, reverse=True):
        if now - stamp > max_age or counts.get(scope, 0) >= SNAPSHOTS:
            continue
        entries = _mapping(mapping)
        # Publication and fetched snapshots can describe the very same revision.
        identity = (scope, tuple(sorted((key, tuple(sorted(names))) for key, names in entries.items())))
        if identity in seen:
            if not published and not seen[identity]:
                keep_maps.add(mapping)
                seen[identity] = True
            continue
        names, records = set().union(*entries.values()), set()
        for key in entries:
            path = output_index.get((scope, key))
            if path is None:
                continue
            description = _record(path)
            payloads = _artifacts(description["data"])
            # Incomplete native records must be fetched again, rather than advertising a hit.
            if payloads and payloads <= artifacts.keys():
                names.update(payloads)
                records.add(path)
        names.intersection_update(artifacts)
        if sum(sizes[name] for name in referenced | names) > max_bytes:
            continue
        seen[identity] = not published
        referenced.update(names)
        keep_outputs.update(records)
        counts[scope] = counts.get(scope, 0) + 1
        if not published:
            keep_maps.add(mapping)
    # Dependencies restored without a revision manifest still have reusable input hashes.
    # Retain their recent complete records, newest first, within the same total budget.
    mapped_scopes = {scope for _, scope, _, _ in candidates}
    dependency_records = [p for p in outputs if p.parent.name not in mapped_scopes and p.parent.name != "TauCeti"]
    retained_size = sum(sizes[name] for name in referenced)
    for path in sorted(dependency_records, key=lambda p: p.stat().st_mtime, reverse=True):
        if now - path.stat().st_mtime > max_age:
            continue
        payloads = _artifacts(_record(path)["data"])
        if not payloads or not payloads <= artifacts.keys():
            continue
        extra = sum(sizes[name] for name in payloads - referenced)
        if retained_size + extra <= max_bytes:
            retained_size += extra
            referenced.update(payloads)
            keep_outputs.add(path)
    remove = [p for name, p in artifacts.items() if name not in referenced]
    stale_outputs = [p for p in outputs if p not in keep_outputs]
    stale_maps = [p for p in mappings if p not in keep_maps]
    freed = sum(p.stat().st_size for p in remove if p.stat().st_nlink == 1)
    result = dict(
        state="collected" if apply else "eligible",
        artifacts_removed=len(remove),
        bytes_unlinked=sum(sizes[p.name] for p in remove),
        bytes_freed=freed,
        retained_bytes=sum(sizes[n] for n in referenced),
        outputs_removed=len(stale_outputs),
        snapshots_removed=len(stale_maps),
        snapshots_retained=sum(counts.values()),
        max_bytes=max_bytes,
    )
    # Build and validate the complete plan before touching any files. Remove indexes first;
    # a crash leaves extra payloads to collect next time, never an index to deleted data.
    if apply:
        for path in [*stale_maps, *stale_outputs, *remove]:
            path.unlink()
        # A killed fetch cannot leave permanent scratch space. The exclusive lease and
        # owner checks above mean no live attempt can own one of these temporary views.
        staging = root / "cache-downloads"
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
    return result


FINISHED = ("landed", "removed")  # batch states whose pool nothing will use again


def batch_pools(root: Path) -> list[Path]:
    """The batch pool roots under the cache root ``root``."""
    directory = root / BATCHES
    if directory.is_symlink():
        raise ValueError(f"symlink batch pool directory: {directory}")
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if re.fullmatch(r"[1-9][0-9]*", p.name))


def _collect_root(store, root: Path, *, apply, now, max_bytes, max_age, batch_id: str | None = None) -> dict:
    """Collect one pool root under its exclusive lease. A batch pool whose batch has finished is removed
    whole; any other idle pool is trimmed to its retention budget."""
    result: dict = {"root": str(root)}
    if batch_id is not None:
        result["batch_id"] = int(batch_id)
    try:
        if root.is_symlink():
            raise ValueError("symlink pool root")
        with cache_lease(root, exclusive=True) as acquired:
            if not acquired or any(_busy(store, source, now, cache_root=root) for source in participants(root)):
                result["state"] = "busy"
                return result
            if batch_id is not None:
                row = store.execute("SELECT state FROM batches WHERE id=?", (int(batch_id),)).fetchone()
                if row is None:
                    result["state"] = "unknown"  # another controller's batch, or not yet visible: keep it
                    return result
                if row[0] in FINISHED:
                    if {p.name for p in root.iterdir()} <= set(POOL_IDENTITY):
                        result["state"] = "absent"
                        return result
                    result.update(state="removed" if apply else "eligible", reason="batch-finished")
                    if apply:
                        _remove_pool(root)
                    return result
            if batch_id is not None and not (root / "lake-cache").exists():
                result["state"] = "absent"
                return result
            result.update(_collect(root, apply=apply, now=now, max_bytes=max_bytes, max_age=max_age))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result.update(state="error", error=str(error))
    return result


# What removal keeps of a finished batch's pool root: its lease, its identity and its users' registrations.
POOL_IDENTITY = ("cache.lock", BATCH_MARKER, "cache-users")


def _remove_pool(root: Path) -> None:
    """Empty a finished batch's pool root under its exclusive lease. The lock file and the batch marker stay,
    so a process waiting on the lease, or one resuming with a binding to this root, finds the same lock and
    the same batch identity rather than a recreated, unmarked directory; the users' registrations stay so
    later collection still sees their attempts."""
    for entry in root.iterdir():
        if entry.name in POOL_IDENTITY:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def collect(
    database: Path, roots: list[Path], *, apply=False, now=None, max_bytes=MAX_BYTES, max_age=MAX_AGE
) -> list[dict]:
    """Collect each cache root, then every batch pool under it (``batch_pools``)."""
    now = time.time() if now is None else now
    results = []
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as store:
        for root in roots:
            results.append(_collect_root(store, root, apply=apply, now=now, max_bytes=max_bytes, max_age=max_age))
            try:
                pools = batch_pools(root)
            except (OSError, ValueError) as error:
                results.append({"root": str(root / BATCHES), "state": "error", "error": str(error)})
                continue
            for pool_root in pools:
                results.append(
                    _collect_root(
                        store,
                        pool_root,
                        apply=apply,
                        now=now,
                        max_bytes=max_bytes,
                        max_age=max_age,
                        batch_id=pool_root.name,
                    )
                )
    return results


def before_task(root: Path) -> dict:
    """Collect between attempts, so continuous claims cannot starve the minute timer.

    Called before taking the attempt's shared lease. Existing trees, publishers and
    the root lock protect every other user; this attempt has not opened the pool yet.
    A cleanup error is recorded but must not fail useful queue work.
    """
    result = {"root": str(root)}
    try:
        with cache_lease(root, exclusive=True) as acquired:
            if not acquired or any(_busy(None, source, time.time(), cache_root=root) for source in participants(root)):
                result["state"] = "busy"
            else:
                result.update(_collect(root, apply=True, now=time.time(), max_bytes=MAX_BYTES, max_age=MAX_AGE))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(state="error", error=str(error))
    return result


@contextlib.contextmanager
def downloads(env: dict):
    """Isolate stock Lake's fixed `.tmp` names; share completed immutable archives.

    Only the fetch view is disposable. Builds and publishers continue using the common
    pool. Transfer copies mutable indexes atomically and preserves valid native records.
    The attempt's shared cache lease covers this entire context.
    """
    pool = Path(env["LAKE_CACHE_DIR"]) if env.get("LAKE_CACHE_DIR") else None
    if pool is None or not (pool.parent / "cache-users").is_dir():
        yield env
        return
    from tauceti_worker.expanded_cache import link

    staging = pool.parent / "cache-downloads"
    staging.mkdir(exist_ok=True)
    if staging.is_symlink():
        raise ValueError("symlink cache download directory")
    with tempfile.TemporaryDirectory(dir=staging, prefix="fetch-") as temporary:
        view = Path(temporary)
        for path in _files(pool / "artifacts", "*.ltar"):
            link(path, view / "artifacts" / path.name)
        # Fetch only the requested revision maps. Copying every historical map through
        # the view would refresh their mtimes and defeat recent-snapshot retention.
        try:
            yield dict(env, LAKE_CACHE_DIR=str(view))
        finally:
            for partial in _files(view / "artifacts", "*.tmp"):
                partial.unlink()
            for mapping in _files(view / "revisions", "*/*.jsonl"):
                try:
                    _mapping(mapping)
                except (ValueError, TypeError):
                    mapping.unlink()
            # The fetch ran in the sandbox, which could write the view, and builds can write the pool:
            # copy nothing unless the view holds no symlink at all, and copy in a sandbox of its own
            # that sees only the two, so a link planted in either cannot reach the worker's files.
            if _symlink_in(view):
                raise ValueError(f"symlink in the cache download view {view}; nothing transferred")
            _transfer(view, pool, env)


def migrate(database: Path, roots: list[Path], shared: Path, *, apply=False) -> list[dict]:
    """Merge idle legacy pools by hardlink, then remove their redundant names.

    Workers using the destination can continue: every insertion is atomic and never
    replaces an existing Lake name. Existing source users exclude migration by lease.
    """
    results = []
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as store:
        for root in roots:
            result = {"root": str(root), "shared": str(shared)}
            try:
                if shared.resolve() == root.resolve():
                    raise ValueError("shared cache root must be separate from worker task roots")
                origin, destination = (root / "lake-cache").resolve(), (shared / "lake-cache").resolve()
                if origin == destination or origin in destination.parents or destination in origin.parents:
                    raise ValueError("legacy and shared cache directories must not overlap")
                register(shared, root)
                with cache_lease(root, exclusive=True) as acquired:
                    if not acquired or _busy(store, root, time.time()):
                        result["state"] = "busy"
                    elif not (root / "lake-cache").exists():
                        result["state"] = "absent"
                    else:
                        source = root / "lake-cache"
                        if source.is_symlink():
                            raise ValueError("symlink legacy cache")
                        paths = []
                        # Inventory directories separately: symlinked directories must never
                        # hide files from the walk, and no deletion starts until validation.
                        for base, dirs, files in os.walk(source, followlinks=False):
                            for name in [*dirs, *files]:
                                path = Path(base) / name
                                if path.is_symlink():
                                    raise ValueError(f"symlink legacy cache entry: {path}")
                            paths.extend(Path(base) / name for name in files)
                        if source.is_symlink() or any(not p.is_file() for p in paths):
                            raise ValueError("unsafe legacy cache")
                        result.update(state="migrated" if apply else "eligible", files=len(paths))
                        if apply:
                            with cache_lease(shared):
                                checked = {shared}
                                for path in paths:
                                    target = shared / "lake-cache" / path.relative_to(source)
                                    parent = shared
                                    for component in target.relative_to(shared).parts[:-1]:
                                        parent /= component
                                        if parent not in checked:
                                            if parent.is_symlink():
                                                raise ValueError(f"symlink shared cache directory: {parent}")
                                            parent.mkdir(exist_ok=True)
                                            checked.add(parent)
                                    if target.is_symlink():
                                        raise ValueError(f"symlink shared cache entry: {target}")
                                    try:
                                        os.link(path, target)
                                    except FileExistsError:
                                        if not target.is_file():
                                            raise ValueError(f"invalid shared cache entry: {target}") from None
                                shutil.rmtree(source)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                result.update(state="error", error=str(error))
            results.append(result)
    return results

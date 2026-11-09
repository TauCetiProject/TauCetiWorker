"""Bound idle repair pools to recent, reusable Lake snapshots.

The root lease covers attempts and background publishers. Collection never runs with a
worktree present, including attempts started before cache leases were introduced.
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


def task_cache(root: Path, task_id: str | int) -> Path:
    """Bind a task once; changing the fleet default cannot move a running attempt."""
    if not re.fullmatch(r"[1-9][0-9]*", str(task_id)):
        raise ValueError("cache bindings require a controller task ID")
    binding = root / "tasks" / str(task_id) / "cache-root.json"
    if binding.exists():
        return Path(_record(binding)["root"])
    configured = os.environ.get("TAUCETI_QUEUE_CACHE_ROOT")
    # Existing attempts predate bindings and still use their private pool.
    selected = root if (root / "worktrees" / f"task-{task_id}").exists() or not configured else Path(configured)
    selected = selected.absolute()
    binding.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(binding, {"root": str(selected)})
    register(selected, root)
    return selected


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


def _busy(store, root: Path, now: float) -> bool:
    trees = root / "worktrees"
    if trees.is_symlink() or (trees.exists() and any(trees.iterdir())):
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
    if any((tasks / task_id).exists() for task_id in claimed):
        return True
    for publication in _files(tasks, "*/logs/publish.json"):
        record = _record(publication)
        if record.get("state") == "running" and now - float(record.get("started_at", now)) < 120:
            return True
    return False


def _collect(root: Path, *, apply: bool, now: float, max_bytes: int, max_age: float) -> dict:
    pool = root / "lake-cache"
    if pool.is_symlink():
        raise ValueError("symlink Lake pool")
    artifacts = {p.name: p for p in _files(pool / "artifacts", "*")}
    if any(not ARTIFACT.fullmatch(name) for name in artifacts):
        raise ValueError("unrecognized Lake artifact; refusing partial collection")
    sizes = {name: path.stat().st_size for name, path in artifacts.items()}
    mappings = _files(pool / "revisions", "*/*.jsonl")
    candidates = [(p.stat().st_mtime, p.parent.name, p, False) for p in mappings]
    # Previous successful publishers did not save their new maps under revisions/. Treat
    # their manifests as snapshots too, so deploying retention cannot discard new outputs.
    publications = [p for source in participants(root) for p in _files(source / "tasks", "*/logs/publish.json")]
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
    return result


def collect(
    database: Path, roots: list[Path], *, apply=False, now=None, max_bytes=MAX_BYTES, max_age=MAX_AGE
) -> list[dict]:
    now = time.time() if now is None else now
    results = []
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as store:
        for root in roots:
            result = {"root": str(root)}
            try:
                with cache_lease(root, exclusive=True) as acquired:
                    if not acquired or any(_busy(store, source, now) for source in participants(root)):
                        result["state"] = "busy"
                    else:
                        result.update(_collect(root, apply=apply, now=now, max_bytes=max_bytes, max_age=max_age))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                result.update(state="error", error=str(error))
            results.append(result)
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
            if not acquired or any(_busy(None, source, time.time()) for source in participants(root)):
                result["state"] = "busy"
            else:
                result.update(_collect(root, apply=True, now=time.time(), max_bytes=MAX_BYTES, max_age=MAX_AGE))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(state="error", error=str(error))
    return result


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
                                for path in paths:
                                    target = shared / "lake-cache" / path.relative_to(source)
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    if target.is_symlink() or target.parent.is_symlink():
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

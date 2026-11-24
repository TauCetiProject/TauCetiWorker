"""Repair worktree lifetime: one persistent tree per batch, and collection of finished ones.

A task runs in ``worktrees/task-<id>``. When it ends, its tree is parked as ``worktrees/batch-<id>``
with its ``.lake``, and the batch's next task on this worker moves it back in (``adopt``), so that
task builds incrementally. Each worker has its own root, and a task takes the parked tree by
renaming it, so no two tasks ever share one.

Collection uses the colocated controller's read-only database. Worker attempts and publishers hold
shared per-task leases on task trees; collection takes an exclusive lease. The lease files live
outside disposable worktrees and are never unlinked. Unknown queue state, active claims, and live
owners preserve the tree. A parked tree is removed once its batch has landed or been removed.
Idle caches retain bounded recent snapshots. Logs, task records and Git refs are kept.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from tauceti_queue import repair
from tauceti_worker.expanded_cache import atomic_json

WORKTREE = re.compile(r"task-([1-9][0-9]*)(?:-[A-Za-z0-9_.-]+)?\Z")
BATCH_TREE = re.compile(r"batch-([1-9][0-9]*)\Z")
BATCH_RECORD = ".lake/tauceti-batch-tree.json"  # in a parked tree: the task that parked it
# Worker state a previous task left in its tree. The listings directory is excluded through the
# clone's info/exclude, so `git clean` keeps it (with its build lock and error list); the output map
# names the previous build.
STALE = (repair.LISTINGS_DIR, ".lake/outputs.jsonl", ".lake/publication-stage")
HANDOFF_SECONDS = 120


def batch_tree(root: Path, batch_id: str | int) -> Path | None:
    """The parked tree of a batch on this worker; None for a task without a controller batch ID."""
    batch_id = str(batch_id)
    return root / "worktrees" / f"batch-{batch_id}" if re.fullmatch(r"[1-9][0-9]*", batch_id) else None


def _move(clone: Path, source: Path, destination: Path) -> None:
    """Rename a worktree and repoint Git at it. ``rename`` fails when the destination exists, so two
    movers can never nest one tree inside another, as ``git worktree move`` into a directory would.
    The tree is locked in Git meanwhile, so another worker's ``git worktree prune`` on the shared
    clone keeps its registration while the recorded path is briefly missing."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    repair.git(clone, "worktree", "lock", "--reason", "moving", str(source))
    try:
        os.rename(source, destination)
    except OSError:
        repair.git(clone, "worktree", "unlock", str(source), check=False)
        raise
    repair.git(clone, "worktree", "repair", str(destination))
    repair.git(clone, "worktree", "unlock", str(destination))


def adopt(clone: Path, root: Path, batch_id: str | int, worktree: Path) -> str | None:
    """Move the batch's parked tree to the task's ``worktree``, keeping its ``.lake``.

    Returns the commit the tree was parked at, or None when there is no parked tree (the task then
    restores from the caches as a new tree does). A tree that cannot be moved is removed.
    """
    parked = batch_tree(root, batch_id)
    if parked is None or worktree.exists():
        return None
    with worktree_lease(root, parked.name, wait=True):  # waits for a collection or a park of this tree
        if parked.is_symlink() or not parked.is_dir():
            return None
        try:
            head = repair.git(parked, "rev-parse", "HEAD").strip()
            _move(clone, parked, worktree)
        except (OSError, RuntimeError) as error:
            print(f"[repair-cleanup] removing unusable batch tree {parked}: {error}", flush=True)
            repair.remove_worktree(clone, parked)
            return None
    for name in STALE:
        path = worktree / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    for operation in ("rebase", "am", "cherry-pick", "revert", "merge"):  # whatever a previous agent left running
        repair.git(worktree, operation, "--quit", check=False)
    return head


def retire(clone: Path, root: Path, batch_id: str | int, task_id: str | int, worktree: Path, *, keep: bool) -> str:
    """At the end of a task: park its tree as the batch's tree (``keep``), else remove it.

    Only a tree whose artifacts were prepared is kept. When the batch already has a parked tree
    (another task of it ran meanwhile), this one is removed. Returns ``parked`` or ``removed``.
    """
    parked = batch_tree(root, batch_id) if keep else None
    if parked is not None and worktree.is_dir() and not worktree.is_symlink():
        with worktree_lease(root, parked.name, wait=True):
            try:
                # The record moves with the tree, so a parked tree always names its task.
                record = worktree / BATCH_RECORD
                record.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(record, {"task_id": str(task_id)})
                _move(clone, worktree, parked)
                return "parked"
            except (OSError, RuntimeError) as error:
                print(f"[repair-cleanup] could not park {worktree} as {parked}: {error}", flush=True)
    repair.remove_worktree(clone, worktree)
    return "removed"


@contextlib.contextmanager
def worktree_lease(root: Path, task_id: str | int, *, exclusive: bool = False, wait: bool = False):
    """Shared ownership for an attempt/publisher; nonblocking exclusive ownership for collection.
    ``batch-<id>`` names a parked batch tree's lease: adoption and parking wait for it exclusively
    (``wait``), collection tries it without waiting."""
    task_id = str(task_id)
    if not re.fullmatch(r"(?:batch-)?[1-9][0-9]*", task_id):
        raise ValueError("worktree leases require a controller task ID")
    directory = root / "worktree-leases"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("worktree lease directory must not be a symlink")
    descriptor = os.open(directory / (task_id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(
                descriptor, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            )
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _record(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: record must be an object")
    return value


def collect(database: Path, clone: Path, roots: list[Path], *, apply: bool = False) -> list[dict]:
    """Remove only known terminal tasks/batches, rechecking lifecycle and claims under the lease."""
    results = []
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as store:
        store.row_factory = sqlite3.Row
        for root in roots:
            worktrees = root / "worktrees"
            if root.is_symlink() or worktrees.is_symlink():
                raise ValueError("repair roots and worktree directories must not be symlinks")
            if not worktrees.is_dir():
                continue
            for tree in sorted(worktrees.iterdir()):
                parked = BATCH_TREE.fullmatch(tree.name)
                if parked and not tree.is_symlink() and tree.is_dir():
                    result = {"path": str(tree), "batch_id": int(parked[1])}
                    try:
                        with worktree_lease(root, tree.name, exclusive=True) as acquired:
                            if not acquired:
                                result["state"] = "busy"
                            else:
                                _collect_batch(store, clone, root, tree, parked[1], result, apply=apply)
                    except (OSError, ValueError, KeyError, sqlite3.Error, RuntimeError) as error:
                        result.update(state="error", error=str(error))
                    results.append(result)
                    continue
                match = WORKTREE.fullmatch(tree.name)
                if not match or tree.is_symlink() or not tree.is_dir():
                    continue
                task_id = int(match[1])
                result = {"path": str(tree), "task_id": task_id}
                try:
                    with worktree_lease(root, task_id, exclusive=True) as acquired:
                        if not acquired:
                            result["state"] = "busy"
                        else:
                            _collect_one(store, clone, root, tree, task_id, result, apply=apply)
                except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                    result.update(state="error", error=str(error))
                results.append(result)
    return results


def _known_task(store, root: Path, task_id: int):
    """The controller's row for a task of this worker's checkpoint, or None when the identities differ."""
    task = store.execute(
        "SELECT t.id, t.batch_id, t.state, t.repository, b.state AS batch_state "
        "FROM tasks t JOIN batches b ON b.id=t.batch_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    checkpoint = _record(root / "tasks" / str(task_id) / "task.json").get("task", {})
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint task must be an object")
    # A local checkpoint must belong to this controller database, not a similarly numbered task
    # from a different queue. Unknown/malformed identity never authorizes deletion.
    if (
        task is None
        or str(checkpoint.get("task_id")) != str(task_id)
        or str(checkpoint.get("batch_id")) != str(task["batch_id"])
    ):
        return None
    binding = checkpoint.get("binding")
    if binding is not None:
        # Qualified native identities are compared in full, including the queue epoch.
        current = store.execute("SELECT task_binding FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        if binding != json.loads(current or "{}"):
            return None
    elif checkpoint.get("repository") != task["repository"]:
        return None
    return task


def _collect_batch(store, clone, root, tree, batch_id, result, *, apply):
    """A parked batch tree is removed once its batch has landed or been removed. Nothing uses a
    parked tree in place: a task moves it to its own path first, under the same lease."""
    if not tree.is_dir():
        result["state"] = "adopted"  # a task took it between the listing and the lease
        return
    record = tree / BATCH_RECORD
    task_id = str(_record(record).get("task_id", "")) if record.is_file() and not record.is_symlink() else ""
    task = _known_task(store, root, int(task_id)) if re.fullmatch(r"[1-9][0-9]*", task_id) else None
    if task is None or str(task["batch_id"]) != batch_id:
        result["state"] = "unknown"
        return
    if task["batch_state"] not in ("landed", "removed"):
        result["state"] = "unfinished"
        return
    result["reason"] = "batch-finished"
    if apply:
        repair.remove_worktree(clone, tree)
        if tree.exists() or tree.is_symlink():
            raise OSError(f"worktree removal incomplete: {tree}")
    result["state"] = "removed" if apply else "eligible"


def _collect_one(store, clone, root, tree, task_id, result, *, apply):
    task = _known_task(store, root, task_id)
    if task is None:
        result["state"] = "unknown"
        return
    finished = task["batch_state"] in ("landed", "removed")
    terminal = task["state"] in ("submitted", "superseded")
    if not (finished or terminal):
        result["state"] = "unfinished"
        return
    now = time.time()
    if store.execute(
        "SELECT 1 FROM claims WHERE task_id=? AND released_at IS NULL AND expires_at>? LIMIT 1", (task_id, now)
    ).fetchone():
        result["state"] = "claimed"
        return
    publications = (root / "tasks" / str(task_id) / "logs").glob("**/publish.json")
    for publication in publications:
        pub = _record(publication)
        # Cover both late publishers and immutable early snapshots during unit startup.
        # Once started the publisher holds this task's shared lease.
        if pub.get("state") == "running" and now - float(pub.get("started_at", now)) < HANDOFF_SECONDS:
            result["state"] = "publisher-starting"
            return
    result.update(batch_id=task["batch_id"], reason="batch-finished" if finished else "task-terminal")
    if apply:
        repair.remove_worktree(clone, tree)
        if tree.exists() or tree.is_symlink():
            raise OSError(f"worktree removal incomplete: {tree}")
    result["state"] = "removed" if apply else "eligible"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="local database, clone and repair roots (JSON)")
    parser.add_argument("--apply", action="store_true", help="remove eligible trees; default is a read-only preview")
    args = parser.parse_args()
    config = _record(args.config)
    results = collect(
        Path(config["database"]), Path(config["clone"]), [Path(p) for p in config["roots"]], apply=args.apply
    )
    from tauceti_queue import repair_cache

    roots = [Path(p) for p in config["roots"]]
    migrations = []
    if config.get("cache_root"):
        shared = Path(config["cache_root"])
        migrations = repair_cache.migrate(Path(config["database"]), roots, shared, apply=args.apply)
        cache_roots = [shared]
    else:
        cache_roots = roots
    caches = repair_cache.collect(Path(config["database"]), cache_roots, apply=args.apply)
    print(
        json.dumps({"apply": args.apply, "worktrees": results, "caches": caches, "cache_migrations": migrations}),
        flush=True,
    )
    return int(any(row["state"] == "error" for row in [*results, *caches, *migrations]))


if __name__ == "__main__":
    raise SystemExit(main())

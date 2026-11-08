"""Collect terminal repair worktrees using the colocated controller's read-only database.

Worker attempts and publishers hold shared per-task leases; collection takes an exclusive lease.
The lease files live outside disposable worktrees and are never unlinked. Unknown queue state,
active claims, and live owners preserve the tree. Logs, task records, Git refs and caches are kept.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from tauceti_queue import repair

WORKTREE = re.compile(r"task-([1-9][0-9]*)(?:-[A-Za-z0-9_.-]+)?\Z")
HANDOFF_SECONDS = 120


@contextlib.contextmanager
def worktree_lease(root: Path, task_id: str | int, *, exclusive: bool = False):
    """Shared ownership for an attempt/publisher; nonblocking exclusive ownership for collection."""
    task_id = str(task_id)
    if not re.fullmatch(r"[1-9][0-9]*", task_id):
        raise ValueError("worktree leases require a controller task ID")
    directory = root / "worktree-leases"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("worktree lease directory must not be a symlink")
    descriptor = os.open(directory / (task_id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH)
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


def _collect_one(store, clone, root, tree, task_id, result, *, apply):
    task = store.execute(
        "SELECT t.id, t.batch_id, t.state, t.repository, b.state AS batch_state "
        "FROM tasks t JOIN batches b ON b.id=t.batch_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    checkpoint = _record(root / "tasks" / str(task_id) / "task.json").get("task", {})
    # A local checkpoint must belong to this controller database, not a similarly numbered task
    # from a different queue. Unknown/malformed identity never authorizes deletion.
    if (
        task is None
        or str(checkpoint.get("task_id")) != str(task_id)
        or str(checkpoint.get("batch_id")) != str(task["batch_id"])
    ):
        result["state"] = "unknown"
        return
    binding = checkpoint.get("binding")
    if binding is not None:
        # Qualified native identities are compared in full, including the queue epoch.
        current = store.execute("SELECT task_binding FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        if binding != json.loads(current or "{}"):
            result["state"] = "unknown"
            return
    elif checkpoint.get("repository") != task["repository"]:
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
    publication = root / "tasks" / str(task_id) / "logs" / "publish.json"
    if publication.exists():
        pub = _record(publication)
        # Close the worker -> systemd publisher startup gap. Once started, the publisher holds
        # the same lease. A crashed publisher's old 'running' record cannot retain a tree forever.
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
    print(json.dumps({"apply": args.apply, "worktrees": results}), flush=True)
    return int(any(row["state"] == "error" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())

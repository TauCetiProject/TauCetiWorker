#!/usr/bin/env python3
"""Whether the queue controller is at a safe point for a restart (docs/queue/operations.md, "Install and update").

    python3 -I services/queue/safepoint.py DATABASE

Reads the controller's database read-only and prints what is in flight. Exit 0: nothing a restart
would cut off; 1: busy, with a `busy:` line per operation; 2: the database cannot be read (counts
as busy). `services/queue/release.sh safepoint` runs it, and `install.sh` waits for exit 0 before
restarting the controller. Standard library only, so it runs from any checkout without an
environment.

A restart is SIGTERM: the loop finishes its current pass and the process exits, but only within
DEFAULT_STOP_GRACE (90 s, tauceti_queue/__main__.py); a longer pass is killed where it stands. Every
store write is a transaction, so nothing is half-recorded, and `Controller.recover` on start releases
the controller's own claims and returns reservations. What a kill can still cost is a side effect
made on GitHub but not yet recorded, or long work thrown away. Busy, therefore, is one of:

- a promotion due or in progress: a pending `promote` outbox entry whose next attempt is due while
  publication is enabled. Performing it pushes the canonical branch and then records the landing
  (`Controller._perform_promotion`, then `_landed`), and the entry is marked done only after both.
  The landing reads every member's landed commit first, about a second per member, so a large
  batch outlasts the stop grace; killed after the push, the landing is recorded again from the
  canonical branch on the next start, and mixed sources call the unsettled send
  `landing-uncertain` and refuse to change primary until it is reconciled.
- a CI dispatch in flight: an attempt in `dispatch_state` `sending` sent recently. Killed between
  the send and its acknowledgement, it stays unknown; normal passes never retransmit it, so it waits
  for the run to be discovered or for an operator's `retry_ci`.
- a controller replay: an unreleased, unexpired claim of worker `controller` (an insertion probe or
  a head rebase replaying a chain). The restart releases it (`released` in the `restart` journal
  entry) and the replay is redone.

Not busy, and printed for information: the head's worker claims and open task (a restart keeps
worker claims; workers retry renewals every 15 s until the claim expires and retry a result for
minutes), a head CI run executing on GitHub (its run id is recorded and the controller re-reads it
by polling, so a missed webhook costs at most one polling interval), and pending outbox entries
(persisted and resumed). A dispatch still `sending` after the window below is left over from an earlier
interruption; a restart does not make it worse, so it is reported as stale, not busy.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

SENDING_WINDOW = 300  # seconds: a dispatch request (30 s HTTP timeout) finishes well within this
TABLES = ("settings", "batches", "claims", "tasks", "ci_attempts", "outbox")


def ago(now: float, then: float | None) -> str:
    return "?" if then is None else f"{now - then:.0f}s ago"


def check(db: sqlite3.Connection, now: float) -> tuple[list[str], list[str]]:
    """The busy operations and the information lines."""
    busy: list[str] = []
    info: list[str] = []
    setting = db.execute("SELECT value FROM settings WHERE key='publication_enabled'").fetchone()
    publishing = setting is not None and setting["value"] == "true"
    for row in db.execute(
        "SELECT id, key, attempts, next_attempt_at FROM outbox WHERE kind='promote' AND state='pending' ORDER BY id"
    ):
        line = f"outbox {row['id']} {row['key']} (attempts {row['attempts']}"
        if not publishing:
            info.append(f"promotion held while publication is disabled: {line})")
        elif row["next_attempt_at"] <= now:
            busy.append(f"promotion due or in progress: {line})")
        else:
            info.append(f"promotion backing off: {line}, next in {row['next_attempt_at'] - now:.0f}s)")
    for row in db.execute(
        "SELECT id, operation, batch_id, created_at FROM claims "
        "WHERE worker='controller' AND released_at IS NULL AND expires_at > ? ORDER BY id",
        (now,),
    ):
        busy.append(
            f"controller replay: claim {row['id']} ({row['operation']}) on batch {row['batch_id']}, "
            f"taken {ago(now, row['created_at'])}"
        )
    for row in db.execute(
        "SELECT id, batch_id, sent_at FROM ci_attempts WHERE dispatch_state='sending' AND outcome='pending' ORDER BY id"
    ):
        line = f"CI dispatch of attempt {row['id']} for batch {row['batch_id']} sent {ago(now, row['sent_at'])}"
        if row["sent_at"] is not None and now - row["sent_at"] < SENDING_WINDOW:
            busy.append(f"{line}, not yet acknowledged")
        else:
            info.append(f"stale: {line} and never acknowledged (an earlier interruption)")
    heads = list(db.execute("SELECT id, version, tip FROM batches WHERE state='head' ORDER BY id"))
    if not heads:
        info.append("no head")
    for head in heads:
        info.append(f"head {head['id']} version {head['version']} tip {head['tip'][:12]}")
        claims = list(
            db.execute(
                "SELECT id, worker, operation, task_id, expires_at FROM claims "
                "WHERE batch_id=? AND released_at IS NULL AND worker != 'controller' ORDER BY id",
                (head["id"],),
            )
        )
        info.append(
            f"head {head['id']} worker claims: {len(claims)} (kept across a restart)"
            + "".join(
                f"; claim {c['id']} {c['worker']} task {c['task_id']} {c['operation']}, "
                f"expires in {c['expires_at'] - now:.0f}s"
                for c in claims
            )
        )
        for task in db.execute(
            "SELECT id, kind FROM tasks WHERE batch_id=? AND state='open' ORDER BY id", (head["id"],)
        ):
            info.append(f"head {head['id']} open task {task['id']} ({task['kind']})")
        attempt = db.execute(
            "SELECT id, outcome, dispatch_state, run_id, run_status, dispatched_at FROM ci_attempts "
            "WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (head["id"],),
        ).fetchone()
        if attempt is not None:
            info.append(
                f"head {head['id']} CI attempt {attempt['id']}: outcome {attempt['outcome']}, "
                f"dispatch {attempt['dispatch_state']}, run {attempt['run_id'] or '-'} "
                f"{attempt['run_status'] or '-'}, dispatched {ago(now, attempt['dispatched_at'])}"
            )
    pending, due = db.execute(
        "SELECT count(*), coalesce(sum(next_attempt_at <= ?), 0) FROM outbox WHERE state='pending'", (now,)
    ).fetchone()
    info.append(f"outbox pending: {pending} ({due} due; persisted and resumed after a restart)")
    return busy, info


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].startswith("-"):
        print("usage: safepoint.py DATABASE", file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1]).expanduser()
    now = time.time()
    stamp = time.strftime("%H:%M:%S %Z", time.localtime(now))
    if not path.exists():
        print(f"{stamp} safe: no queue database at {path}, nothing to interrupt")
        return 0
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not present & set(TABLES):
            print(f"{stamp} safe: {path} holds no queue records, nothing to interrupt")
            return 0
        busy, info = check(db, now)
    except sqlite3.Error as error:
        print(f"{stamp} unknown: cannot read {path}: {error}")
        return 2
    for line in busy:
        print(f"busy: {line}")
    for line in info:
        print(f"info: {line}")
    if busy:
        print(f"{stamp} not a safe point: {len(busy)} operation(s) a restart would interrupt")
        return 1
    print(f"{stamp} safe point: nothing in flight that a restart would interrupt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

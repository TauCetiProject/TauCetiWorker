"""Host FIFO admission with flock leases. No PID/name polling; dead owners lose their lease."""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
import uuid


@contextlib.contextmanager
def admitted(root, slots, *, watch, waiting, priority=None):
    handle = None
    ticket = None
    lease = None
    prepared = False
    # A short scheduler lock serializes ticket creation and slot grants, never expensive work.
    with (root / "scheduler.lock").open("a+") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        counter = root / "sequence"
        try:
            number = int(counter.read_text()) + 1 if counter.exists() else 1
        except ValueError:
            number = max((int(p.name.split("-")[1]) for p in root.glob("wait-*")), default=0) + 1
        temporary = root / f"sequence-{uuid.uuid4().hex}.tmp"
        temporary.write_text(str(number))
        os.replace(temporary, counter)
        ticket = root / f"wait-{number:020d}-{uuid.uuid4().hex}"
        lease = ticket.open("x+")
        fcntl.flock(lease, fcntl.LOCK_EX)
        # Resumed jobs keep their original age across repeated round deadlines.
        lease.write(str(priority if priority is not None else time.time_ns()))
        lease.flush()
    try:
        while handle is None:
            watch()
            with (root / "scheduler.lock").open("a+") as scheduler:
                fcntl.flock(scheduler, fcntl.LOCK_EX)
                live = []
                for path in root.glob("wait-*"):
                    if path == ticket:
                        live.append((int(path.read_text()), path.name))
                        continue
                    try:
                        with path.open("r+") as other:
                            try:
                                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                path.unlink(missing_ok=True)
                            except BlockingIOError:
                                data = other.read().splitlines()
                                if len(data) == 1:  # a candidate checkpoint is not ready for capacity yet
                                    live.append((int(data[0]), path.name))
                    except FileNotFoundError:
                        pass
                # Only the oldest waiter tries slots. Multiple free slots are filled in FIFO order.
                if live and min(live)[1] == ticket.name:
                    for index in range(slots):
                        fd = os.open(root / f"slot-{index}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            handle = fd
                            ticket.unlink()
                            break
                        except BlockingIOError:
                            os.close(fd)
                        except BaseException:
                            os.close(fd)
                            raise
            if handle is None:
                if not prepared:
                    # Slow preservation must not leave idle slots behind an unready oldest ticket.
                    with (root / "scheduler.lock").open("a+") as scheduler:
                        fcntl.flock(scheduler, fcntl.LOCK_EX)
                        age = ticket.read_text().splitlines()[0]
                        lease.seek(0)
                        lease.truncate()
                        lease.write(age + "\npreparing")
                        lease.flush()
                    try:
                        waiting()
                    finally:
                        with (root / "scheduler.lock").open("a+") as scheduler:
                            fcntl.flock(scheduler, fcntl.LOCK_EX)
                            lease.seek(0)
                            lease.truncate()
                            lease.write(age)
                            lease.flush()
                    prepared = True
                else:
                    waiting()
                time.sleep(0.1)
        yield
    finally:
        ticket.unlink(missing_ok=True)
        lease.close()
        if handle is not None:
            os.close(handle)

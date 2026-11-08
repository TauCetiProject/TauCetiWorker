#!/usr/bin/env python3
"""Stale repair-lock recovery cannot remove a newly acquired live lock."""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_queue import repair_tools


class ReclaimRace(unittest.TestCase):
    def test_two_stale_observers_never_reclaim_the_replacement_live_lock(self):
        with tempfile.TemporaryDirectory(prefix="repair-lock-reclaim-") as tmp:
            lock = Path(tmp) / "build.lock"
            lock.mkdir()
            dead_pid = 2147483000
            (lock / "pid").write_text(str(dead_pid))
            stale_observers = threading.Barrier(2)
            first_entered = threading.Event()
            contender_reached = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()
            guard = threading.Lock()
            local = threading.local()
            active = 0
            entries = 0
            renames = 0
            overlaps = []
            errors = []
            real_kill, real_rename = os.kill, os.rename

            def kill(pid, signal):
                if pid == dead_pid and not getattr(local, "observed", False):
                    local.observed = True
                    stale_observers.wait(timeout=10)
                elif pid == os.getpid():
                    contender_reached.set()  # The contender checked the replacement live holder.
                return real_kill(pid, signal)

            def rename(source, destination):
                nonlocal renames
                with guard:
                    renames += 1
                    call = renames
                if call == 2:
                    # Both already observed the dead PID. Delay the second
                    # reclaim until the first waiter has entered its build.
                    if not first_entered.wait(10):
                        raise AssertionError("first builder never entered")
                return real_rename(source, destination)

            def builder():
                nonlocal active, entries
                try:
                    repair_tools.take_lock(lock, log=lambda *_args, **_kwargs: None)
                    try:
                        with guard:
                            active += 1
                            entries += 1
                            rank = entries
                            if active > 1:
                                overlaps.append(active)
                        if rank == 1:
                            first_entered.set()
                            if not release_first.wait(10):
                                raise AssertionError("first builder was never released")
                        else:
                            second_entered.set()
                            contender_reached.set()
                    finally:
                        with guard:
                            active -= 1
                        shutil.rmtree(lock, ignore_errors=True)
                except BaseException as error:
                    errors.append(error)
                    contender_reached.set()

            threads = [threading.Thread(target=builder, daemon=True) for _ in range(2)]
            with (
                patch.object(repair_tools.os, "kill", side_effect=kill),
                patch.object(repair_tools.os, "rename", side_effect=rename),
            ):
                try:
                    for thread in threads:
                        thread.start()
                    self.assertTrue(first_entered.wait(10))
                    self.assertTrue(contender_reached.wait(10))
                    self.assertFalse(
                        second_entered.is_set(), "a stale observer entered while the replacement held the lock"
                    )
                    self.assertEqual(overlaps, [])
                finally:
                    release_first.set()
                    for thread in threads:
                        thread.join(10)
                self.assertEqual(errors, [])
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertTrue(second_entered.is_set())
                self.assertFalse(lock.exists())
                self.assertEqual(renames, 1, "only the original dead holder was reclaimed")
                self.assertTrue(lock.with_name(lock.name + ".reclaim").is_file(), "the flock inode stays in place")


if __name__ == "__main__":
    unittest.main()

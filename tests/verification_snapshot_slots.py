"""Candidate snapshots are bounded host-wide, and a full host never blocks a snapshot forever."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_worker import verification_jobs as jobs


class SnapshotSlots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tauceti-snapshot-slots-")
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {"TAUCETI_BUILD_CACHE_ROOT": str(Path(tmp.name) / "builds")})
        environment.start()
        self.addCleanup(environment.stop)
        assert os.environ["TAUCETI_BUILD_CACHE_ROOT"].startswith(tmp.name)

    def test_at_most_the_slot_count_run_at_once(self):
        running, peak, lock = [0], [0], threading.Lock()

        def work():
            with jobs.candidate_slot():
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                time.sleep(0.3)
                with lock:
                    running[0] -= 1

        threads = [threading.Thread(target=work) for _ in range(jobs.SNAPSHOT_SLOTS + 3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], jobs.SNAPSHOT_SLOTS)

    def test_a_full_host_proceeds_after_the_wait(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with jobs.candidate_slot():
                held.set()
                release.wait(10)

        holders = []
        with patch.object(jobs, "SNAPSHOT_SLOTS", 1), patch.object(jobs, "SNAPSHOT_SLOT_WAIT", 1):
            holder = threading.Thread(target=hold)
            holder.start()
            holders.append(holder)
            held.wait(5)
            started = time.monotonic()
            with jobs.candidate_slot():
                waited = time.monotonic() - started
            release.set()
        for holder in holders:
            holder.join()
        self.assertGreaterEqual(waited, 0.9)
        self.assertLess(waited, 5)


if __name__ == "__main__":
    unittest.main()

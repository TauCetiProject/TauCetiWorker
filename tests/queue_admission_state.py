"""Pending intake survives process lifetime and never acknowledges a racing input change."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue.admission_state import AdmissionState
from tauceti_queue.store import Store


class AdmissionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "queue.sqlite"
        self.now = 1000.0
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.state = AdmissionState(self.store, "o/r", clock=lambda: self.now)

    def test_complete_input_is_not_repeated_and_changes_become_pending(self):
        self.assertTrue(self.state.observe(1, "head", "v1"))
        self.assertFalse(self.state.observe(1, "head", "v1"))
        work = self.state.due()[0]
        self.assertTrue(self.state.complete(work, "v1", "review1", "waiting"))
        self.assertEqual(self.state.due(), [])
        self.assertTrue(self.state.observe(1, "head", "build-finished"))
        self.assertEqual(len(self.state.due()), 1)

    def test_new_event_fences_success_and_deferred_old_results(self):
        self.state.observe(1, "head", "v1")
        work = self.state.due()[0]
        self.state.dirty(1, head="new-head", urgent=True)
        self.assertFalse(self.state.complete(work, "v1", "review1", "approved"))
        self.assertFalse(self.state.defer(work, self.now + 3600, "old quota error"))
        new = self.state.due()[0]
        self.assertEqual(new["head_sha"], "new-head")
        self.assertTrue(new["urgent"])

    def test_real_deadline_and_pending_work_survive_restart(self):
        self.state.observe(1, "head", "v1")
        work = self.state.due()[0]
        self.state.defer(work, self.now + 7200, "Retry-After")
        self.store.close()
        self.store = Store(self.path)
        self.state = AdmissionState(self.store, "o/r", clock=lambda: self.now)
        self.assertEqual(self.state.due(), [])
        self.assertEqual(self.state.summary()["pending"], 1)
        self.now += 7200
        self.assertEqual(len(self.state.due()), 1)
        self.assertEqual(self.state.due()[0]["generation"], work["generation"])

    def test_old_work_and_revocations_receive_service(self):
        self.state.observe(1, "head", "v1")
        self.now += 1
        self.state.observe(2, "head", "v1")
        self.state.attempted(self.state.due()[0])
        self.assertEqual([r["number"] for r in self.state.due()], [2, 1])
        self.state.dirty(3, urgent=True)
        self.assertEqual(self.state.due()[0]["number"], 3)

    def test_large_backlog_has_no_invocation_or_pr_count_cutoff(self):
        for number in range(1, 1201):
            self.state.observe(number, "head", "v1")
        work = self.state.due()
        self.assertEqual(len(work), 1200)
        for row in work:
            self.state.complete(row, "v1", "review", "waiting")
        self.assertEqual(self.state.summary()["pending"], 0)


if __name__ == "__main__":
    unittest.main()

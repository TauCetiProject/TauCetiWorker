"""Pending intake survives process lifetime and never acknowledges a racing input change."""

import sqlite3
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

    def test_attempts_retain_fifo_place_and_revocations_receive_service(self):
        self.state.observe(1, "head", "v1")
        self.now += 1
        self.state.observe(2, "head", "v1")
        self.state.attempted(self.state.due()[0])
        self.assertEqual([r["number"] for r in self.state.due()], [1, 2])
        self.assertEqual(self.state.get(1)["attempted_at"], self.now)
        self.state.dirty(3, urgent=True)
        self.assertEqual(self.state.due()[0]["number"], 3)
        self.assertEqual(self.state.next_due(prefer_urgent=True)["number"], 3)
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 1)

    def test_paced_evaluation_keeps_fifo_place_until_complete(self):
        for number in range(1, 4):
            self.state.observe(number, "head", "v1")
            self.now += 1
        generation = self.state.get(1)["generation"]
        queue_at = self.state.get(1)["queue_at"]
        for resource in ("core:background", "graphql:background", "core"):
            work = self.state.next_due(prefer_urgent=False)
            self.assertEqual(work["number"], 1)
            self.state.attempted(work)
            self.state.defer(work, self.now + 10, "resource wait", resource)
            self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 2)
            self.now += 10
            self.assertEqual(self.state.get(1)["generation"], generation)
            self.assertEqual(self.state.get(1)["queue_at"], queue_at)
        work = self.state.next_due(prefer_urgent=False)
        self.assertEqual(work["number"], 1)
        self.state.complete(work, "v1", "review1", "approved")
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 2)

    def test_source_failures_yield_and_resource_waits_preserve_failure_order(self):
        for number in range(1, 4):
            self.state.observe(number, "head", "v1")
        self.now += 1
        first = self.state.get(1)
        self.state.attempted(first)
        self.state.defer(first, self.now + 10, "git unavailable")
        self.now += 1
        second = self.state.get(2)
        self.state.defer(second, self.now + 10, "compare malformed")
        self.now += 10
        self.assertEqual([r["number"] for r in self.state.due()], [3, 1, 2])
        self.state.complete(self.state.get(3), "v1", "review3", "waiting")
        queue_at = self.state.get(1)["queue_at"]
        self.state.attempted(self.state.get(1))
        self.state.defer(self.state.get(1), self.now + 10, "paced", "core:background")
        self.now += 10
        self.assertEqual(self.state.get(1)["queue_at"], queue_at)
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 1)
        self.state.defer(self.state.get(1), self.now + 10, "git still unavailable")
        self.now += 10
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 2)

    def test_other_resources_and_urgent_revocations_progress_during_fifo_wait(self):
        self.state.observe(1, "head", "v1")
        self.state.defer(self.state.get(1), self.now + 20, "paced", "core:background")
        self.now += 1
        self.state.observe(2, "head", "v1")
        self.state.defer(self.state.get(2), self.now + 5, "paced", "graphql:background")
        self.state.observe(3, "head", "v1", urgent=True)
        self.store.insert("inputs", repository="o/r", number=3, head_sha="head", state="waiting", approved_at=self.now)
        self.state.defer(self.state.get(3), self.now + 5, "paced", "core:background")
        self.now += 5
        self.assertEqual(self.state.next_due(prefer_urgent=True)["number"], 3)
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 2)
        self.assertEqual(self.state.next_due(urgent_only=True)["number"], 3)
        self.state.complete(self.state.get(3), "v1", "review3", "review-unsafe")
        self.assertEqual(self.state.next_due(prefer_urgent=True)["number"], 2)
        self.now += 14
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 1)

    def test_queue_position_survives_restart_and_moves_on_new_source(self):
        for number in (1, 2):
            self.state.observe(number, "head", "v1")
            self.state.defer(self.state.get(number), self.now + 10, "source unavailable")
        self.store.close()
        self.store = Store(self.path)
        self.state = AdmissionState(self.store, "o/r", clock=lambda: self.now)
        self.assertEqual(self.state.get(1)["queue_at"], self.now)
        old = self.state.get(1)
        self.now += 1
        self.state.dirty(1, head="new-head")
        self.assertEqual(self.state.get(1)["queue_at"], self.now)
        self.assertEqual(self.state.get(1)["first_pending_at"], old["first_pending_at"])
        self.assertFalse(self.state.defer(old, self.now + 10, "stale source failure"))
        self.assertEqual(self.state.get(1)["queue_at"], self.now)
        self.state.complete(self.state.get(2), "v1", "review2", "waiting")
        self.assertIsNone(self.state.get(2)["queue_at"])

    def test_continuous_invalidations_between_paced_attempts_yield_to_other_work(self):
        self.state.observe(1, "head", "v1")
        first_pending_at = self.state.get(1)["first_pending_at"]
        for iteration in range(3):
            busy = self.state.next_due(prefer_urgent=False)
            self.assertEqual(busy["number"], 1)
            self.state.attempted(busy)
            self.state.defer(busy, self.now + 10, "paced", "core:background")
            self.now += 1
            self.state.observe(iteration + 2, "head", "v1")
            self.now += 1
            self.state.dirty(1, head=f"head-{iteration}")
            self.assertEqual(self.state.get(1)["first_pending_at"], first_pending_at)
            self.assertEqual(self.state.get(1)["queue_at"], self.now)
            self.assertFalse(self.state.complete(busy, "v1", "old review", "approved"))
            self.assertFalse(self.state.defer(busy, self.now + 100, "stale source failure"))
            other = self.state.next_due(prefer_urgent=False)
            self.assertEqual(other["number"], iteration + 2)
            self.state.complete(other, "v1", "review2", "waiting")
            self.now += 10

    def test_failed_retry_is_not_starved_by_continuous_later_arrivals(self):
        self.state.observe(1, "head", "v1")
        self.now += 1
        self.state.observe(2, "head", "v1")
        self.now += 1
        self.state.defer(self.state.get(1), self.now + 10, "git unavailable")
        queue_at = self.state.get(1)["queue_at"]
        for number in range(3, 13):
            self.now += 1
            self.state.observe(number, "head", "v1")
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 2)
        self.state.complete(self.state.get(2), "v1", "review2", "waiting")
        self.assertEqual(self.state.next_due(prefer_urgent=False)["number"], 1)
        self.assertEqual(self.state.get(1)["queue_at"], queue_at)
        self.assertEqual([r["number"] for r in self.state.due()], [1, *range(3, 13)])

    def test_existing_database_uses_pending_age_without_rewriting_queue_position(self):
        old_path = Path(self.tmp.name) / "old.sqlite"
        connection = sqlite3.connect(old_path)
        try:
            connection.execute(
                "CREATE TABLE admission_work (id INTEGER PRIMARY KEY, repository TEXT NOT NULL DEFAULT '', "
                "number INTEGER NOT NULL DEFAULT 0, head_sha TEXT NOT NULL DEFAULT '', "
                "generation INTEGER NOT NULL DEFAULT 1, evaluated_generation INTEGER NOT NULL DEFAULT 0, "
                "first_pending_at REAL NOT NULL DEFAULT 0, "
                "attempted_at REAL NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "INSERT INTO admission_work "
                "(repository,number,head_sha,generation,first_pending_at,attempted_at,next_attempt_at) "
                "VALUES ('o/r',1,'retained-head',4,800,900,1100)"
            )
            connection.commit()
        finally:
            connection.close()
        old_store = Store(old_path)
        self.addCleanup(old_store.close)
        state = AdmissionState(old_store, "o/r", clock=lambda: self.now)
        row = state.get(1)
        self.assertIsNone(row["queue_at"])
        self.assertEqual(row["head_sha"], "retained-head")
        self.assertEqual(row["generation"], 4)
        self.assertEqual(row["attempted_at"], 900)
        self.assertEqual(row["next_attempt_at"], 1100)
        state.observe(2, "head", "v1")
        self.now = 1100
        self.assertEqual(state.next_due(prefer_urgent=False)["number"], 1)
        state.attempted(state.get(1))
        state.defer(state.get(1), self.now + 10, "paced", "core:background")
        self.now += 10
        self.assertEqual(state.next_due(prefer_urgent=False)["number"], 1)
        self.assertIsNone(state.get(1)["queue_at"])

    def test_large_backlog_has_no_invocation_or_pr_count_cutoff(self):
        for number in range(1, 1201):
            self.state.observe(number, "head", "v1")
        work = self.state.due()
        self.assertEqual(len(work), 1200)
        for row in work:
            self.state.complete(row, "v1", "review", "waiting")
        self.assertEqual(self.state.summary()["pending"], 0)

    def test_shared_deadline_avoids_retrying_every_waiter_and_survives_restart(self):
        for number in range(1, 5):
            self.state.observe(number, "head", "v1")
        self.state.defer(self.state.get(1), self.now + 10, "paced", "core:background")
        self.state.defer(self.state.get(2), self.now + 10, "paced", "graphql:background")
        self.state.defer(self.state.get(3), self.now + 10, "git unavailable")
        self.state.defer(self.state.get(4), self.now + 20, "next core read", "core:background")
        self.now += 10
        self.assertEqual([r["number"] for r in self.state.due()], [2, 3])
        pending = {r["number"]: r for r in self.state.summary()["pending_prs"]}
        self.assertEqual(pending[1]["next_attempt_at"], self.now + 10)
        self.assertEqual(self.state.get(1)["attempted_at"], 0, "a peer's wait is not another PR attempt")
        self.store.close()
        self.store = Store(self.path)
        self.state = AdmissionState(self.store, "o/r", clock=lambda: self.now)
        self.assertEqual([r["number"] for r in self.state.due()], [2, 3])

    def test_new_source_and_urgent_revocations_do_not_inherit_background_waits(self):
        for number in range(1, 4):
            self.state.observe(number, "head", "v1")
            self.state.defer(self.state.get(number), self.now + 10, "paced", "core:background")
        self.store.insert("inputs", repository="o/r", number=2, head_sha="head", state="waiting", approved_at=self.now)
        self.state.dirty(2, urgent=True)
        self.state.defer(self.state.get(2), self.now + 10, "paced", "core:background")
        self.state.defer(self.state.get(3), self.now + 20, "next core read", "core:background")
        self.now += 10
        self.assertEqual([r["number"] for r in self.state.due()], [2])
        self.state.dirty(1)
        self.assertEqual(self.state.get(1)["retry_resource"], "")
        self.assertEqual([r["number"] for r in self.state.due()], [2, 1])

    def test_urgent_revocations_still_obey_actual_server_deadlines_after_restart(self):
        for number, resource in enumerate(("core", "graphql", "secondary"), start=1):
            self.state.observe(number, "head", "v1", urgent=True)
            self.store.insert(
                "inputs", repository="o/r", number=number, head_sha="head", state="waiting", approved_at=self.now
            )
            self.state.defer(self.state.get(number), self.now + 10, "server deadline", resource)
            self.state.observe(number + 10, "head", "v1")
            self.state.defer(self.state.get(number + 10), self.now + 20, "new server deadline", resource)
        self.now += 10
        self.assertEqual(self.state.due(), [])
        self.store.close()
        self.store = Store(self.path)
        self.state = AdmissionState(self.store, "o/r", clock=lambda: self.now)
        self.assertEqual(self.state.due(), [])

    def test_racing_source_change_does_not_discard_observed_resource_capacity(self):
        self.state.observe(1, "head", "v1")
        old = self.state.get(1)
        self.state.dirty(1, head="new-head", urgent=True)
        self.assertFalse(self.state.defer(old, self.now + 90, "server cooldown", "core"))
        self.assertEqual(self.state.get(1)["retry_resource"], "")
        self.assertEqual(self.state.get(1)["last_error"], "")
        self.assertEqual(self.store.get_setting(self.state.resource_key)["core"], self.now + 90)


if __name__ == "__main__":
    unittest.main()

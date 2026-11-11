#!/usr/bin/env python3
"""Total-admission limits reserve the third worker exclusively for head work."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from queue_controller import PROJECT, ControllerCase

from tauceti_queue.fakes import reopen_controller


class HeadCapacity(ControllerCase):
    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 3, "test")
        self.controller.set_setting("background_start_limit", 2, "test")
        self.controller.set_setting("prepare_min_members", 1, "test")
        self.controller.set_setting("prepare_quiet_seconds", 0, "test")
        self.controller.set_setting("pre_rebase_enabled", False, "test")
        self.controller.set_setting("prepare_pre_rebased", False, "test")
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.head = self.controller._head()
        self.batches = []
        for n in (201, 301, 401):
            batch = self.controller._create_open_batch(self.repo.main_sha(), self.clock())
            self.approve(n, {f"P{n}.lean": f"{n}\n"})
            with self.controller._lock:
                self.assertTrue(self.controller._probe(self.controller.input_for(PROJECT, n), batch, self.clock()))
            self.batches.append(self.controller.store.row("batches", {"id": batch["id"]}))
        self.controller._prepare_open_batches(self.clock())

    def claim(self, name):
        return self.controller.claim_task(name, "admin-worker")

    def head_task(self):
        return self.controller._create_task(self.controller._head(), "repair", self.head["base"], [], self.clock())

    def test_two_background_workers_allow_an_immediate_third_head_claim(self):
        first, second = self.claim("one"), self.claim("two")
        self.assertEqual([first.kind, second.kind], ["prepare", "prepare"])
        self.assertNotEqual(first.batch_id, second.batch_id)
        self.assertIsNone(self.claim("three"))
        taskid = self.head_task()
        third = self.claim("three")
        self.assertEqual((third.task_id, third.kind), (taskid, "repair"))
        self.assertEqual(len(self.controller._active_task_claims()), 3)
        self.assertIsNone(self.claim("four"))
        self.controller.renew_task(first.task_id, first.claim_token)
        self.controller.renew_task(second.task_id, second.claim_token)

    def test_a_head_and_one_background_worker_block_another_background_start(self):
        self.head_task()
        first, second = self.claim("one"), self.claim("two")
        self.assertEqual((first.kind, second.kind), ("repair", "prepare"))
        self.assertIsNone(self.claim("three"), "the third admission must be for head work")
        self.assertEqual(len(self.controller._active_task_claims()), 2)
        adopted = self.controller.submit_task(first.task_id, first.claim_token, first.tip)
        self.assertTrue(adopted.accepted, adopted)
        third = self.claim("three")
        self.assertEqual(third.kind, "prepare", "one live claim leaves room for another background start")
        self.assertEqual(len(self.controller._active_task_claims()), 2)

    def test_head_priority_wins_even_if_background_task_is_older(self):
        self.clock.advance(1)
        taskid = self.head_task()
        self.assertEqual(self.claim("one").task_id, taskid)

    def test_release_or_expiry_frees_a_background_admission_without_interrupting_others(self):
        one, two = self.claim("one"), self.claim("two")
        self.controller.release_task(one.task_id, one.claim_token, "worker-stopped")
        self.assertIsNotNone(self.claim("three"))
        self.controller.renew_task(two.task_id, two.claim_token)
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        self.assertIsNotNone(self.claim("four"))
        self.assertIsNotNone(self.claim("five"))
        self.assertIsNone(self.claim("six"))

    def test_concurrent_background_requests_cannot_consume_the_reserved_slot(self):
        barrier = threading.Barrier(3)
        tasks = []
        errors = []

        def claim(n):
            try:
                barrier.wait(timeout=5)
                tasks.append(self.claim(str(n)))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=claim, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(t is not None for t in tasks), 2)
        self.head_task()
        self.assertEqual(self.claim("reserved").kind, "repair")

    def test_restart_preserves_both_background_claims_and_the_reserved_head_capacity(self):
        one, two = self.claim("one"), self.claim("two")
        self.controller = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.controller.recover()
        self.assertIsNone(self.claim("three"))
        self.controller.renew_task(one.task_id, one.claim_token)
        self.controller.renew_task(two.task_id, two.claim_token)
        self.head_task()
        self.assertEqual(self.claim("three").kind, "repair")

    def test_existing_two_worker_configuration_retains_head_plus_one_background(self):
        self.controller.set_setting("repair_concurrency", 2, "test")
        self.head_task()
        self.assertEqual(self.claim("one").kind, "repair")
        self.assertEqual(self.claim("two").kind, "prepare")
        self.assertIsNone(self.claim("three"))


if __name__ == "__main__":
    unittest.main()

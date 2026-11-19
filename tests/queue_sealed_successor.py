#!/usr/bin/env python3
"""Sealed successor membership, background ownership and promotion against real Git chains."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_controller import PROJECT, ControllerCase

from tauceti_queue import chain, feeds
from tauceti_queue.controller import DEFAULT_SETTINGS, VERDICT_CONTEXTS, TaskError
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller, reopen_controller, run_git


class SealedSuccessor(ControllerCase):
    def setUp(self):
        super().setUp()
        # A small configurable boundary exercises the same comparisons as the production 100.
        self.controller.set_setting("sealed_successor_min_members", 3, "test")
        self.controller.set_setting("repair_concurrency", 2, "test")
        self.failing_head()

    def add(self, batch, numbers):
        for number in numbers:
            self.approve(number, {f"P{number}.lean": f"{number}\n"})
            row = self.controller.input_for(PROJECT, number)
            with self.controller._lock:
                self.assertTrue(self.controller._probe(row, batch, self.clock()))
            batch = self.controller.store.row("batches", {"id": batch["id"]})
        return batch

    def first(self, numbers=(201, 202, 203)):
        return self.add(self.controller._open_batch(), numbers)

    def later(self, numbers=(301, 302, 303, 304)):
        batch = self.controller._create_open_batch(self.repo.main_sha(), self.clock())
        return self.add(batch, numbers)

    def row(self, batch):
        return self.controller.store.row("batches", {"id": batch["id"]})

    def claim_prepare(self):
        # The head is failing: its repair must retain the first claim slot.
        head_task = self.controller.claim_task("head-worker", "admin-worker")
        self.assertEqual(head_task.kind, "repair")
        task = self.controller.claim_task("background-worker", "admin-worker")
        self.assertEqual(task.kind, "prepare")
        return task

    def test_oldest_seals_at_threshold_even_with_a_larger_later_batch(self):
        self.assertEqual(DEFAULT_SETTINGS["sealed_successor_min_members"], 100)
        first, later = self.first(), self.later()
        self.controller.tick()
        self.assertEqual((self.row(first)["sealed"], self.row(later)["sealed"]), (1, 0))
        self.assertEqual(self.row(first)["state"], "open", "background protocol remains compatible")
        self.assertEqual(self.controller.status()["sealed_successor"]["id"], first["id"])
        self.assertEqual(self.controller.status()["counters"]["successors_sealed"], 1)
        self.controller.tick()
        self.assertEqual(self.row(later)["sealed"], 0, "only one successor")
        self.assertEqual(len(self.controller.store.rows("journal", {"kind": "successor-sealed"})), 1)

    def test_smaller_oldest_is_not_overtaken_by_larger_later_batch(self):
        first, later = self.first((201, 202)), self.later()
        self.controller.tick()
        self.assertEqual((self.row(first)["sealed"], self.row(later)["sealed"]), (0, 0))
        self.assertIsNone(self.controller.status()["sealed_successor"])
        self.add(first, (203,))
        self.controller.tick()
        self.assertEqual(self.row(first)["sealed"], 1)

    def test_no_head_seals_immediately_below_the_threshold(self):
        self.controller.store.update("batches", {"state": "head"}, state="removed")
        first = self.first((201,))
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], first["id"])

    def test_sealed_successor_prepares_without_size_or_quiet_delay(self):
        first = self.first()
        self.controller.tick()
        status = self.controller.status()["sealed_successor"]
        self.assertEqual(status["quiet_seconds"], 0)
        self.assertLess(status["size"], self.controller.setting("prepare_min_members"))
        self.assertEqual(status["prepare_task"]["kind"], "prepare")
        task = self.claim_prepare()
        self.assertEqual((task.batch_id, task.base), (first["id"], first["base"]))
        self.assertIsNone(self.controller.claim_task("third-worker", "admin-worker"))
        self.assertEqual(len(self.github.runs), 1, "no background staging CI")

    def test_inflow_routes_away_before_claim_and_after_clean_preparation(self):
        first = self.first()
        self.controller.tick()
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertNotEqual(self.controller.input_for(PROJECT, 301)["batch_id"], first["id"])
        task = self.claim_prepare()
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.approve(302, {"P302.lean": "302\n"})
        self.controller.tick()
        self.assertEqual(self.row(first)["version"], first["version"])
        self.assertEqual(self.row(first)["prepared_version"], first["version"])
        self.assertEqual(self.row(first)["sealed"], 1)
        self.assertNotEqual(self.controller.input_for(PROJECT, 302)["batch_id"], first["id"])

    def test_successor_does_not_consume_unsealed_capacity(self):
        self.controller.set_setting("open_batch_limit", 1, "test")
        first = self.first()
        self.controller.tick()
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertEqual(len(self.controller._unsealed_batches()), 1)
        self.assertEqual(len(self.controller._open_batches()), 2)
        self.assertNotEqual(self.controller.input_for(PROJECT, 301)["batch_id"], first["id"])

    def test_claimed_preparation_survives_freezing_and_adopts_edits(self):
        first = self.first()
        self.controller.set_setting("sealed_successor_min_members", 0, "test")
        self.controller.set_setting("prepare_min_members", 1, "test")
        self.controller.set_setting("prepare_quiet_seconds", 0, "test")
        self.controller.tick()
        agent = ScriptedAgent(
            "background-worker",
            "admin-worker",
            self.repo,
            self.root / "agent",
            lambda _: {f"{PROJECT}#202": Resolution({"P202.lean": "fixed\n"}, explanation="Fix P202.")},
        )
        self.controller.claim_task("head-worker", "admin-worker")
        task = agent.claim(self.controller)
        tip = agent.produce(task)
        self.controller.set_setting("sealed_successor_min_members", 3, "test")
        self.controller.tick()
        self.assertEqual(self.row(first)["sealed"], 1)
        self.controller.renew_task(task.task_id, task.claim_token)
        result = self.controller.submit_task(task.task_id, task.claim_token, tip)
        self.assertTrue(result.accepted, result)
        self.assertEqual(self.row(first)["sealed"], 1)
        self.assertEqual(self.repo.file_at(self.row(first)["tip"], "P202.lean"), "fixed\n")
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertEqual(self.row(first)["version"], result.version)

    def test_head_landing_waits_for_owner_then_promotes_and_replenishes(self):
        first, later = self.first(), self.later()
        self.controller.tick()
        task = self.claim_prepare()
        sealed_at = self.row(first)["sealed_at"]
        # An external landing is reconciled while the successor is being prepared.
        run_git(
            self.repo.clone, "push", "-q", str(self.repo.origin), f"{self.controller._head()['tip']}:refs/heads/main"
        )
        self.clock.advance(1)
        self.controller.tick()
        self.assertIsNone(self.controller._head())
        self.assertEqual(self.row(first)["state"], "open")
        self.controller.renew_task(task.task_id, task.claim_token)
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], first["id"])
        self.assertEqual(self.controller._head()["sealed_at"], sealed_at)
        self.assertEqual(self.row(later)["sealed"], 1, "replenish the successor immediately")
        self.assertEqual(len(self.github.runs), 2, "the new head gets authoritative CI")
        self.assertEqual(self.github.runs[-1].head_sha, self.controller._head()["tip"])

    def test_headless_handoff_fills_two_seals_without_interrupting_the_preparation_owner(self):
        first, later = self.first(), self.later()
        third = self.later((401, 402, 403, 404, 405))
        self.controller.tick()
        task = self.claim_prepare()
        run_git(
            self.repo.clone, "push", "-q", str(self.repo.origin), f"{self.controller._head()['tip']}:refs/heads/main"
        )
        self.controller.tick()
        self.assertIsNone(self.controller._head())
        self.assertEqual([self.row(b)["sealed"] for b in (first, later, third)], [1, 1, 0])
        self.assertEqual(self.row(later)["version"], later["version"])
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "open")
        self.controller.renew_task(task.task_id, task.claim_token)
        active = self.controller._active_claims(first["id"], task_id=task.task_id)
        self.assertEqual(len(active), 1)
        self.assertIsNotNone(self.controller._open_own_base_prepare(self.row(later)))
        self.approve(501, {"P501.lean": "501\n"})
        self.controller.tick()
        self.assertEqual(self.row(later)["version"], later["version"])
        self.assertNotIn(self.controller.input_for(PROJECT, 501)["batch_id"], (first["id"], later["id"]))
        sealed_at = self.row(later)["sealed_at"]
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], first["id"])
        self.assertEqual(self.row(later)["sealed_at"], sealed_at)
        self.assertEqual([self.row(b)["sealed"] for b in (first, later, third)], [1, 1, 0])

    def test_headless_claimed_unsealed_batch_and_its_successor_both_freeze(self):
        first, later = self.first(), self.later()
        self.controller.set_setting("sealed_successor_min_members", 0, "test")
        self.controller.set_setting("prepare_min_members", 1, "test")
        self.controller.set_setting("prepare_quiet_seconds", 0, "test")
        self.controller.tick()
        task = self.claim_prepare()
        self.controller.store.update("batches", {"state": "head"}, state="removed")
        self.controller.set_setting("sealed_successor_min_members", 3, "test")
        self.controller.tick()
        self.assertIsNone(self.controller._head())
        self.assertEqual((self.row(first)["sealed"], self.row(later)["sealed"]), (1, 1))
        self.controller.renew_task(task.task_id, task.claim_token)

    def test_handoff_does_not_skip_a_small_oldest_unsealed_batch(self):
        first = self.first()
        small = self.later((301, 302))
        bigger = self.later((401, 402, 403, 404))
        self.controller.tick()
        task = self.claim_prepare()
        run_git(
            self.repo.clone, "push", "-q", str(self.repo.origin), f"{self.controller._head()['tip']}:refs/heads/main"
        )
        self.controller.tick()
        self.assertIsNone(self.controller._head())
        self.assertEqual([self.row(b)["sealed"] for b in (first, small, bigger)], [1, 0, 0])
        self.controller.renew_task(task.task_id, task.claim_token)

    def test_preexisting_headless_state_is_repaired_on_the_next_tick_and_survives_restart(self):
        first, later = self.first(), self.later()
        self.controller.tick()
        task = self.claim_prepare()
        self.controller.store.update("batches", {"state": "head"}, state="removed")
        # Recover exactly the production state: one sealed preparation claim and no head.
        self.controller = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.controller.recover()
        self.controller.tick()
        self.assertEqual((self.row(first)["sealed"], self.row(later)["sealed"]), (1, 1))
        self.controller.renew_task(task.task_id, task.claim_token)
        self.controller.tick()
        self.assertEqual(len(self.controller.store.rows("journal", {"kind": "successor-sealed"})), 2)

    def test_unclaimed_offer_does_not_delay_promotion(self):
        first = self.first()
        self.controller.tick()
        taskid = self.controller.status()["sealed_successor"]["prepare_task"]["task_id"]
        run_git(
            self.repo.clone, "push", "-q", str(self.repo.origin), f"{self.controller._head()['tip']}:refs/heads/main"
        )
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], first["id"])
        self.assertEqual(self.controller.store.row("tasks", {"id": taskid})["resolution"], "sealed")

    def test_expired_owner_does_not_strand_successor(self):
        first = self.first()
        self.controller.tick()
        task = self.claim_prepare()
        run_git(
            self.repo.clone, "push", "-q", str(self.repo.origin), f"{self.controller._head()['tip']}:refs/heads/main"
        )
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        self.controller.tick()
        self.assertEqual(self.controller._head()["id"], first["id"])
        with self.assertRaises(TaskError):
            self.controller.renew_task(task.task_id, task.claim_token)

    def test_restart_keeps_sealing_claim_and_routing(self):
        first = self.first()
        self.controller.tick()
        task = self.claim_prepare()
        old = self.controller
        self.controller = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.controller.recover()
        self.assertTrue(self.controller.status()["sealed_successor"]["sealed"])
        self.controller.renew_task(task.task_id, task.claim_token)
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertEqual(self.row(first)["version"], first["version"])
        self.assertNotEqual(self.controller.input_for(PROJECT, 301)["batch_id"], first["id"])
        old.store.close()

    def test_disabling_successor_sealing_does_not_reopen_existing_successor(self):
        first = self.first()
        self.controller.tick()
        self.controller.set_setting("sealed_successor_min_members", 0, "test")
        self.approve(301, {"P301.lean": "301\n"})
        self.controller.tick()
        self.assertEqual(self.row(first)["sealed"], 1)
        self.assertNotEqual(self.controller.input_for(PROJECT, 301)["batch_id"], first["id"])

    def test_disabled_or_inference_hold_prevents_new_successor(self):
        first = self.first()
        self.controller.set_setting("sealed_successor_min_members", 0, "test")
        self.controller.tick()
        self.assertEqual(self.row(first)["sealed"], 0)
        self.controller.set_setting("sealed_successor_min_members", 3, "test")
        self.controller._set_state("inference_hold", {"since": self.clock(), "last_error": "test"})
        self.controller.tick()
        self.assertEqual(self.row(first)["sealed"], 0)

    def test_inflight_probe_cannot_append_after_membership_is_sealed(self):
        first = self.first()
        self.approve(301, {"P301.lean": "301\n"})
        row = self.controller.input_for(PROJECT, 301)
        original = chain.build

        def seal_during_probe(*args, **kwargs):
            self.controller.store.update("batches", {"id": first["id"]}, sealed=1, sealed_at=self.clock())
            return original(*args, **kwargs)

        with patch.object(chain, "build", seal_during_probe), self.controller._lock:
            self.assertFalse(self.controller._probe(row, first, self.clock()))
        self.assertEqual(self.row(first)["version"], first["version"])
        self.assertEqual(self.controller.input_for(PROJECT, 301)["state"], "waiting")

    def test_pre_rebased_preparation_is_reused_but_the_promoted_head_still_needs_ci(self):
        controller, github, repo, clock = make_controller(self.root / "pre-rebase")
        self.addCleanup(controller.store.close)
        github.admins.add("admin-worker")
        controller.set_setting("sealed_successor_min_members", 3, "test")
        controller.set_setting("repair_concurrency", 2, "test")
        for number in (101, 201, 202, 203):
            head = github.create_pr(number, {f"P{number}.lean": f"{number}\n"})
            controller.approve(PROJECT, number, head, "reviewer")
            if number == 101:
                controller.tick()
        controller.tick()
        successor = controller.status()["sealed_successor"]
        speculative = controller.claim_task("background-worker", "admin-worker")
        self.assertTrue(speculative.extra["pre_rebased"])
        controller.release_task(speculative.task_id, speculative.claim_token, "prepared_clean")
        prepared = controller.store.row("batches", {"id": successor["id"]})
        self.assertEqual(prepared["pre_rebase_prepared"], speculative.tip)
        self.assertEqual(len(github.runs), 1)
        github.complete_runs()
        for context in VERDICT_CONTEXTS:
            controller.handle_event("status", {"sha": github.runs[-1].head_sha, "context": context})
        controller.tick()  # land the preceding head
        controller.tick()  # promote and dispatch the successor
        head = controller._head()
        self.assertEqual((head["id"], head["tip"], head["base"]), (successor["id"], speculative.tip, repo.main_sha()))
        self.assertEqual(len(github.runs), 2)
        self.assertEqual(github.runs[-1].head_sha, speculative.tip)
        self.assertEqual(controller.status()["head"]["stage"], "ci")
        self.assertNotEqual(repo.main_sha(), speculative.tip)
        github.complete_runs()
        for context in VERDICT_CONTEXTS:
            controller.handle_event("status", {"sha": speculative.tip, "context": context})
        controller.tick()
        self.assertEqual(repo.main_sha(), speculative.tip)
        self.assertEqual(controller.store.row("batches", {"id": successor["id"]})["state"], "landed")

    def test_existing_public_feed_identifies_waiting_sealed_successor(self):
        first = self.first()
        self.controller.tick()
        feed = feeds.ControllerFeedSource(self.controller)
        batches = feed.active_batches(PROJECT, "main")
        successor = next(b for b in batches if b.id == first["id"])
        self.assertTrue(successor.sealed)
        self.assertEqual(successor.state, "waiting")
        self.assertEqual(successor.head_sha, first["tip"])


if __name__ == "__main__":
    import unittest

    unittest.main()

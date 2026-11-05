#!/usr/bin/env python3
"""The local end-to-end fixture of the AI merge queue: real Git history, a fake GitHub, scripted CI and agents.

Every test runs the controller over a temporary bare repository through `tauceti_queue.fakes`, with
a controllable clock and no inference. The tests are named after the acceptance scenarios of
`docs/redesign/specs/ai-merge-queue.md`; the mapping is in `docs/queue/controller.md`.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain, feeds  # noqa: E402
from tauceti_queue.controller import MERGED_TITLE_PREFIX, ControllerError, TaskError  # noqa: E402
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller, reopen_controller  # noqa: E402
from tauceti_queue.loop import ReconciliationLoop  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


def fail_members(*numbers, kind="compiler"):
    """A CI policy that fails any candidate containing one of `numbers`, with a diagnostic per member."""
    failing = set(numbers)

    def policy(_head, _base, members):
        hits = [n for n in members if n in failing]
        if hits:
            return "fail", [
                {"kind": kind, "module": f"TauCeti/Pr{n}.lean", "message": f"error in PR {n}"} for n in hits
            ]
        return "pass", []

    return policy


def verdicts(*sequence):
    """A CI policy that answers with the given verdicts in order, then passes."""
    remaining = list(sequence)

    def policy(_head, _base, _members):
        verdict = remaining.pop(0) if remaining else "pass"
        if verdict == "fail":
            return "fail", [{"kind": "compiler", "module": "TauCeti/Batch.lean", "message": "scripted failure"}]
        return verdict, []

    return policy


class QueueFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")
        self.github.admins.add("second-admin")

    # --- helpers ---------------------------------------------------------------------------------

    def approve(self, number, files, **kwargs):
        head = self.github.create_pr(number, files, **kwargs)
        result = self.controller.approve(PROJECT, number, head, "reviewer")
        self.assertTrue(result["accepted"], result)
        return head

    def tick(self, n=1):
        reasons = []
        for _ in range(n):
            reasons = self.controller.tick()
        return reasons

    def head(self):
        return self.controller.store.row("batches", {"state": "head"})

    def open_batch(self):
        return self.controller.store.row("batches", {"state": "open"})

    def members(self, batch):
        return self.controller.members(batch["id"])

    def run_ci(self, event=True):
        """Run every queued candidate build to completion and let the controller observe it."""
        self.github.start_runs()
        self.clock.advance(300)
        self.github.progress_runs("build", "modules 12/12")
        self.clock.advance(300)
        completed = self.github.complete_runs()
        if event:
            for run in completed:
                self.controller.handle_event(
                    "workflow_run", {"action": "completed", "workflow_run": {"head_sha": run.head_sha}}
                )
                self.controller.handle_event("status", {"sha": run.head_sha, "context": "build", "state": "success"})
        return completed

    def agent(self, name="agent", login="admin-worker", script=None, **kwargs):
        script = script or (lambda task: {})
        return ScriptedAgent(name, login, self.repo, self.root / name, script, **kwargs)

    def drive(self, agents=(), limit=12):
        """Tick, run CI and let agents work until no head remains; returns the number of rounds."""
        for rounds in range(limit):
            self.tick()
            if self.head() is None:
                return rounds
            if self.head() is not None and self.controller.status()["head"]["stage"] == "ci":
                self.run_ci()
                continue
            for agent in agents:
                agent.run(self.controller)
        self.tick()
        return limit

    def chain_commits(self, batch):
        return chain.chain_commits(self.repo.clone, batch["base"], batch["tip"])

    def first_parent_chain(self, base, tip):
        return chain.chain_commits(self.repo.origin, base, tip)


class CleanFlow(QueueFixture):
    def test_s1_clean_insertion_rebase_ci_and_publication_without_agents(self):
        base = self.repo.main_sha()
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        self.assertIsNotNone(head)
        self.assertEqual(sorted(m["number"] for m in self.members(head)), [101, 102])
        self.assertEqual(len(self.github.dispatches), 1, "the sealed head is dispatched in the same pass")
        payload = self.github.dispatches[0]["client_payload"]
        self.assertEqual(
            payload,
            {
                "head_sha": head["tip"],
                "base_sha": base,
                "batch_id": "1",
                "members": [m["number"] for m in self.members(head)],
                "base_ref": "main",
            },
        )
        # A third PR approved while the head builds goes to the open batch, which is based on the old main.
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        open_batch = self.open_batch()
        self.assertEqual(open_batch["base"], base)
        self.assertEqual([m["number"] for m in self.members(open_batch)], [103])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"])
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        # The open batch seals as the new head without delay and is rebased onto the new main by replay.
        new_head = self.head()
        self.assertEqual(new_head["id"], open_batch["id"])
        self.assertEqual(new_head["base"], head["tip"])
        self.assertEqual(new_head["version"], open_batch["version"] + 1)
        self.assertEqual([e["detail"]["base"] for e in self.controller.store.journal_entries("rebased")], [head["tip"]])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), new_head["tip"])
        self.assertEqual(self.repo.file_at("main", "C.lean"), "c\n")
        self.assertEqual(self.controller.status()["counters"].get("tasks_repair", 0), 0)
        self.assertEqual(self.controller.status()["counters"].get("tasks_rebase_conflict", 0), 0)
        # Publication: Bors's close with the title prefix, one comment per member, labels left alone.
        self.assertEqual(set(self.github.closed), {101, 102, 103})
        self.assertTrue(all(title.startswith(MERGED_TITLE_PREFIX) for title in self.github.closed.values()))
        self.assertEqual(sorted(c["number"] for c in self.github.comments), [101, 102, 103])
        self.assertIn("bors-staging", self.github.labels[101])
        self.assertEqual([i["state"] for i in self.controller.inputs()], ["landed"] * 3)

    def test_s3_every_commit_single_parent_bijective_including_empty(self):
        self.repo.advance_main({"Already.lean": "done\n"}, "landed elsewhere")
        self.approve(101, {"A.lean": "a\n"})
        self.approve(
            102, {"Already.lean": "done\n"}, start=self.repo.rev_parse("main~1")
        )  # content already in main: empty squash
        self.approve(103, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        commits = self.chain_commits(head)
        self.assertEqual(len(commits), 3)
        self.assertTrue(all(len(c.parents) == 1 for c in commits))
        report = chain.validate(
            self.repo.clone, head["base"], head["tip"], [m["source_pr"] for m in self.members(head)]
        )
        self.assertTrue(report.ok, report.violations)
        self.assertEqual([r.empty for r in report.commits if r.source_pr == f"{PROJECT}#102"], [True])
        self.assertEqual(sorted(c.source_pr for c in commits), sorted(m["source_pr"] for m in self.members(head)))
        self.run_ci()
        self.tick(2)
        landed = self.first_parent_chain(head["base"], self.repo.main_sha())
        self.assertEqual([c.source_pr for c in landed], [c.source_pr for c in commits])

    def test_s3_validation_accepts_membership_equivalent_replacement_in_another_order(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        head = self.head()
        task = self.controller.claim_task("w", "admin-worker")
        self.assertIsNotNone(task)
        # A replacement chain built in the opposite order from the accepted heads: same membership, same messages.
        members = [self.controller._chain_member(m) for m in reversed(self.members(head))]
        result = chain.build(self.repo.clone, task.onto, members, chain_id="reordered")
        self.assertEqual(result.status, chain.CLEAN)
        adoption = self.controller.submit_task(task.task_id, task.claim_token, result.tip)
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual([c.source_pr for c in self.chain_commits(self.head())], [m.source_pr for m in members])
        self.assertEqual(set(adoption.provenance.values()), {"clean"})

    def test_s8_open_batch_never_launches_ci_and_settings_change_without_a_pr(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.approve(103, {"C.lean": "c\n"})
        self.tick(3)
        self.assertEqual(len(self.members(self.open_batch())), 2)
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual({d["client_payload"]["head_sha"] for d in self.github.dispatches}, {self.head()["tip"]})
        before = self.controller.status()["settings"]
        self.assertEqual(before["insertion_age_window"], 16)
        self.assertEqual(before["repair_concurrency"], 1)
        self.assertEqual(before["stall_interval_seconds"], 900)
        self.controller.set_setting("insertion_age_window", 4, "operator")
        self.controller.set_setting("stall_interval_seconds", 120, "operator")
        after = self.controller.status()["settings"]
        self.assertEqual((after["insertion_age_window"], after["stall_interval_seconds"]), (4, 120))
        with self.assertRaises(ControllerError):
            self.controller.set_setting("insertion_age_window", "many", "operator")
        with self.assertRaises(ControllerError):
            self.controller.set_setting("no_such_setting", 1, "operator")

    def test_s7_sealing_promotion_and_restart_keep_one_open_batch_and_at_most_one_head(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        self.assertEqual(self.controller.store.count("batches", {"state": "head"}), 1)
        self.assertEqual(self.controller.store.count("batches", {"state": "open"}), 1)
        # Seal has no collection delay: sealed in the same pass the first member was inserted.
        sealed = self.controller.store.journal_entries("sealed")
        inserted = self.controller.store.journal_entries("inserted")
        self.assertEqual(sealed[0]["at"], inserted[0]["at"])
        # A claimed head is protected: a worker holds a task claim while main advances; no rebase happens.
        self.run_ci()
        self.tick()
        task = self.controller.claim_task("w", "admin-worker")
        self.assertIsNotNone(task)
        self.repo.advance_main({"Admin.lean": "x\n"})
        reasons = self.tick()
        self.assertIn("claimed", " ".join(reasons))
        head = self.head()
        self.assertEqual(head["version"], task.batch_version)
        self.assertNotEqual(head["base"], self.repo.main_sha())
        # Restart: the same invariants hold and the worker's claim survives.
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        restarted.tick()
        self.assertEqual(restarted.store.count("batches", {"state": "head"}), 1)
        self.assertEqual(restarted.store.count("batches", {"state": "open"}), 1)
        self.assertEqual(restarted.renew_task(task.task_id, task.claim_token) > self.clock(), True)


class Repair(QueueFixture):
    def test_s2_s4_ci_failure_yields_repair_task_and_validated_replacement_with_explanations(self):
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Broken.lean": "broken\n"})
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "repair")
        task_status = status["head"]["task"]
        self.assertEqual(task_status["kind"], "repair")
        self.assertEqual([d["module"] for d in task_status["diagnostics"]], ["TauCeti/Pr102.lean"])
        self.assertEqual(len(self.github.dispatches), 1, "no singleton or bisection builds")

        def script(task):
            self.assertEqual(task.kind, "repair")
            self.assertEqual(sorted(m.number for m in task.members), [101, 102, 103])
            self.assertEqual(task.diagnostics[0]["message"], "error in PR 102")
            self.assertEqual(task.onto, task.base)
            return {
                f"{PROJECT}#102": Resolution(
                    {"Broken.lean": "fixed\n"},
                    explanation="Fixed the broken declaration so the batch compiles (conflict with #101).",
                )
            }

        agent = self.agent(script=script)
        self.github.ci_policy = verdicts()
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual(
            adoption.provenance, {f"{PROJECT}#101": "clean", f"{PROJECT}#102": "edited", f"{PROJECT}#103": "clean"}
        )
        self.assertEqual(adoption.version, head["version"] + 1)
        prior = {c.source_pr: c.message for c in chain.chain_commits(self.repo.clone, head["base"], head["tip"])}
        replaced = {c.source_pr: c.message for c in self.chain_commits(self.head())}
        for source_pr, message in prior.items():
            self.assertTrue(replaced[source_pr].startswith(message.rstrip()), source_pr)
        self.assertIn("Fixed the broken declaration", replaced[f"{PROJECT}#102"])
        self.assertEqual(replaced[f"{PROJECT}#101"], prior[f"{PROJECT}#101"])
        # The replacement runs CI again and lands; the comment is built from the message.
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": head["id"]})["tip"])
        comment = self.github.comments_for(102)[0]["body"]
        self.assertIn("Fixed the broken declaration", comment)
        self.assertIn("Related PRs: [#101]", comment)
        self.assertIn("/commit/", comment)
        self.assertEqual(agent.invocations, 1)
        self.assertEqual(self.controller.status()["counters"]["repair_rounds"], 1)

    def test_s2_rebase_conflict_yields_task_and_replacement_chain(self):
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        head_one = self.head()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        self.assertEqual([m["number"] for m in self.members(self.open_batch())], [102, 103])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head_one["tip"])
        head = self.head()
        self.assertEqual(head["id"], self.controller.batches()[1]["id"])
        status = self.controller.status()["head"]
        self.assertEqual(status["stage"], "rebase_conflict")
        self.assertEqual(status["task"]["kind"], "rebase_conflict")
        self.assertEqual(status["task"]["diagnostics"][0]["paths"], ["Shared.lean"])
        self.assertEqual(status["task"]["diagnostics"][0]["source_pr"], f"{PROJECT}#102")
        self.assertEqual(len(self.github.dispatches), 1, "a conflicting head is not dispatched")

        def script(task):
            self.assertEqual(task.kind, "rebase_conflict")
            self.assertEqual(task.onto, head_one["tip"])
            return {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"},
                    explanation="Merged the line two edits of #101 and this PR.",
                )
            }

        agent = self.agent(script=script)
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        head = self.head()
        self.assertEqual(head["base"], head_one["tip"])
        self.assertEqual(adoption.provenance[f"{PROJECT}#103"], "clean")
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(
            self.repo.file_at("main", "Shared.lean"), "line one\nline two changed by 101 and 102\nline three\n"
        )
        self.assertEqual(len(self.github.comments_for(103)), 1)
        self.assertNotIn("edited this contribution", self.github.comments_for(103)[0]["body"])
        self.assertIn("edited this contribution", self.github.comments_for(102)[0]["body"])

    def test_s14_shrink_to_green_lands_with_an_empty_commit_and_names_what_was_dropped(self):
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Hopeless.lean": "nope\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    empty=True,
                    explanation="Dropped Hopeless.lean: it cannot compile against #101; contribution reduced to an empty commit.",
                )
            }
        )
        self.github.ci_policy = verdicts()
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        commits = {c.source_pr: c for c in self.chain_commits(self.head())}
        empty = commits[f"{PROJECT}#102"]
        self.assertEqual(empty.tree, chain.tree_of(self.repo.clone, empty.parents[0]))
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": head["id"]})["tip"])
        self.assertIsNone(self.repo.file_at("main", "Hopeless.lean"))
        self.assertIn("Dropped Hopeless.lean", self.github.comments_for(102)[0]["body"])
        self.assertEqual(self.controller.status()["counters"]["shrink_to_empty"], 1)
        self.assertEqual(self.controller.inputs()[1]["state"], "landed")

    def test_s17_two_source_failures_land_with_three_full_runs_and_no_singletons(self):
        self.github.ci_policy = fail_members(103, 105)
        for number in range(101, 107):
            self.approve(number, {f"F{number}.lean": f"{number}\n"})
        self.tick()
        head = self.head()
        self.assertEqual(len(self.members(head)), 6)
        fixed = set()

        def script(task):
            failing = {int(d["module"].split("Pr")[1].split(".")[0]) for d in task.diagnostics}
            number = min(failing)  # one failure per round, as a cautious agent might
            fixed.add(number)
            return {
                f"{PROJECT}#{number}": Resolution({f"F{number}.lean": "fixed\n"}, explanation=f"Fixed PR {number}.")
            }

        self.github.ci_policy = fail_members(103, 105)
        policy = self.github.ci_policy

        def repaired_policy(head_sha, base_sha, members):
            still = [n for n in (103, 105) if n not in fixed]
            return fail_members(*still)(head_sha, base_sha, members)

        self.github.ci_policy = repaired_policy
        rounds = self.drive(agents=[self.agent(script=script)])
        self.assertLess(rounds, 12)
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertEqual(len(self.github.dispatches), 3)
        self.assertTrue(all(len(d["client_payload"]["members"]) == 6 for d in self.github.dispatches))
        self.assertEqual(self.controller.status()["counters"]["repair_rounds"], 2)
        del policy

    def test_s4_edit_without_explanation_is_rejected_and_the_same_attempt_adds_it(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.run_ci()
        self.tick()
        silent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "fixed\n"})}, omit_explanation=True
        )
        rejection = silent.run(self.controller)
        self.assertFalse(rejection.accepted)
        self.assertEqual(rejection.reason, "explanation-required")
        self.assertEqual(rejection.violations[0]["code"], "explanation-required")
        self.assertTrue(chain.object_exists(self.repo.origin, rejection.tip), "rejected commits stay retained")
        task = silent.tasks[-1]
        # The same claim adds the explanation and resubmits.
        silent.omit_explanation = False
        tip = silent.produce(task)
        adoption = self.controller.submit_task(task.task_id, task.claim_token, tip)
        self.assertTrue(adoption.accepted, adoption)


class Claims(QueueFixture):
    def failing_head(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        return self.head()

    def test_s6_s11_claims_expiry_late_results_and_second_claimants(self):
        head = self.failing_head()
        self.assertIsNone(self.controller.claim_task("intruder", "not-an-admin"), "authorization is repository admin")
        first = self.agent(
            "first",
            "admin-worker",
            script=lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "first\n"}, explanation="First fix.")},
        )
        second = self.agent(
            "second",
            "second-admin",
            script=lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "second\n"}, explanation="Second fix.")},
        )
        task = first.claim(self.controller)
        self.assertIsNotNone(task)
        self.assertIsNone(second.claim(self.controller), "repair_concurrency 1: a second claimant is refused")
        self.assertEqual(self.controller.status()["counters"]["claim_collisions"], 1)
        # The first worker's claim expires; the second claims; the first's late result is rejected but retained.
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        self.tick()
        late_tip = first.produce(task)
        second_task = second.claim(self.controller)
        self.assertIsNotNone(second_task)
        self.assertEqual(second_task.task_id, task.task_id)
        late = self.controller.submit_task(task.task_id, task.claim_token, late_tip)
        self.assertFalse(late.accepted)
        self.assertEqual(late.reason, "claim-expired")
        self.assertTrue(chain.object_exists(self.repo.origin, late_tip))
        self.assertEqual(self.head()["version"], head["version"], "a late result cannot overwrite state")
        adoption = self.controller.submit_task(
            second_task.task_id, second_task.claim_token, second.produce(second_task)
        )
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual(self.repo.file_at(self.head()["tip"], "A.lean"), "second\n")
        # After adoption the first worker's token is dead, members are neither duplicated nor lost.
        with self.assertRaises(TaskError):
            self.controller.renew_task(task.task_id, task.claim_token)
        self.assertEqual(sorted(m["number"] for m in self.members(self.head())), [101, 102])
        report = chain.validate(
            self.repo.clone,
            self.head()["base"],
            self.head()["tip"],
            [m["source_pr"] for m in self.members(self.head())],
        )
        self.assertTrue(report.ok, report.violations)
        # A result for the superseded version is rejected as late.
        stale = self.controller.submit_task(second_task.task_id, second_task.claim_token, late_tip)
        self.assertFalse(stale.accepted)
        self.assertIn(stale.reason, ("claim-superseded", "version-mismatch"))

    def test_s11_renewal_keeps_a_claim_alive_and_release_frees_the_task(self):
        self.failing_head()
        agent = self.agent()
        task = agent.claim(self.controller)
        ttl = self.controller.setting("claim_ttl_seconds")
        self.clock.advance(ttl - 10)
        renewed = self.controller.renew_task(task.task_id, task.claim_token)
        self.assertEqual(renewed, self.clock() + ttl)
        self.clock.advance(ttl - 10)
        self.assertIsNone(self.agent("other", "second-admin").claim(self.controller))
        self.controller.release_task(task.task_id, task.claim_token, "worker-shutdown")
        self.assertIsNotNone(self.agent("other", "second-admin").claim(self.controller))

    def test_s6_reservation_and_duplicate_approvals_cannot_insert_a_pr_twice(self):
        head_sha = self.approve(101, {"A.lean": "a\n"})
        again = self.controller.approve(PROJECT, 101, head_sha, "reviewer")
        self.assertEqual(again["reason"], "already-approved")
        self.assertEqual(self.controller.store.count("inputs"), 1)
        self.tick()
        self.assertEqual(self.controller.approve(PROJECT, 101, head_sha, "reviewer")["reason"], "member")
        self.assertEqual(self.controller.store.count("members"), 1)
        # A different head while a member: refused, the member stays.
        new_head = self.github.push_pr(101, {"A.lean": "a2\n"})
        self.assertEqual(self.controller.approve(PROJECT, 101, new_head, "reviewer")["reason"], "member")
        self.assertEqual(self.controller.store.count("members"), 1)


class HeadOperations(QueueFixture):
    def test_s10_admin_advance_sends_the_head_back_to_rebase_and_promotion_adds_no_merge_commit(self):
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        old_base = head["base"]
        self.github.start_runs()
        admin = self.repo.advance_main({"Admin.lean": "x\n"})
        self.github.complete_runs()
        self.tick()  # the pass fetched main and sees the advance before reading the verdict
        head = self.head()
        self.assertEqual(head["base"], admin)
        self.assertEqual(head["version"], 3)
        self.assertNotEqual(self.repo.main_sha(), head["tip"])
        self.assertEqual(len(self.github.dispatches), 2, "the rebased head needs its own run")
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"])
        for commit in self.first_parent_chain(old_base, self.repo.main_sha()):
            self.assertEqual(len(commit.parents), 1)
        self.assertEqual(
            sorted(c.source_pr for c in self.first_parent_chain(admin, self.repo.main_sha())),
            [f"{PROJECT}#101", f"{PROJECT}#102"],
        )

    def test_member_landed_by_an_admin_merge_rebases_to_an_empty_commit_and_the_head_lands(self):
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        before = self.chain_commits(head)
        self.github.start_runs()
        # An admin squash-merges PR 101 on GitHub (subject suffix, no trailer) and then edits the same
        # line again; the plain cherry-pick of 101's commit cannot take that.
        landing = self.repo.advance_main(
            {"Shared.lean": "line one\nline two changed by 101\nline three\n"}, message="Change 101 (#101)"
        )
        admin = self.repo.advance_main(
            {"Shared.lean": "line one\nline two changed by 101, then by admin\nline three\n"},
            message="admin edits line two",
        )
        self.github.complete_runs()
        self.tick()
        prior = next(c for c in before if c.source_pr == f"{PROJECT}#101")
        self.assertFalse(chain.merge_trees(self.repo.clone, admin, prior.sha, merge_base=prior.parents[0]).clean)
        head = self.head()
        self.assertEqual(head["base"], admin)
        self.assertEqual(self.controller.store.count("tasks"), 0, "no rebase-conflict task")
        self.assertNotEqual(self.controller.status()["head"]["stage"], "rebase_conflict")
        commits = self.chain_commits(head)
        self.assertEqual([c.source_pr for c in commits], [c.source_pr for c in before], "same members, same order")
        landed_member = next(c for c in commits if c.source_pr == f"{PROJECT}#101")
        self.assertEqual(landed_member.tree, chain.tree_of(self.repo.clone, landed_member.parents[0]), "101 is empty")
        self.assertEqual(landed_member.message.splitlines()[0], prior.message.splitlines()[0])
        self.assertIn(f"Landed on main as {landing} outside the queue; nothing left to apply.", landed_member.message)
        other = next(c for c in commits if c.source_pr == f"{PROJECT}#102")
        self.assertEqual(other.message, next(c for c in before if c.source_pr == f"{PROJECT}#102").message)
        rebased = self.controller.store.journal_entries("rebased", head["id"])[-1]
        self.assertEqual(rebased["detail"]["landed_on_base"], [{"source_pr": f"{PROJECT}#101", "commit": landing}])
        self.assertEqual([m["provenance"] for m in self.members(head)], ["clean", "clean"])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"])
        self.assertEqual(
            self.repo.file_at("main", "Shared.lean"), "line one\nline two changed by 101, then by admin\nline three\n"
        )
        self.assertEqual(self.repo.file_at("main", "B.lean"), "b\n")
        self.assertEqual(
            sorted(c.source_pr for c in self.first_parent_chain(admin, self.repo.main_sha())),
            [f"{PROJECT}#101", f"{PROJECT}#102"],
        )

    def test_s10_promotion_refused_after_an_advance_between_pass_and_push(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.github.fail_next["fast_forward"] = 1  # the first push attempt fails, main advances meanwhile
        self.tick()
        self.assertEqual(self.controller.status()["outbox"]["pending"], 1)
        admin = self.repo.advance_main({"Admin.lean": "x\n"})
        self.clock.advance(3600)
        self.tick()
        entry = self.controller.store.row("outbox", {"kind": "promote"})
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["result"].startswith("parent-changed"))
        self.assertEqual(self.head()["base"], admin)
        self.assertEqual(self.controller.status()["counters"]["promotion_refused_parent_changed"], 1)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": head["id"]})["tip"])

    def test_s10_lost_promotion_response_is_reconciled_from_main(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.github.lose_fast_forward_response = True
        self.tick()
        self.assertEqual(self.repo.main_sha(), head["tip"], "the push happened")
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "head")
        self.tick()
        landed = self.controller.store.row("batches", {"id": head["id"]})
        self.assertEqual((landed["state"], landed["landed_how"]), ("landed", "promoted"))
        self.assertEqual(self.controller.store.count("journal", {"kind": "landed"}), 1)
        self.assertEqual([c for c, _ in self.github.calls].count("fast_forward"), 1)

    def test_s10_s19_stale_status_cannot_approve_changed_source(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        first_tip = self.head()["tip"]
        self.github.start_runs()
        self.repo.advance_main({"Admin.lean": "x\n"})
        self.github.complete_runs()  # the first run passes, but for the old tip
        self.tick()
        head = self.head()
        self.assertNotEqual(head["tip"], first_tip)
        # A success status on the new tip from the OLD run (a stale callback) is not a verdict.
        self.github.post_status(head["tip"], "success", self.github.runs[0].run_id, "stale")
        self.github.runs[-1].status = "completed"
        self.github.runs[-1].conclusion = "success"
        self.controller.handle_event("status", {"sha": head["tip"], "context": "build", "state": "success"})
        self.tick()
        self.assertNotEqual(self.repo.main_sha(), head["tip"])
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"], "batch_version": head["version"]})
        self.assertEqual(attempt["outcome"], "infrastructure")
        self.assertIn("missing-status", attempt["outcome_detail"])
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted", "the approval is intact")

    def test_s19_pr_events_and_branch_pushes_never_cancel_a_candidate_or_post_its_verdict(self):
        head_sha = self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        self.github.start_runs()
        new_head = self.github.push_pr(101, {"A.lean": "a-pushed\n"})
        self.controller.handle_event(
            "pull_request",
            {"action": "synchronize", "number": 101, "pull_request": {"number": 101, "head": {"sha": new_head}}},
        )
        self.controller.handle_event("push", {"ref": "refs/heads/feature-101", "after": new_head})
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 102, "pull_request": {"number": 102, "merged": False}}
        )
        self.tick()
        self.assertEqual(self.github.runs[0].status, "in_progress")
        self.assertEqual(self.head()["tip"], head["tip"])
        self.assertEqual({m["number"]: m["accepted_head"] for m in self.members(head)}[101], head_sha)
        self.assertEqual([i["state"] for i in self.controller.inputs()], ["inserted", "inserted"])
        # A cancelled run leaves the candidate unchanged; the controller re-dispatches on its own schedule.
        self.github.cancel_runs(head["tip"])
        self.controller.handle_event("workflow_run", {"action": "completed", "workflow_run": {"head_sha": head["tip"]}})
        self.tick()
        self.assertEqual(self.head()["tip"], head["tip"])
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")
        self.assertEqual((attempt["outcome"], attempt["outcome_detail"]), ("infrastructure", "run-cancelled"))
        self.clock.advance(self.controller.setting("infrastructure_retry_backoff_seconds") + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2)
        self.assertEqual(self.github.dispatches[1]["client_payload"]["head_sha"], head["tip"])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"])
        self.assertEqual(self.repo.file_at("main", "A.lean"), "a\n", "the accepted head landed, not the later push")

    def test_infrastructure_failures_are_retried_with_backoff_and_bounded(self):
        self.github.ci_policy = verdicts("infrastructure", "lost", "no-status", "infrastructure")
        self.controller.set_setting("infrastructure_retry_limit", 3, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        backoff = self.controller.setting("infrastructure_retry_backoff_seconds")
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "infrastructure")
        self.clock.advance(backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2)
        self.clock.advance(self.controller.setting("lost_run_seconds") + 1)  # the lost run never starts
        self.tick()
        self.assertIn("lost-run", self.controller.status()["head"]["ci"]["outcome_detail"])
        self.clock.advance(2 * backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 3)
        self.run_ci()
        self.tick()
        self.assertIn("missing-status", self.controller.status()["head"]["ci"]["outcome_detail"])
        self.clock.advance(4 * backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 4)
        self.run_ci()
        self.clock.advance(8 * backoff + 1)
        reasons = self.tick()
        self.assertEqual(len(self.github.dispatches), 4, "bounded: the fourth infrastructure failure stops re-dispatch")
        self.assertIn("retries exhausted", " ".join(reasons))
        self.assertEqual(self.head()["id"], head["id"], "the head waits with its membership intact")
        self.assertEqual(
            self.controller.status()["counters"].get("tasks_repair", 0), 0, "infrastructure never makes a repair task"
        )
        self.controller.retry_ci(PROJECT, "operator")
        self.tick()
        self.assertEqual(len(self.github.dispatches), 5)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"])

    def test_failed_run_without_a_readable_log_yet_is_observed_again_not_classified(self):
        # GitHub publishes job logs a little after a run completes: the adapter then reports only a
        # `log_unavailable` infrastructure finding. That is not a verdict; the next observation decides.
        self.github.ci_policy = lambda *_: (
            "fail",
            [
                {
                    "kind": "infrastructure",
                    "module": "",
                    "message": "log of job 1 unavailable",
                    "reason": "log_unavailable",
                }
            ],
        )
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        reasons = self.tick()
        self.assertIn("waiting for its log", " ".join(reasons))
        attempt = self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")
        self.assertEqual(attempt["outcome"], "pending")
        self.assertEqual(len(self.github.dispatches), 1, "no re-dispatch while the log is pending")
        # The log appears: the same run now yields the compiler finding and a repair task opens.
        with self.github.lock:
            self.github.runs[-1].diagnostics = [
                {"kind": "compiler", "module": "TauCeti/A.lean", "message": "error in A"}
            ]
        self.clock.advance(self.controller.setting("polling_interval_seconds") + 1)
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        self.assertEqual(self.controller.status()["head"]["task"]["diagnostics"][0]["module"], "TauCeti/A.lean")

    def test_s9_conflicting_pr_is_probed_again_and_inserts_once_its_batch_mate_landed(self):
        self.controller.set_setting("open_batch_limit", 1, "t")  # increment 1: one open batch
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        self.assertEqual(self.controller.inputs()[2]["state"], "waiting")
        self.assertIn("Shared.lean", self.controller.inputs()[2]["last_probe"])
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-conflict"}), 1)
        self.assertEqual(
            self.controller.status()["counters"].get("tasks_rebase_conflict", 0), 0, "no agent for insertion"
        )
        # The first head lands; 102 seals as the head and its hopeless edit is shrunk by repair; 103 is probed again.
        self.run_ci()
        self.tick(2)
        head = self.head()
        self.assertEqual([m["number"] for m in self.members(head)], [102])
        self.assertEqual(self.controller.inputs()[2]["state"], "waiting")
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda t: {f"{PROJECT}#102": Resolution(empty=True, explanation="Dropped: the edit cannot build.")}
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertEqual(self.controller.inputs()[2]["state"], "inserted")
        self.assertEqual(self.controller.inputs()[2]["probes"] >= 2, True)
        self.assertEqual(agent.invocations, 1)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "Shared.lean"), "line one\nline two by 103\nline three\n")

    def test_conflicting_pr_is_not_probed_again_until_the_batch_base_changes(self):
        self.controller.set_setting("open_batch_limit", 1, "t")  # one open batch: a conflicting input waits
        self.approve(101, {"A.lean": "a\n"})
        self.tick()  # head 101
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        conflicted = self.controller.inputs()[2]
        self.assertEqual(conflicted["state"], "waiting")
        probes = conflicted["probes"]
        open_batch = self.controller.store.row("batches", {"state": "open"})
        self.assertEqual(conflicted["last_probe_base"], open_batch["base"])
        # Appending members moves the tip but cannot resolve a conflict with content already in the chain.
        for number, path in ((104, "B.lean"), (105, "C.lean")):
            self.approve(number, {path: "x\n"})
            self.tick()
        self.assertEqual(self.controller.inputs()[3]["state"], "inserted")
        self.assertEqual(self.controller.inputs()[4]["state"], "inserted")
        self.assertNotEqual(self.controller.store.row("batches", {"state": "open"})["tip"], open_batch["tip"])
        self.assertEqual(self.controller.inputs()[2]["probes"], probes, "no re-probe while the base is unchanged")
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-conflict"}), 1)
        # The head lands, the open batch seals and a new open batch on the new base re-probes 103.
        self.run_ci()
        self.tick(2)
        self.assertEqual([m["number"] for m in self.members(self.head())], [102, 104, 105])
        self.tick()
        self.assertGreater(self.controller.inputs()[2]["probes"], probes, "re-probed against the new base")
        self.assertEqual(self.controller.inputs()[2]["state"], "inserted", "clean against the new base")

    def test_pr_conflicting_with_main_itself_gets_the_conflict_request(self):
        self.repo.advance_main({"Shared.lean": "line one\nline two by main\nline three\n"})
        self.approve(
            101, {"Shared.lean": "line one\nline two by 101\nline three\n"}, start=self.repo.rev_parse("main~1")
        )
        self.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "cancelled")
        self.assertEqual(self.controller.inputs()[0]["reason"], "conflicts-with-main")
        self.tick()
        self.assertIn("conflicts with `main`", self.github.comments_for(101)[0]["body"])
        self.assertIsNone(self.head())


class OpenBatches(QueueFixture):
    """A step beyond increment 1 (operator decision, 2026-11-05): up to `open_batch_limit` open batches."""

    def open_batches(self):
        return self.controller.batches("open")

    def test_conflicting_pr_is_assembled_into_a_second_open_batch_based_on_canonical(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()  # head 101
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        batch_a = self.open_batch()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        # 103 conflicts with 102 in batch A and is assembled into batch B in the same pass.
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-conflict"}), 1)
        self.assertEqual(self.controller.inputs()[2]["state"], "inserted")
        batch_a, batch_b = self.open_batches()
        self.assertEqual(self.controller.inputs()[2]["batch_id"], batch_b["id"])
        self.assertEqual([m["number"] for m in self.members(batch_a)], [102])
        self.assertEqual([m["number"] for m in self.members(batch_b)], [103])
        self.assertEqual(batch_b["base"], self.repo.main_sha(), "based on the canonical commit")
        self.assertNotEqual(batch_b["base"], batch_a["tip"], "never stacked on another open batch")
        self.assertEqual(self.controller.inputs()[2]["last_probe"], "inserted")
        status = self.controller.status()
        self.assertEqual(status["open_batch"]["id"], batch_a["id"], "the first open batch, for compatibility")
        self.assertEqual(
            [(b["id"], b["size"], b["base"]) for b in status["open_batches"]],
            [(batch_a["id"], 1, batch_a["base"]), (batch_b["id"], 1, batch_b["base"])],
        )
        self.assertIsNotNone(status["open_batches"][0]["oldest_member_age_seconds"])
        source = feeds.ControllerFeedSource(self.controller)
        self.assertEqual(
            {(view.id, view.state) for view in source.active_batches(PROJECT, "main") if not view.sealed},
            {(batch_a["id"], "waiting"), (batch_b["id"], "waiting")},
        )
        # A clean input still goes into the first open batch; a third open batch is never created.
        self.approve(104, {"D.lean": "d\n"})
        self.tick()
        self.assertEqual([m["number"] for m in self.members(self.open_batch())], [102, 104])
        self.assertEqual(len(self.open_batches()), 2)
        # The head lands: A (the oldest) seals, B stays open and keeps receiving insertions.
        self.run_ci()
        self.tick(2)
        head = self.head()
        self.assertEqual(head["id"], batch_a["id"])
        self.assertEqual([m["number"] for m in self.members(head)], [102, 104])
        self.assertEqual([b["id"] for b in self.open_batches()], [batch_b["id"]])
        self.approve(105, {"E.lean": "e\n"})
        self.tick()
        self.assertEqual([m["number"] for m in self.members(self.open_batch())], [103, 105])
        # A lands; B seals and its conflict with 102's content is the head's rebase conflict task.
        self.run_ci()
        self.tick(2)
        head = self.head()
        self.assertEqual(head["id"], batch_b["id"])
        status = self.controller.status()["head"]
        self.assertEqual(status["stage"], "rebase_conflict")
        self.assertEqual(status["task"]["diagnostics"][0]["source_pr"], f"{PROJECT}#103")
        self.assertEqual(status["task"]["diagnostics"][0]["paths"], ["Shared.lean"])
        agent = self.agent(
            script=lambda t: {
                f"{PROJECT}#103": Resolution(
                    {"Shared.lean": "line one\nline two by 102 and 103\nline three\n"},
                    explanation="Merged the line two edits of #102 and this PR.",
                )
            }
        )
        self.assertTrue(agent.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "Shared.lean"), "line one\nline two by 102 and 103\nline three\n")
        self.assertEqual(self.repo.file_at("main", "E.lean"), "e\n")
        self.assertEqual({row["state"] for row in self.controller.inputs()}, {"landed"})

    def test_open_batch_limit_one_keeps_the_conflicting_pr_waiting(self):
        self.controller.set_setting("open_batch_limit", 1, "operator")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick(3)
        self.assertEqual(self.controller.inputs()[2]["state"], "waiting")
        self.assertEqual(self.controller.inputs()[2]["probes"], 1)
        self.assertEqual(len(self.open_batches()), 1)
        self.assertEqual(len(self.controller.status()["open_batches"]), 1)
        # Raising the limit assembles it into a second batch on the next pass.
        self.controller.set_setting("open_batch_limit", 2, "operator")
        self.tick()
        self.assertEqual(self.controller.inputs()[2]["state"], "inserted")
        self.assertEqual(len(self.open_batches()), 2)

    def test_pr_conflicting_with_every_open_batch_waits_until_one_seals(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        batch_a, batch_b = self.open_batches()
        self.approve(104, {"Shared.lean": "line one\nline two by 104\nline three\n"})
        self.tick()
        waiting = self.controller.inputs()[3]
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["probes"], 2, "probed once against each open batch")
        self.assertEqual(len(self.open_batches()), 2, "no third batch beyond the limit")
        self.tick(2)
        self.assertEqual(self.controller.inputs()[3]["probes"], 2, "not probed again while both bases stand")
        # The head lands and A seals: A's content is no longer in an open batch, so 104 is assembled into a
        # new open batch on the canonical commit (as it would be in increment 1 once the batch sealed); its
        # conflict with 102 resurfaces at that batch's rebase, as a `rebase_conflict` task for the agent.
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.head()["id"], batch_a["id"])
        self.assertEqual([b["id"] for b in self.open_batches()], [batch_b["id"]])
        self.assertEqual(self.controller.inputs()[3]["state"], "waiting", "A sealed after this tick's insertions")
        self.tick()
        self.assertEqual(self.controller.inputs()[3]["state"], "inserted")
        batch_b, batch_c = self.open_batches()
        self.assertEqual(self.controller.inputs()[3]["batch_id"], batch_c["id"])
        self.assertEqual(batch_c["base"], self.repo.main_sha())
        self.assertNotEqual(batch_c["base"], self.head()["tip"], "never stacked on the unlanded head")

    def test_empty_second_open_batch_is_removed(self):
        self.repo.advance_main({"Shared.lean": "line one\nline two by main\nline three\n"})
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        batch_a = self.open_batch()
        # 103 conflicts with 102 in batch A; on the second batch, fresh on the canonical commit, the
        # conflict is with main itself, so the input is cancelled and the empty batch removed.
        self.approve(
            103, {"Shared.lean": "line one\nline two by 103\nline three\n"}, start=self.repo.rev_parse("main~1")
        )
        self.tick()
        self.assertEqual(self.controller.inputs()[2]["state"], "cancelled")
        self.assertEqual(self.controller.inputs()[2]["reason"], "conflicts-with-main")
        self.assertEqual([b["id"] for b in self.open_batches()], [batch_a["id"]])
        removed = self.controller.store.journal_entries("batch-removed")
        self.assertEqual([entry["detail"]["reason"] for entry in removed], ["idle"])
        self.assertEqual(self.controller.status()["open_batch"]["id"], batch_a["id"])
        self.assertEqual(len(self.controller.status()["open_batches"]), 1)

    def test_restart_attributes_a_legacy_conflict_record_to_the_open_batch(self):
        self.controller.set_setting("open_batch_limit", 1, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        conflicted = self.controller.inputs()[2]
        self.assertEqual(conflicted["state"], "waiting")
        batch_a = self.open_batch()
        # A database written before `probe_conflicts` existed records the conflict by base only.
        self.controller.store.update("inputs", {"id": conflicted["id"]}, probe_conflicts="{}")
        self.controller.set_setting("open_batch_limit", 2, "t")
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        self.assertEqual(json.loads(restarted.inputs()[2]["probe_conflicts"]), {str(batch_a["id"]): batch_a["base"]})
        restarted.tick()
        self.assertEqual(restarted.inputs()[2]["probes"], 2, "not re-probed against A; probed against B")
        self.assertEqual(restarted.inputs()[2]["state"], "inserted")
        self.assertEqual(len(restarted.batches("open")), 2)


class Lanes(QueueFixture):
    def test_s20_urgent_pr_is_in_the_next_head_without_cancelling_the_running_one(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        running = self.head()
        self.github.start_runs()
        self.controller.set_setting("insertion_age_window", 2, "t")
        for number in range(102, 108):
            self.approve(number, {f"F{number}.lean": "x\n"})
        self.approve(199, {"Urgent.lean": "now\n"})
        self.assertTrue(self.controller.mark_urgent(PROJECT, 199, "operator")["urgent"])
        self.tick()
        self.assertEqual(self.members(self.open_batch())[0]["number"], 199)
        self.assertTrue(self.open_batch()["urgent"])
        self.assertEqual(self.head()["id"], running["id"])
        self.assertEqual(self.github.runs[0].status, "in_progress")
        for run in self.github.complete_runs():
            self.controller.handle_event("status", {"sha": run.head_sha, "context": "build", "state": "success"})
        self.tick(2)
        head = self.head()
        self.assertIn(199, [m["number"] for m in self.members(head)])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "Urgent.lean"), "now\n")
        self.assertEqual(
            [c for c, _ in self.github.calls].count("fast_forward"), 2, "landed through the queue, no admin merge"
        )

    def test_s18_no_pr_waits_more_than_the_window_in_passes(self):
        self.controller.set_setting("insertion_age_window", 3, "t")
        self.github.ci_policy = verdicts("fail")
        self.approve(100, {"A.lean": "a\n"})
        self.tick()  # a head that will not land for a while
        self.run_ci()
        self.tick()
        for number in range(101, 125):
            self.approve(number, {f"F{number}.lean": "x\n"})
        self.tick(3)
        drawn = [
            e["detail"]["number"]
            for e in self.controller.store.journal_entries("inserted")
            if e["detail"]["number"] != 100
        ]
        self.assertEqual(sorted(drawn), list(range(101, 125)))
        self.assertLessEqual(self.controller.status()["max_passes_waited"], 3)
        self.assertEqual(len(self.members(self.open_batch())), 24)
        # The draw is random inside the window: not simply FIFO.
        self.assertNotEqual(drawn, sorted(drawn))
        self.assertTrue(all(abs(drawn.index(n) - (n - 101)) <= 3 for n in drawn), drawn)

    def test_s16_s21_problem_reports_closures_and_relabelling_do_not_withdraw_a_member(self):
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        head = self.head()
        self.assertEqual(self.controller.unapprove(PROJECT, 101, "reviewer")["reason"], "member")
        self.assertEqual(self.controller.cancel(PROJECT, 101, "author")["reason"], "member")
        self.github.prs[101]["state"] = "closed"  # a housekeeping pipeline closed it
        self.github.labels[101] = {"awaiting-author"}
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 101, "pull_request": {"number": 101}}
        )
        self.controller.handle_event(
            "pull_request", {"action": "unlabeled", "number": 101, "pull_request": {"number": 101}}
        )
        self.tick()
        self.assertEqual(sorted(m["number"] for m in self.members(head)), [101, 102])
        self.assertEqual(self.controller.store.count("journal", {"kind": "member-event-ignored"}), 2)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "A.lean"), "a\n")
        self.assertEqual(
            self.controller.store.row("outbox", {"key": f"landed-close:{head['id']}:101"})["result"], "already-closed"
        )
        # A waiting (not yet staged) input is withdrawn by r- and by a closure, as with Bors.
        self.approve(103, {"C.lean": "c\n"})
        self.assertTrue(self.controller.unapprove(PROJECT, 103)["withdrawn"])
        head_sha = self.approve(104, {"D.lean": "d\n"})
        self.controller.handle_event(
            "pull_request",
            {"action": "closed", "number": 104, "pull_request": {"number": 104, "head": {"sha": head_sha}}},
        )
        self.assertEqual({i["number"]: i["state"] for i in self.controller.inputs()}[104], "cancelled")


class Holds(QueueFixture):
    def test_s13_inference_hold_stops_sealing_keeps_the_head_and_resumes_on_success(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        agent = self.agent(script=lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "fixed\n"}, explanation="Fixed.")})
        threshold = self.controller.setting("inference_failure_threshold")
        for n in range(threshold):
            task = agent.claim(self.controller)
            self.assertIsNotNone(task, "blocked tasks stay exposed")
            self.controller.release_task(task.task_id, task.claim_token, "inference_unavailable", f"provider error {n}")
        hold = self.controller.status()["inference_hold"]
        self.assertIsNotNone(hold)
        self.assertEqual(hold["since"], self.clock())
        self.assertEqual(hold["last_error"], f"provider error {threshold - 1}")
        members_before = [(m["number"], m["commit_sha"], m["provenance"]) for m in self.members(head)]
        self.clock.advance(5000)
        reasons = self.tick()
        self.assertIn("inference hold", " ".join(reasons))
        self.assertIn("inference hold older than an hour", " ".join(self.controller.status()["alerts"]))
        self.assertEqual([(m["number"], m["commit_sha"], m["provenance"]) for m in self.members(head)], members_before)
        self.assertEqual(self.head()["id"], head["id"])
        self.assertTrue(chain.object_exists(self.repo.origin, head["tip"]))
        # The hold ends with the first completed task; the queue resumes by itself.
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.assertIsNone(self.controller.status()["inference_hold"])
        self.assertEqual(self.controller.status()["counters"]["inference_holds"], 1)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "A.lean"), "fixed\n")

    def test_s13_hold_without_a_head_prevents_sealing_until_a_provider_call_succeeds(self):
        for _ in range(self.controller.setting("inference_failure_threshold")):
            self.controller.report_inference("worker", False, "credentials exhausted")
        self.approve(101, {"A.lean": "a\n"})
        reasons = self.tick()
        self.assertIsNone(self.head())
        self.assertEqual(len(self.members(self.open_batch())), 1, "insertion needs no inference and continues")
        self.assertIn("inference hold: not sealing", reasons)
        self.assertEqual(self.github.dispatches, [])
        self.controller.report_inference("worker", True)
        self.tick()
        self.assertIsNotNone(self.head())
        self.assertEqual(len(self.github.dispatches), 1)


class Restart(QueueFixture):
    def test_s12_restart_recovers_pending_outbox_expired_claim_and_repeats_no_work(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(script=lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "fixed\n"}, explanation="Fixed.")})
        task = agent.claim(self.controller)
        # A comment the first process could not deliver, and the worker's claim, outlive the process.
        self.github.fail_next["post_comment"] = 1
        self.controller._enqueue(
            "comment", "test:undelivered", {"number": 101, "body": "hello", "marker": "<!-- t -->"}
        )
        self.tick()
        self.assertEqual(self.controller.status()["outbox"]["pending"], 1)
        self.clock.advance(self.controller.setting("claim_ttl_seconds") + 1)
        dispatches = len(self.github.dispatches)
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        loop = ReconciliationLoop(restarted, max_interval=0.05)
        recovered = restarted.recover()
        self.assertEqual(recovered["outbox_pending"], 1)
        self.clock.advance(restarted.setting("outbox_retry_max_seconds"))
        loop.run_once(woken=True)
        self.assertEqual(restarted.status()["outbox"]["pending"], 0)
        self.assertEqual(self.github.comments_for(101)[0]["body"], "hello")
        self.assertEqual(len(self.github.dispatches), dispatches, "no repeated dispatch after restart")
        self.assertEqual(restarted.status()["head"]["task"]["active_claims"], [])
        with self.assertRaises(TaskError):
            restarted.renew_task(task.task_id, task.claim_token)
        self.assertEqual(restarted.status()["service"]["revision"], "fixture-restart")
        # The task is claimable again and the queue finishes under the new process.
        self.github.ci_policy = verdicts()
        adoption = agent.run(restarted)
        self.assertTrue(adoption.accepted, adoption)
        restarted.tick()
        self.run_ci()
        restarted.tick()
        restarted.tick()
        self.assertEqual(self.repo.file_at("main", "A.lean"), "fixed\n")

    def test_s12_loop_runs_passes_on_wake_and_on_the_polling_interval(self):
        self.controller.set_setting("polling_interval_seconds", 1, "t")
        loop = ReconciliationLoop(self.controller, max_interval=0.05)
        loop.start()
        try:
            self.approve(101, {"A.lean": "a\n"})
            self.wait_for(lambda: len(self.github.dispatches) == 1, "the sealed head is dispatched by the loop")
            self.run_ci()
            self.wait_for(lambda: self.batch_state(1) == "landed", "the loop observes the pass and promotes the head")
            self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": 1})["tip"])
            self.wait_for(lambda: len(self.github.closed) == 1, "the loop drains the landing side effects")
        finally:
            loop.stop()
        self.assertGreater(loop.passes, 1)
        self.assertIsNone(loop.last_error)

    def batch_state(self, batch_id):
        return self.controller.store.row("batches", {"id": batch_id})["state"]

    def wait_for(self, condition, what, timeout=60.0):
        """Poll `condition` until it holds; CI runners are slow and loaded, so the timeout is generous."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"timed out after {timeout:.0f}s waiting for {what}; reasons: {self.controller.status()['reasons']}")


class StatusAndComments(QueueFixture):
    def test_s22_status_names_runner_host_phase_progress_stall_and_revision(self):
        self.controller.set_setting("lost_run_seconds", 24 * 3600, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        status = self.controller.status()
        self.assertEqual(status["schema"], "tauceti-queue.status/v1")
        self.assertEqual(status["service"]["revision"], "fixture")
        self.assertEqual(status["head"]["stage"], "ci")
        self.github.start_runs()
        self.clock.advance(30)
        self.github.progress_runs("build", "modules 3/12")
        self.controller.handle_event(
            "check_run", {"action": "in_progress", "check_run": {"head_sha": self.head()["tip"]}}
        )
        self.tick()
        ci = self.controller.status()["head"]["ci"]
        self.assertEqual((ci["runner"], ci["host"]), ("fleet-runner-1", "chungus2"))
        self.assertEqual([p["name"] for p in ci["phases"]], ["preparation", "build"])
        self.assertEqual(ci["phases"][0]["status"], "completed")
        self.assertEqual(ci["phases"][1]["detail"], "modules 3/12")
        self.assertIsNone(self.controller.status()["head"]["stall"])
        stall_after = self.controller.setting("stall_interval_seconds")
        self.clock.advance(stall_after + 1)
        self.tick()
        stall = self.controller.status()["head"]["stall"]
        self.assertIsNotNone(stall)
        self.assertEqual(stall["phase"], "build")
        self.assertEqual(stall["since"], ci["last_progress_at"] + stall_after)
        self.clock.advance(stall_after + 1)
        self.tick()
        self.assertTrue(any("stalled" in a for a in self.controller.status()["alerts"]))
        import json

        json.dumps(self.controller.status())

    def test_s5_mechanical_rebase_makes_no_edit_comment_and_comment_delivery_retries_independently(self):
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda t: {f"{PROJECT}#102": Resolution({"B.lean": "fixed\n"}, explanation="Fixed B.")}
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.repo.advance_main({"Admin.lean": "x\n"})  # forces a mechanical rebase of both members
        self.tick()
        head = self.head()
        self.assertEqual({m["number"]: m["provenance"] for m in self.members(head)}, {101: "clean", 102: "edited"})
        self.run_ci()
        self.github.fail_next["post_comment"] = 1
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), head["tip"], "landing does not wait for comments")
        self.assertEqual(self.controller.status()["outbox"]["pending"], 1)
        self.clock.advance(self.controller.setting("outbox_retry_seconds") + 1)
        self.tick()
        self.assertEqual(self.controller.status()["outbox"]["pending"], 0)
        clean_comment = self.github.comments_for(101)[0]["body"]
        self.assertIn("Pull request successfully merged into main.", clean_comment)
        self.assertNotIn("edited this contribution", clean_comment)
        edited_comment = self.github.comments_for(102)[0]["body"]
        self.assertIn("edited this contribution", edited_comment)
        self.assertIn("> Fixed B.", edited_comment)
        self.assertIn(f"refs/tauceti-queue/batches/{head['id']}/v{head['version']}", edited_comment)
        self.assertTrue(chain.object_exists(self.repo.origin, head["tip"]))
        self.assertEqual(
            self.repo.rev_parse(f"refs/tauceti-queue/batches/{head['id']}/v{head['version']}"), head["tip"]
        )

    def test_shadow_mode_builds_batches_but_publishes_nothing(self):
        self.controller.set_setting("publication_enabled", False, "operator")
        self.approve(101, {"A.lean": "a\n"})
        reasons = self.tick()
        self.assertIsNotNone(self.head())
        self.assertEqual(self.github.dispatches, [])
        self.assertTrue(any("would dispatch" in r for r in reasons))
        writes = [
            c
            for c, _ in self.github.calls
            if c in ("dispatch_candidate", "fast_forward", "post_comment", "set_labels", "close_pr", "update_ref")
        ]
        self.assertEqual(writes, [])
        # Bors lands the same PR; the service observes the canonical advance and records its head as landed.
        member_commit = self.members(self.head())[0]["commit_sha"]
        self.github.update_ref(PROJECT, "refs/heads/main", member_commit)
        self.tick()
        landed = self.controller.store.row("batches", {"id": 1})
        self.assertEqual((landed["state"], landed["landed_how"]), ("landed", "external"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The queue controller's unit behaviours: settings, approval intake, events, the draw, tasks, holds, comments, status.

The end-to-end flows are in tests/queue_fixture.py; this file checks the controller's individual
decisions against the fake GitHub without driving whole head cycles.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import chain  # noqa: E402
from tauceti_queue.controller import (  # noqa: E402
    DEFAULT_SETTINGS,
    MERGED_TITLE_PREFIX,
    ControllerError,
    TaskError,
)
from tauceti_queue.fakes import Resolution, ScriptedAgent, make_controller  # noqa: E402
from tauceti_queue.ports import AGENT_INSTRUCTION  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")

    def approve(self, number, files, **kwargs):
        head = self.github.create_pr(number, files, **kwargs)
        return head, self.controller.approve(PROJECT, number, head, "reviewer")

    def failing_head(self):
        self.github.ci_policy = lambda *_: ("fail", [{"kind": "compiler", "module": "M.lean", "message": "boom"}])
        self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()
        self.github.complete_runs()
        self.controller.handle_event("status", {"sha": self.github.runs[0].head_sha, "context": "build"})
        self.controller.tick()
        return self.controller.store.row("batches", {"state": "head"})


class Settings(ControllerCase):
    def test_defaults_and_validation(self):
        self.assertEqual(self.controller.setting("insertion_age_window"), 16)
        self.assertEqual(self.controller.setting("repair_concurrency"), 1)
        self.assertEqual(self.controller.setting("stall_interval_seconds"), 900)
        self.assertTrue(self.controller.setting("publication_enabled"), "the fixture enables publication")
        self.assertFalse(DEFAULT_SETTINGS["publication_enabled"])
        self.controller.set_setting("repair_concurrency", 2, "kim")
        self.assertEqual(self.controller.status()["settings"]["repair_concurrency"], 2)
        for key, value in (
            ("repair_concurrency", -1),
            ("repair_concurrency", True),
            ("publication_enabled", 1),
            ("membership_label", 3),
        ):
            with self.assertRaises(ControllerError):
                self.controller.set_setting(key, value, "kim")
        with self.assertRaises(ControllerError):
            self.controller.setting("nope")
        entry = self.controller.store.journal_entries("setting")[-1]
        self.assertEqual((entry["detail"]["key"], entry["detail"]["actor"]), ("repair_concurrency", "kim"))

    def test_canonical_branch_is_discovered_from_the_clone(self):
        self.assertEqual(self.controller.canonical_branch, "main")


class Approvals(ControllerCase):
    def test_approval_records_the_input_and_retains_the_head(self):
        head, result = self.approve(101, {"A.lean": "a\n"}, title="Add A", body="Adds A.", author="bob")
        self.assertEqual(result["reason"], "approved")
        row = result["input"]
        self.assertEqual(
            (row["title"], row["description"], row["author"], row["reviewer"]), ("Add A", "Adds A.", "bob", "reviewer")
        )
        self.assertEqual(row["retained_ref"], f"refs/tauceti-queue/inputs/101/{head}")
        self.assertTrue(chain.object_exists(self.repo.clone, head))
        self.controller.tick()
        self.assertEqual(self.repo.rev_parse(row["retained_ref"]), head, "retained on GitHub through the outbox")

    def test_approval_refusals(self):
        head = self.github.create_pr(101, {"A.lean": "a\n"})
        self.assertEqual(self.controller.approve(PROJECT, 101, "0" * 40, "r")["reason"], "head-mismatch")
        self.github.prs[101]["draft"] = True
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "draft")
        self.github.prs[101]["draft"] = False
        self.github.prs[101]["state"] = "closed"
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "closed")
        self.github.prs[101]["state"] = "open"
        self.github.prs[101]["base_ref"] = "release"
        self.assertEqual(self.controller.approve(PROJECT, 101, head, "r")["reason"], "base-branch")
        with self.assertRaises(ControllerError):
            self.controller.approve("other/repo", 101, head, "r")
        self.assertEqual(self.controller.store.count("inputs"), 0)

    def test_new_head_supersedes_a_waiting_approval_and_import_keeps_original_age(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        new_head = self.github.push_pr(101, {"A.lean": "a2\n"})
        result = self.controller.approve(PROJECT, 101, new_head, "reviewer", priority=5, approved_at=1_700_000_000.0)
        self.assertEqual(result["reason"], "approved")
        rows = self.controller.inputs()
        self.assertEqual([(r["head_sha"], r["state"]) for r in rows], [(head, "cancelled"), (new_head, "waiting")])
        self.assertEqual(rows[0]["reason"], "superseded-by-new-approval")
        self.assertEqual((rows[1]["priority"], rows[1]["approved_at"]), (5, 1_700_000_000.0))
        self.assertEqual(self.controller.set_priority(PROJECT, 101, 9)["priority"], 9)
        self.assertEqual(self.controller.set_priority(PROJECT, 404, 9)["changed"], False)

    def test_waiting_inputs_react_to_events_and_members_do_not(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.handle_event(
            "pull_request",
            {"action": "synchronize", "number": 101, "pull_request": {"number": 101, "head": {"sha": "f" * 40}}},
        )
        self.assertEqual(self.controller.inputs()[0]["reason"], "head-changed")
        self.approve(102, {"B.lean": "b\n"})
        self.controller.handle_event(
            "pull_request", {"action": "converted_to_draft", "number": 102, "pull_request": {"number": 102}}
        )
        self.assertEqual(self.controller.inputs()[1]["reason"], "draft")
        self.approve(103, {"C.lean": "c\n"})
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 103, "pull_request": {"number": 103, "merged": True}}
        )
        self.assertEqual(self.controller.inputs()[2]["reason"], "merged-externally")
        self.approve(104, {"D.lean": "d\n"})
        self.controller.tick()
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 104, "pull_request": {"number": 104}}
        )
        self.assertEqual(self.controller.inputs()[3]["state"], "inserted")
        self.controller.handle_event("issue_comment", {"action": "created"})
        self.assertEqual(self.controller.store.count("journal", {"kind": "event"}), 5)

    def test_externally_landed_waiting_input_is_withdrawn_on_the_canonical_advance(self):
        head, _ = self.approve(101, {"A.lean": "a\n"})
        self.controller.tick()  # inserted, sealed, dispatched: a member now
        head2, _ = self.approve(102, {"B.lean": "b\n"})
        self.controller.tick()  # 102 is in the open batch
        head3, _ = self.approve(103, {"C.lean": "c\n"})
        # An admin lands 103's content with its Source-PR trailer directly on main.
        member = chain.Member(PROJECT, 103, head3, title="Change 103")
        self.controller.github.fetch_refs(PROJECT, [head3])
        built = chain.build(self.repo.clone, self.repo.main_sha(), [member], chain_id="admin")
        self.github.update_ref(PROJECT, "refs/heads/main", built.tip)
        self.controller.tick()
        states = {r["number"]: (r["state"], r["reason"]) for r in self.controller.inputs()}
        self.assertEqual(states[103], ("cancelled", "landed-externally"))
        self.assertEqual(states[102][0], "inserted", "members are kept")


class Draw(ControllerCase):
    def test_draw_is_random_inside_the_window_and_bounded(self):
        rows = [
            {"id": n, "number": n, "urgent": 0, "priority": 0, "approved_at": n, "passes_waited": 0}
            for n in range(1, 30)
        ]
        self.controller.set_setting("insertion_age_window", 4, "t")
        seen = set()
        for _ in range(50):
            seen.add(self.controller._draw(rows)["id"])
        self.assertEqual(seen, {1, 2, 3, 4})
        rows[2]["passes_waited"] = 4
        self.assertEqual(self.controller._draw(rows)["id"], 3, "an input that waited the window's passes is drawn")
        rows[1]["passes_waited"] = 4
        self.assertEqual(self.controller._draw(rows)["id"], 2, "the oldest overdue first")
        rows[10]["urgent"] = 1
        self.assertEqual(
            self.controller._draw(sorted(rows, key=lambda r: (-r["urgent"], r["approved_at"])))["id"],
            11,
            "urgent first",
        )


class Tasks(ControllerCase):
    def test_task_record_carries_members_diagnostics_refs_and_instruction(self):
        head = self.failing_head()
        self.assertIsNone(self.controller.claim_task("w", "nobody"))
        task = self.controller.claim_task("worker-1", "admin-worker")
        self.assertEqual(task.kind, "repair")
        self.assertEqual(
            (task.batch_id, task.batch_version, task.base, task.tip, task.onto),
            (head["id"], head["version"], head["base"], head["tip"], head["base"]),
        )
        self.assertEqual(task.diagnostics, [{"kind": "compiler", "module": "M.lean", "message": "boom"}])
        self.assertEqual(task.members[0].number, 101)
        self.assertEqual(task.members[0].retained_ref, f"refs/tauceti-queue/inputs/101/{task.members[0].accepted_head}")
        self.assertIn(f"refs/tauceti-queue/batches/{head['id']}/v{head['version']}", task.retained_refs)
        self.assertEqual(task.instruction, AGENT_INSTRUCTION)
        self.assertEqual(task.expires_at, self.clock() + self.controller.setting("claim_ttl_seconds"))
        json.dumps(task.to_dict())
        status = self.controller.status()["head"]
        self.assertEqual(status["claim"]["worker"], "worker-1")
        self.assertEqual(status["task"]["active_claims"][0]["worker"], "worker-1")

    def test_task_api_errors(self):
        head = self.failing_head()
        with self.assertRaises(TaskError) as caught:
            self.controller.renew_task(99, "x")
        self.assertEqual(caught.exception.code, "unknown-claim")
        task = self.controller.claim_task("worker-1", "admin-worker")
        with self.assertRaises(TaskError):
            self.controller.renew_task(task.task_id, "wrong-token")
        with self.assertRaises(TaskError):
            self.controller.submit_task(99, task.claim_token, head["tip"])
        with self.assertRaises(TaskError):
            self.controller.submit_task(task.task_id, "wrong-token", head["tip"])
        # Admin permission revoked between claim and submit.
        self.github.admins.discard("admin-worker")
        result = self.controller.submit_task(task.task_id, task.claim_token, head["tip"])
        self.assertEqual(result.reason, "not-authorized")
        self.github.admins.add("admin-worker")
        # An unchanged chain resubmitted is a valid (if pointless) adoption: nothing edited.
        result = self.controller.submit_task(task.task_id, task.claim_token, head["tip"])
        self.assertTrue(result.accepted)
        self.assertEqual(result.provenance, {f"{PROJECT}#101": "clean"})

    def test_invalid_chains_are_rejected_with_the_tool_violations(self):
        head = self.failing_head()
        task = self.controller.claim_task("worker-1", "admin-worker")
        # A chain that drops the member (tip == base) fails validation: count mismatch.
        result = self.controller.submit_task(task.task_id, task.claim_token, head["base"])
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("count-mismatch", {v["code"] for v in result.violations})
        # A chain with an extra commit for an unknown PR.
        stranger = chain.Member(PROJECT, 999, head["base"], title="not a member")
        built = chain.build(self.repo.clone, head["tip"], [stranger], chain_id="stranger")
        result = self.controller.submit_task(task.task_id, task.claim_token, built.tip)
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("unknown-member", {v["code"] for v in result.violations})
        self.assertEqual(self.controller.status()["counters"]["chain_validation_failures"], 2)
        self.assertTrue(any("failed validation 2 times" in a for a in self.controller.status()["alerts"]))
        # A commit whose message lost the prior text.
        commits = chain.chain_commits(self.repo.clone, head["base"], head["tip"])
        rewritten = chain.commit_tree(
            self.repo.clone,
            commits[0].tree,
            head["base"],
            f"rewritten\n\nSource-PR: {PROJECT}#101\nAccepted-Head: {commits[0].accepted_head}\n",
            ("a", "a@x"),
        )
        result = self.controller.submit_task(task.task_id, task.claim_token, rewritten)
        self.assertEqual(result.reason, "invalid-chain")
        self.assertIn("message-not-preserved", {v["code"] for v in result.violations})
        self.controller.tick()  # the retention push is an outbox side effect
        self.assertEqual(
            self.repo.rev_parse(f"refs/tauceti-queue/batches/{head['id']}/submissions/{rewritten}"),
            rewritten,
            "rejected chains stay retained",
        )
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["version"], head["version"])

    def test_repair_concurrency_allows_parallel_claims_and_first_adoption_wins(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.github.admins.add("second-admin")
        self.failing_head()
        one = ScriptedAgent(
            "one",
            "admin-worker",
            self.repo,
            self.root / "one",
            lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "one\n"}, explanation="One.")},
        )
        two = ScriptedAgent(
            "two",
            "second-admin",
            self.repo,
            self.root / "two",
            lambda t: {f"{PROJECT}#101": Resolution({"A.lean": "two\n"}, explanation="Two.")},
        )
        task_one = one.claim(self.controller)
        task_two = two.claim(self.controller)
        self.assertIsNotNone(task_two)
        self.assertEqual(task_one.task_id, task_two.task_id)
        tip_two = two.produce(task_two)
        tip_one = one.produce(task_one)
        self.assertTrue(self.controller.submit_task(task_two.task_id, task_two.claim_token, tip_two).accepted)
        late = self.controller.submit_task(task_one.task_id, task_one.claim_token, tip_one)
        self.assertEqual((late.accepted, late.reason), (False, "claim-superseded"))
        self.assertEqual(
            self.repo.file_at(self.controller.store.row("batches", {"state": "head"})["tip"], "A.lean"), "two\n"
        )


class Holds(ControllerCase):
    def test_hold_starts_after_the_threshold_and_backs_off(self):
        threshold = self.controller.setting("inference_failure_threshold")
        for _ in range(threshold - 1):
            self.assertIsNone(self.controller.report_inference("w", False, "err"))
        self.assertIsNone(self.controller.inference_hold())
        hold = self.controller.report_inference("w", False, "quota")
        self.assertEqual((hold["since"], hold["last_error"], hold["attempts"]), (self.clock(), "quota", 0))
        first_retry = hold["next_retry_at"]
        self.assertEqual(first_retry, self.clock() + self.controller.setting("inference_hold_backoff_seconds"))
        hold = self.controller.report_inference("w", False, "again")
        self.assertEqual(
            hold["next_retry_at"], first_retry, "a report before the retry time does not extend the backoff"
        )
        self.clock.advance(first_retry - self.clock() + 1)
        hold = self.controller.report_inference("w", False, "still")
        self.assertEqual(hold["attempts"], 1)
        self.assertEqual(
            hold["next_retry_at"], self.clock() + 2 * self.controller.setting("inference_hold_backoff_seconds")
        )
        self.assertEqual(hold["last_error"], "still")
        self.assertEqual(self.controller.status()["inference_hold"]["reports"], threshold + 2)
        self.controller.report_inference("w", True)
        self.assertIsNone(self.controller.inference_hold())
        self.assertEqual(self.controller.status()["counters"], {"inference_holds": 1, "inference_holds_resolved": 1})
        # Failures start counting again from zero after a success.
        self.controller.report_inference("w", False, "err")
        self.assertIsNone(self.controller.inference_hold())


class Comments(ControllerCase):
    def test_member_comment_text(self):
        batch = {"id": 7, "version": 3}
        clean = {
            "number": 101,
            "accepted_head": "a" * 40,
            "provenance": "clean",
            "commit_sha": "b" * 40,
            "explanation": "",
            "edited_shas": "[]",
        }
        text = self.controller.member_comment(batch, clean)
        self.assertEqual(text.splitlines()[0], "Pull request successfully merged into main.")
        self.assertIn("Build succeeded:", text)
        self.assertIn(f"Merged revision: `{'a' * 40}`.", text)
        self.assertNotIn("edited", text)
        edited = dict(
            clean,
            provenance="edited",
            explanation="Kept #102's version of the lemma.\n\nDropped the duplicate import.",
            edited_shas=json.dumps(["c" * 40, "b" * 40]),
        )
        text = self.controller.member_comment(batch, edited)
        self.assertIn(f"https://github.com/{PROJECT}/commit/{'b' * 40}", text)
        self.assertIn("> Kept #102's version of the lemma.\n>\n> Dropped the duplicate import.", text)
        self.assertIn(f"Related PRs: [#102](https://github.com/{PROJECT}/pull/102).", text)
        self.assertIn(f"Edited revisions: [`{'c' * 12}`](https://github.com/{PROJECT}/commit/{'c' * 40}).", text)
        self.assertIn("refs/tauceti-queue/batches/7/v3", text)
        self.assertEqual(self.controller._mentioned_prs(f"see {PROJECT}#5, other/repo#6 and #7 and #5", exclude=7), [5])
        self.assertEqual(MERGED_TITLE_PREFIX, "[Merged by Bors] - ")


class StatusShape(ControllerCase):
    def test_status_is_json_serialisable_and_stable_when_empty(self):
        status = self.controller.status()
        json.dumps(status)
        self.assertEqual(status["schema"], "tauceti-queue.status/v1")
        self.assertEqual(status["service"]["revision"], "fixture")
        self.assertEqual(status["service"]["started_at"], self.clock())
        self.assertEqual(
            set(status),
            {
                "schema",
                "repository",
                "canonical_branch",
                "canonical_sha",
                "service",
                "settings",
                "publication_enabled",
                "inference_hold",
                "waiting",
                "open_batch",
                "head",
                "external_head",
                "approvals",
                "tasks",
                "outbox",
                "reasons",
                "alerts",
                "counters",
                "head_cycle_seconds",
                "insertion_passes",
                "max_passes_waited",
            },
        )
        self.assertIsNone(status["head"])
        self.assertIsNone(status["external_head"])
        self.assertEqual(status["approvals"], {"webhook": 0, "polled": 0, "revoked_by_poll": 0, "last_poll": None})
        self.assertEqual(status["waiting"]["count"], 0)
        self.approve(101, {"A.lean": "a\n"})
        self.clock.advance(30)
        status = self.controller.status()
        self.assertEqual((status["waiting"]["count"], status["waiting"]["oldest_age_seconds"]), (1, 30))
        self.controller.tick()
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "ci")
        self.assertEqual(status["head"]["size"], 1)
        self.assertEqual(status["open_batch"]["size"], 0)
        self.assertEqual(status["head"]["ci"]["dispatch_id"], "dispatch-1")
        json.dumps(status)

    def test_recover_releases_controller_claims_and_returns_reservations(self):
        self.approve(101, {"A.lean": "a\n"})
        row = self.controller.inputs()[0]
        self.controller.store.update("inputs", {"id": row["id"]}, state="reserved")
        batch = self.controller._create_open_batch(self.repo.main_sha(), self.clock())
        self.controller._take_claim(batch, "insert", self.clock())
        self.controller.store.update("claims", {"batch_id": batch["id"]}, worker="controller")
        report = self.controller.recover()
        self.assertEqual((report["released_claims"], report["returned_reservations"]), (1, 1))
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")
        self.assertEqual(self.controller.store.row("claims", {"batch_id": batch["id"]})["release_reason"], "restart")


if __name__ == "__main__":
    unittest.main()

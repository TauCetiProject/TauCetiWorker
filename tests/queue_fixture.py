#!/usr/bin/env python3
"""The local end-to-end fixture of the AI merge queue: real Git history, a fake GitHub, scripted CI and agents.

Every test runs the controller over a temporary bare repository through `tauceti_queue.fakes`, with
a controllable clock and no inference. The tests are named after the acceptance scenarios of
`docs/redesign/specs/ai-merge-queue.md`; the mapping is in `docs/queue/controller.md`.
"""

import io
import json
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import (  # noqa: E402
    admission_planning,
    chain,
    escalation,
    feeds,
    monitoring,
    repair_stall,
    stuck_heads,
)
from tauceti_queue import runtime as rt  # noqa: E402
from tauceti_queue.approval_poll import ApprovalPoller  # noqa: E402
from tauceti_queue.controller import (  # noqa: E402
    CANDIDATE_BUILD_STEP,
    CANDIDATE_HANDOFF_STEP,
    CANDIDATE_PUBLICATION_JOB,
    LOG_GRACE_SECONDS,
    MERGED_TITLE_PREFIX,
    MISSING_SOURCE_WATCH,
    ControllerError,
    TaskError,
    publishing,
)
from tauceti_queue.fakes import Resolution, ScriptedAgent, Status, make_controller, reopen_controller  # noqa: E402
from tauceti_queue.loop import ReconciliationLoop  # noqa: E402
from tauceti_queue.ports import CandidatePhase  # noqa: E402

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
                # The batch's tip after its first insertion, then its base.
                "repair_outputs_revs": [
                    self.controller.store.row("batch_tips", {"batch_id": head["id"], "version": 1})["tip"],
                    base,
                ],
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
        self.assertEqual(self.controller.input_for(PROJECT, 102)["reason"], "empty-commit")
        self.assertEqual(self.github.closed[102], "Change 102")
        [comment] = self.github.comments_for(102)
        self.assertIn("The final staging commit is empty", comment["body"])
        self.assertNotIn("The repair agent's explanation", comment["body"], "do not invent a reason for a clean empty")

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

    def test_s14_shrink_to_green_rejects_the_empty_contribution_and_names_what_was_dropped(self):
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
        withdrawn = self.controller.inputs()[1]
        self.assertEqual((withdrawn["state"], withdrawn["reason"]), ("cancelled", "empty-commit"))
        self.assertIsNone(withdrawn["landed_at"])
        self.assertIsNone(withdrawn["landed_sha"])
        self.assertEqual(self.controller.status()["counters"]["prs_landed"], 1)
        self.assertEqual(self.controller.status()["counters"]["prs_edited_landed"], 0)
        self.assertEqual(self.controller.status()["counters"]["prs_withdrawn_empty"], 1)
        self.assertNotIn("prs_rejected", self.controller.status()["counters"])
        # Sent back to its author at adoption: the PR stays open and is not closed at landing.
        self.assertNotIn(102, self.github.closed)
        self.assertEqual(self.github.closed[101], MERGED_TITLE_PREFIX + "Change 101")
        [comment] = self.github.comments_for(102)
        self.assertIn("withdrew this PR from batch", comment["body"])
        self.assertIn("What to do next", comment["body"])
        self.assertNotIn("successfully merged", comment["body"])
        self.assertNotIn("bors-staging", self.github.labels[102])
        self.assertNotIn("repaired-in-queue", self.github.labels[102])
        outcomes = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        self.assertEqual({o.pr: o.state for o in outcomes}, {101: "ok", 102: "canceled"})
        landed = self.controller.store.journal_entries("landed")[0]["detail"]
        self.assertEqual((landed["members"], landed["withdrawn"]), ([101], [102]))
        self.assertNotIn("rejected", landed)
        exported = monitoring.project(
            self.controller.store.path, clock=self.clock, facts=monitoring.CommitFacts(self.repo.clone)
        )
        item = next(m for m in exported["members"] if m["pr"] == "102")
        self.assertEqual(item["disposition"], "withdrawn_empty")
        self.assertIsNone(item["landed_at"])

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


class RecordingNotifier:
    """A Zulip stand-in: records each delivered message; `lose` replies are lost after delivery."""

    def __init__(self, lose=0):
        self.messages = []
        self.calls = []
        self.lose = lose

    def send(self, marker, content, *, reconcile):
        self.calls.append((marker, reconcile))
        if reconcile and any(marker in message for message in self.messages):
            return "zulip reconciled"
        self.messages.append(content)
        if self.lose:
            self.lose -= 1
            raise RuntimeError("lost Zulip reply")
        return f"zulip {len(self.messages)}"


class StuckHeads(QueueFixture):
    """The CI recovery pass's stuck-head report: status, summary line and one direct message per period."""

    def setUp(self):
        super().setUp()
        self.notifier = self.controller.notifier = RecordingNotifier()
        self.report = self.root / "ci-recovery" / "stuck-heads.json"
        self.controller.stuck_heads_report = str(self.report)

    def write(self, *, since_hours, observed_ago=60, prs=(43797, 43792), repo="TauCetiProject/TauCeti"):
        now = self.clock()
        heads = [
            {"pr": pr, "head": "a" * 40, "reason": "no-ci", "last_push_at": now - (9 + i) * 3600, "run": None}
            for i, pr in enumerate(prs)
        ]
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(
            json.dumps(
                {
                    "schema": stuck_heads.SCHEMA,
                    "repo": repo,
                    "observed_at": now - observed_ago,
                    "complete": True,
                    "stuck_since": now - since_hours * 3600 if heads else None,
                    "heads": heads,
                }
            )
        )

    def test_a_short_stuck_period_is_reported_in_status_without_a_message(self):
        self.write(since_hours=2)
        self.tick()
        status = self.controller.status()
        self.assertEqual((status["stuck_heads"]["state"], status["stuck_heads"]["count"]), ("fresh", 2))
        self.assertEqual(status["stuck_heads"]["heads"][0]["pr"], 43792)  # oldest push first
        self.assertFalse(any("stuck without CI" in alert for alert in status["alerts"]))
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(rt.summarize(status).splitlines()[-2], "stuck heads: 2, oldest 10h")

    def test_six_hours_stuck_sends_one_message_per_stuck_period(self):
        self.write(since_hours=7)
        self.tick(3)
        self.assertEqual(len(self.notifier.messages), 1)
        message = self.notifier.messages[0]
        self.assertIn("2 PR heads stuck without CI or a final state since", message)
        started = stuck_heads.datetime.fromtimestamp(self.clock() - 7 * 3600, stuck_heads.TIMEZONE)
        self.assertIn(started.strftime("since %a %d %b %H:%M AEDT"), message)
        self.assertIn("https://github.com/TauCetiProject/TauCeti/pull/43797 no-ci", message)
        self.assertTrue(any("stuck without CI" in alert for alert in self.controller.status()["alerts"]))
        self.assertEqual(len(self.controller.store.journal_entries("stuck-heads-notified")), 1)
        self.clock.advance(3600)
        self.write(since_hours=8)  # a later pass of the same period: the same start
        self.tick()
        self.assertEqual(len(self.notifier.messages), 1)
        self.write(since_hours=0, prs=())
        self.tick()
        self.write(since_hours=6.5)  # a new period
        self.tick()
        self.assertEqual(len(self.notifier.messages), 2)

    def test_a_partial_pass_counts_as_a_lower_bound(self):
        self.write(since_hours=7)
        report = json.loads(self.report.read_text())
        self.report.write_text(json.dumps(dict(report, complete=False)))
        self.tick()
        self.assertIn("at least 2 (partial pass) PR heads stuck", self.notifier.messages[0])
        self.assertIn("(partial pass)", rt.summarize(self.controller.status()))

    def test_malformed_reports_are_invalid_not_fatal_or_silent(self):
        now = self.clock()
        good = {
            "schema": stuck_heads.SCHEMA,
            "repo": "TauCetiProject/TauCeti",
            "observed_at": now,
            "complete": True,
            "stuck_since": now - 7 * 3600,
            "heads": [{"pr": 1, "head": "a" * 40, "reason": "no-ci", "last_push_at": now - 9 * 3600}],
        }
        cases = [
            dict(good, observed_at=10**400),
            dict(good, observed_at=now + 3600),
            dict(good, heads=None),
            dict(good, stuck_since=None),
            dict(good, heads=[dict(good["heads"][0], last_push_at=float("inf"))]),
            dict(good, heads=["not a head"]),
        ]
        self.report.parent.mkdir(parents=True, exist_ok=True)
        for case in cases:
            with self.subTest(case=str(case)[:120]):
                self.report.write_text(json.dumps(case))
                self.tick()
                self.assertEqual(self.controller.status()["stuck_heads"]["state"], "invalid")
        self.report.write_text("[" * 100000 + "]" * 100000)
        self.tick()
        self.assertEqual(self.controller.status()["stuck_heads"]["state"], "invalid")
        self.assertEqual(self.notifier.messages, [])
        self.assertIn("stuck heads: report invalid", rt.summarize(self.controller.status()))

    def test_stale_missing_or_foreign_reports_never_message(self):
        for kwargs, state in (
            (dict(since_hours=9, observed_ago=4 * 3600), "stale"),
            (dict(since_hours=9, repo="someone/else"), "invalid"),
        ):
            with self.subTest(state=state):
                self.write(**kwargs)
                self.tick()
                self.assertEqual(self.controller.status()["stuck_heads"]["state"], state)
                self.assertEqual(self.notifier.messages, [])
        self.report.unlink()
        self.tick()
        status = self.controller.status()
        self.assertEqual(status["stuck_heads"]["state"], "missing")
        self.assertIn("stuck heads: report missing", rt.summarize(status))
        self.controller.stuck_heads_report = None
        self.assertIsNone(stuck_heads.summary(self.controller.status()["stuck_heads"]))


class EscalationNotifier(unittest.TestCase):
    def test_zulip_notifier_reconciles_a_retry_by_its_marker(self):
        notifier = escalation.ZulipNotifier(
            {"site": "https://zulip.invalid", "email": "bot@x", "api_key": "k", "recipient_id": 5}
        )
        calls = []

        def request(method, params):
            calls.append((method, params))
            if method == "GET":
                return {"messages": [{"id": 77, "content": "earlier text with marker-1"}]}
            return {"id": 78}

        with patch.object(notifier, "_request", side_effect=request):
            self.assertEqual(notifier.send("marker-1", "body marker-1", reconcile=True), "zulip 77 (reconciled)")
            self.assertEqual([method for method, _ in calls], ["GET"])
            self.assertEqual(notifier.send("marker-2", "body marker-2", reconcile=True), "zulip 78")
            self.assertEqual(notifier.send("marker-3", "body marker-3", reconcile=False), "zulip 78")
        self.assertEqual([method for method, _ in calls], ["GET", "GET", "POST", "POST"])
        self.assertEqual(json.loads(calls[-1][1]["to"]), [5])

    def test_notifier_configuration_comes_from_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zulip.json"
            path.write_text(json.dumps({"site": "https://z.invalid", "email": "b", "api_key": "k", "recipient_id": 3}))
            notifier = escalation.notifier_from_environment({escalation.ZULIP_CONFIG_ENV: str(path)})
            self.assertEqual(notifier.config["recipient_id"], 3)
            self.assertIsNone(escalation.notifier_from_environment({}))
            path.write_text("{}")
            with self.assertRaises(ValueError):
                escalation.notifier_from_environment({escalation.ZULIP_CONFIG_ENV: str(path)})


class ProgressAlertNotifier(unittest.TestCase):
    """The progress alert target reads TauCetiFleet's `fleet-alert --zulip-config` format."""

    def test_a_stream_topic_target_posts_there_and_reconciles_in_that_topic(self):
        calls = []

        config = {
            "site": "https://z.example",
            "email": "bot@z",
            "api_key": "k",
            "stream": "ops",
            "topic": "fleet health",
        }
        notifier = escalation.ZulipNotifier(config, streams=True)

        class Opener:
            def open(self, req, timeout):
                calls.append((req.get_method(), req.full_url, req.data))
                body = (
                    {"result": "success", "messages": []}
                    if req.get_method() == "GET"
                    else {"result": "success", "id": 7}
                )
                return io.BytesIO(json.dumps(body).encode())

        calls = []
        # Through the real transport: a stream target names no recipient and still passes its checks.
        with patch("tauceti_worker.audit_alerts.urllib.request.build_opener", lambda *_: Opener()):
            self.assertEqual(notifier.send("m1", "hello m1", reconcile=True), "zulip 7")
        self.assertIn("ops", urllib.parse.unquote(calls[0][1]))
        posted = urllib.parse.parse_qs(calls[1][2].decode())
        self.assertEqual(
            posted, {"type": ["stream"], "to": ["ops"], "topic": ["fleet health"], "content": ["hello m1"]}
        )
        from tauceti_worker import audit_alerts

        with (
            self.assertRaises(ValueError),
            patch("tauceti_worker.audit_alerts.urllib.request.build_opener", lambda *_: Opener()),
        ):
            audit_alerts.request(config, "POST", {})  # audit direct messages still need a recipient
        with self.assertRaises(ValueError):
            escalation.ZulipNotifier({k: v for k, v in config.items() if k != "stream"}, streams=True)

    def test_the_target_comes_from_its_own_variable_and_must_be_private(self):
        self.assertIsNone(escalation.progress_notifier_from_environment({}))
        self.assertIsNone(
            escalation.progress_notifier_from_environment({escalation.ZULIP_CONFIG_ENV: "/nonexistent"}),
            "the escalation direct messages' configuration is not a progress alert target",
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fleet-alert-zulip.json"
            path.write_text(json.dumps({"site": "https://z.example", "email": "e", "api_key": "k", "recipient_id": 5}))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                escalation.progress_notifier_from_environment({escalation.PROGRESS_ALERT_CONFIG_ENV: str(path)})
            path.chmod(0o600)
            notifier = escalation.progress_notifier_from_environment({escalation.PROGRESS_ALERT_CONFIG_ENV: str(path)})
            self.assertFalse(notifier.stream)


class LoopTermination(QueueFixture):
    """A failing head is repaired by agents however often it fails: the queue never withdraws, bisects
    or evicts a member on its own (docs/queue/escalation.md). Infrastructure failures back off
    automatically and are told to the operator."""

    def setUp(self):
        super().setUp()
        self.notifier = self.controller.notifier = RecordingNotifier()

    def fails_on(self, path, verdict="fail", attributed=None):
        """A CI policy failing any candidate whose tree holds `path`: a property of the content, as real CI's is."""

        def policy(head, _base, _members):
            if self.repo.file_at(head, path) is None:
                return "pass", []
            if verdict != "fail":
                return verdict, []
            finding = {"kind": "compiler", "module": f"TauCeti/{path}", "message": f"TauCeti/{path}:3:1: error: boom"}
            if attributed is not None:
                finding["source_pr"] = f"{PROJECT}#{attributed}"
            return "fail", [finding]

        return policy

    def give_up(self, times):
        """Workers claim the head's task and give it back without a submission."""
        for _ in range(times):
            task = self.controller.claim_task("flaky-worker", "admin-worker")
            self.assertIsNotNone(task)
            self.controller.release_task(task.task_id, task.claim_token, "agent_failed", "no result")

    def unhelpful_repair(self, number, path):
        """An agent that changes content without fixing the failure."""
        agent = self.agent(
            name=f"unhelpful-{number}-{len(self.github.dispatches)}",
            script=lambda task: {
                f"{PROJECT}#{number}": Resolution(
                    {path: f"tweak {len(self.github.dispatches)}\n"}, explanation="Tweaked."
                )
            },
        )
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)

    def test_a_recurring_failure_goes_to_repair_every_time_and_withdraws_nobody(self):
        self.github.ci_policy = self.fails_on("Bad.lean", attributed=102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Bad.lean": "bad\n"})
        self.tick()
        self.run_ci()
        self.tick()
        for _ in range(4):
            self.give_up(4)
            self.unhelpful_repair(101, "A.lean")
            self.tick()
            self.run_ci()
            self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        self.assertEqual(self.controller.store.count("tasks", {"kind": "repair"}), 5)
        for number in (101, 102):
            self.assertEqual(self.controller.input_for(PROJECT, number)["state"], "inserted")
        self.assertEqual(self.controller.status()["escalations"], [])
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.controller.store.count("failures", {}), 0)

    def test_a_repair_that_leaves_the_content_unchanged_is_dispatched_again(self):
        self.github.ci_policy = self.fails_on("Bad.lean")
        self.approve(101, {"Bad.lean": "bad\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        task = self.controller.claim_task("w", "admin-worker")
        adoption = self.controller.submit_task(task.task_id, task.claim_token, task.tip)
        self.assertTrue(adoption.accepted, adoption)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2)
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        self.assertEqual(self.controller.status()["escalations"], [])

    def test_a_held_escalation_recorded_before_does_not_hold_the_head(self):
        self.github.ci_policy = self.fails_on("Bad.lean")
        self.approve(101, {"Bad.lean": "bad\n"})
        self.tick()
        head = self.head()
        self.controller._set_state(
            "escalation_hold",
            {
                "batch_id": head["id"],
                "version": head["version"],
                "detail": "escalation failed",
                "at": self.clock(),
                "retries": 3,
                "retry_at": self.clock() + 86400,
                "failure": {"id": None, "members": "[]", "lines": "[]"},
                "cause": "an earlier escalation",
            },
        )
        self.run_ci()
        self.tick()
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "repair")
        self.assertFalse(any("holds after escalation" in alert for alert in status["alerts"]), status["alerts"])
        self.assertEqual(
            status["loop_termination"],
            {
                "infrastructure_backoff": None,
                "repair_stalls": [],
                "head_progress": status["loop_termination"]["head_progress"],
            },
        )

    def test_repeated_infrastructure_failures_back_off_automatically_and_recover(self):
        self.controller.set_setting("infrastructure_failure_limit", 2, "test")
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        for _ in range(3):
            self.run_ci()
            self.clock.advance(600)
            self.tick()
        # Two failures reach the limit: no operator is needed, the queue backs off on its own.
        self.assertEqual(len(self.github.dispatches), 2)
        status = self.controller.status()
        self.assertTrue(any("infrastructure backoff" in alert for alert in status["alerts"]), status["alerts"])
        self.assertTrue(any("re-dispatch at" in reason for reason in status["reasons"]), status["reasons"])
        self.assertEqual(status["loop_termination"]["infrastructure_backoff"]["retries"], 0)
        self.tick()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("infrastructure-backoff", self.notifier.messages[0])
        # 30 minutes, then 60, then 120, then 240, then 240: each wait is honoured, none needs `retry-ci`.
        delays = []
        for _ in range(5):
            dispatched = len(self.github.dispatches)
            waited = 0
            while len(self.github.dispatches) == dispatched:
                self.clock.advance(600)
                waited += 600
                self.tick()
                self.assertLess(waited, 5 * 3600)
            delays.append(waited)
            self.run_ci()
            self.tick()
        self.assertEqual([round(d / 1800) for d in delays], [1, 2, 4, 8, 8])
        self.assertEqual(len(self.notifier.messages), 1, "no message per retry")
        self.github.ci_policy = lambda *_: ("pass", [])
        while len(self.github.dispatches) == 7:
            self.clock.advance(600)
            self.tick()
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head())
        self.assertEqual(self.repo.file_at("main", "A.lean"), "a\n")
        self.assertIsNone(self.controller.status()["loop_termination"]["infrastructure_backoff"])
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertIn("infrastructure-recovered", self.notifier.messages[1])
        self.assertFalse(any("CI infrastructure backoff since" in a for a in self.controller.status()["alerts"]))
        self.assertFalse(any("infrastructure-recovered" in a for a in self.controller.status()["alerts"]))

    def test_an_infrastructure_backoff_spans_head_versions_with_one_message(self):
        self.controller.set_setting("infrastructure_failure_limit", 1, "test")
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.run_ci()
        self.tick()
        self.assertIsNotNone(self.controller.status()["loop_termination"]["infrastructure_backoff"])
        # The base moves: a new head version dispatches at once, and its failure keeps backing off.
        self.repo.advance_main({"Other.lean": "o\n"})
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2)
        self.run_ci()
        self.clock.advance(1200)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 2, "still backing off on the new version")
        self.tick()
        self.assertEqual(len(self.notifier.messages), 1)

    def test_notification_delivery_retries_without_a_second_message(self):
        self.notifier.lose = 1
        self.controller.set_setting("infrastructure_failure_limit", 1, "test")
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(len(self.notifier.messages), 1)
        self.clock.advance(self.controller.setting("outbox_retry_max_seconds"))
        self.tick()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual([reconcile for _, reconcile in self.notifier.calls], [False, True])
        self.assertEqual(self.controller.store.count("outbox", {"kind": "notify", "state": "done"}), 1)

    def test_without_a_notifier_the_alert_remains_and_the_outbox_settles(self):
        self.controller.notifier = None
        self.controller.set_setting("infrastructure_failure_limit", 1, "test")
        self.github.ci_policy = lambda *_: ("infrastructure", [])
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.run_ci()
        self.tick(2)
        entry = self.controller.store.row("outbox", {"kind": "notify"})
        self.assertEqual((entry["state"], entry["result"]), ("done", "notifier-unconfigured"))
        alerts = self.controller.status()["alerts"]
        self.assertTrue(any("escalation (infrastructure-backoff)" in a for a in alerts), alerts)


# The agent's last message on batch 465's repair task 1415, one of the 109 that said a human was needed.
TASK_1415_CONCLUSION = """I made no changes to the batch: HEAD is still `cc0e44e29450`, the same chain the queue already rejected. No content change in the PRs will fix this failure, so the queue will reject this chain again. The batch has to be split, or `lint-env.sh` fixed, which a human has to do because `scripts/` is human-owned.

**Why it fails.** It is the same LINT-ENV failure as every earlier attempt on this task:
- `tasks/1415/logs/lint-env.log` shows `DocScanDriver.lean:3436/3437:0: error: maximum recursion depth reached in the code generator`.

**Focused checks:**
- `python3 .tauceti-repair/check`: `ok` (linear on `b608709b45f2`, trailers and subjects intact, clean tree)."""

DRIVER_FAILURE = {
    "kind": "lint",
    "message": "LINT-ENV: FAIL — driver failure: the docstring-scan driver did not elaborate cleanly — see output above",
    "module": "",
}


class StallSignature(unittest.TestCase):
    """The failure signature and the agent's conclusion (`tauceti_queue/repair_stall.py`)."""

    def test_identical_failures_share_a_signature_whatever_varies_between_runs(self):
        first = {
            "kind": "lint",
            "module": "TauCeti.A",
            "message": "A.lean:3436:0: error at deadbeef1 after 31.2s in /tmp/x1/y",
        }
        again = {
            "kind": "lint",
            "module": "TauCeti.A",
            "message": "A.lean:12:0:  error at cafe0042 after 30.9s in /tmp/z9/y",
        }
        other = {"kind": "audit", "module": "TauCeti.A", "message": first["message"]}
        elsewhere = dict(first, module="TauCeti.B")
        tip = "c" * 40
        self.assertEqual(repair_stall.signature(tip, [first]), repair_stall.signature(tip, [again]))
        self.assertEqual(
            repair_stall.signature(tip, [first, DRIVER_FAILURE]), repair_stall.signature(tip, [DRIVER_FAILURE, again])
        )
        self.assertNotEqual(repair_stall.signature(tip, [first]), repair_stall.signature(tip, [other]), "another check")
        self.assertNotEqual(repair_stall.signature(tip, [first]), repair_stall.signature(tip, [elsewhere]))
        self.assertNotEqual(
            repair_stall.signature(tip, [first]), repair_stall.signature("d" * 40, [first]), "a new tip"
        )
        self.assertNotEqual(
            repair_stall.signature(tip, [first]),
            repair_stall.signature(tip, [dict(first, message="unused variable `x`")]),
            "another message",
        )

    def test_the_conclusion_keeps_why_the_agent_cannot_progress(self):
        text = repair_stall.conclusion(TASK_1415_CONCLUSION)
        self.assertIn("a human has to do", text)
        self.assertIn("No content change in the PRs will fix this failure", text)
        self.assertNotIn("Focused checks", text)
        self.assertLessEqual(len(text), repair_stall.CONCLUSION_LIMIT)
        self.assertEqual(repair_stall.conclusion("Fixed the import.\n\nAll checks pass."), "Fixed the import.")
        self.assertEqual(repair_stall.conclusion(None), "")

    def test_the_backoff_doubles_up_to_its_maximum(self):
        self.assertEqual(
            [repair_stall.retry_delay(n, 1800, 14400) for n in range(6)], [1800, 3600, 7200, 14400, 14400, 14400]
        )


class StalledRepair(QueueFixture):
    """A head repair that keeps checkpointing the same tip with the same findings is stalled: one
    journal-only progress alert quoting the agent, a backoff before the next claim, and a resume when
    its inputs change (docs/queue/escalation.md, "Stalled repair")."""

    REVISION = "a" * 40

    def setUp(self):
        super().setUp()
        self.notifier = self.controller.notifier = RecordingNotifier()
        self.github.ci_policy = lambda head, _base, _members: (
            ("fail", [DRIVER_FAILURE]) if self.repo.file_at(head, "Bad.lean") is not None else ("pass", [])
        )
        self.approve(101, {"Bad.lean": "bad\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")

    def checkpoint(self, revision=REVISION, findings=(DRIVER_FAILURE,), conclusion=TASK_1415_CONCLUSION):
        task = self.controller.claim_task("repair-3", "admin-worker", worker_revision=revision)
        self.assertIsNotNone(task, "the task is offered")
        notes = {"repair_checkpoint": list(findings), "worker_revision": revision, "agent_conclusion": conclusion}
        result = self.controller.submit_task(
            task.task_id, task.claim_token, task.tip, json.dumps(notes), checkpoint=True
        )
        self.assertEqual(result.reason, "repair-checkpoint")
        self.clock.advance(780)  # each of 1415's claims took about 13 minutes
        return task

    def escalated(self, action):
        return [e for e in self.controller.store.journal_entries("escalated") if e["detail"]["action"] == action]

    def test_three_identical_checkpoints_alert_once_journal_only_and_back_off(self):
        task = self.checkpoint()
        self.checkpoint()
        self.assertEqual(self.escalated("repair-stalled"), [])
        self.checkpoint()
        self.assertEqual(len(self.escalated("repair-stalled")), 1)
        self.tick()
        # Journal-only until decision 135 configures a target: nothing goes to the operator's direct messages.
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.controller.store.count("outbox", {"kind": "notify"}), 0)
        entry = self.controller.store.row("outbox", {"kind": "progress-alert"})
        self.assertEqual((entry["state"], entry["result"]), ("done", "journal-only"))
        content = json.loads(entry["payload"])["content"]
        self.assertIn("repair-stalled", content)
        self.assertIn("a human has to do", content)
        self.assertIn("driver failure", content)
        status = self.controller.status()
        alerts = [a for a in status["alerts"] if a.startswith("repair stalled")]
        self.assertEqual(len(alerts), 1, status["alerts"])
        self.assertIn(f"task {task.task_id}", alerts[0])
        self.assertIn("AEDT", alerts[0])
        self.assertIn("agent: ", alerts[0])
        self.assertFalse(any("unclaimed for" in a for a in status["alerts"]), "backed off on purpose")
        self.assertEqual(status["head"]["repair_stall"]["checkpoints"], 3)
        # Backed off: the same code is not offered the task for 30 minutes, then 60, ...
        self.assertIsNone(self.controller.claim_task("repair-3", "admin-worker", worker_revision=self.REVISION))
        self.clock.advance(1800 - 780 - 60)
        self.tick()
        self.assertIsNone(self.controller.claim_task("repair", "admin-worker", worker_revision=self.REVISION))
        self.clock.advance(60)
        self.checkpoint()
        stall = self.controller.status()["loop_termination"]["repair_stalls"][0]
        self.assertEqual((stall["checkpoints"], stall["retries"]), (4, 1))
        self.assertEqual(stall["retry_at"] - (self.clock() - 780), 3600)
        self.assertEqual(len(self.escalated("repair-stalled")), 1, "one alert per stall")
        self.assertEqual(len(self.controller.store.journal_entries("repair-stall-backoff")), 1)

    def test_a_worker_on_new_code_is_offered_the_stalled_task_at_once(self):
        for _ in range(3):
            self.checkpoint()
        self.assertIsNone(self.controller.claim_task("repair-3", "admin-worker", worker_revision=self.REVISION))
        self.assertIsNone(self.controller.claim_task("old", "admin-worker"), "an unknown revision waits too")
        # The fix is installed on one unit: its revision has not tried the task.
        self.checkpoint(revision="b" * 40)
        claimed = self.controller.store.journal_entries("repair-stall-claimed")
        self.assertEqual(claimed[-1]["detail"]["reason"], "new-worker-revision")
        # Same failure on the new code: still stalled, and both revisions now wait for the backoff.
        stall = self.controller.status()["loop_termination"]["repair_stalls"][0]
        self.assertEqual(stall["revisions"], [self.REVISION, "b" * 40])
        self.assertIsNone(self.controller.claim_task("w", "admin-worker", worker_revision="b" * 40))

    def test_other_findings_end_the_stall_with_one_recovery_message(self):
        for _ in range(3):
            self.checkpoint()
        self.checkpoint(revision="b" * 40, findings=({"kind": "lint", "module": "TauCeti.A", "message": "simpNF"},))
        self.tick()
        self.assertEqual(len(self.escalated("repair-stall-ended")), 1)
        self.assertEqual(self.controller.status()["loop_termination"]["repair_stalls"], [])
        self.assertFalse(any(a.startswith("repair stalled") for a in self.controller.status()["alerts"]))
        self.assertIsNotNone(self.controller.claim_task("w", "admin-worker", worker_revision=self.REVISION))

    def test_a_new_head_version_ends_the_stall(self):
        for _ in range(3):
            self.checkpoint()
        self.clock.advance(1800)
        self.tick()
        task = self.controller.claim_task("w", "admin-worker", worker_revision=self.REVISION)
        self.github.ci_policy = lambda *_: ("pass", [])
        resolution = Resolution({"Bad.lean": "good\n"}, explanation="Fixed.")
        agent = self.agent(script=lambda _task: {f"{PROJECT}#101": resolution})
        tip = agent.produce(task)
        self.assertTrue(self.controller.submit_task(task.task_id, task.claim_token, tip).accepted)
        self.tick()
        self.assertEqual(len(self.escalated("repair-stall-ended")), 1)
        self.assertEqual(self.controller.status()["loop_termination"]["repair_stalls"], [])

    def test_disabled_stall_detection_keeps_offering_the_task(self):
        self.controller.set_setting("repair_stall_checkpoints", 0, "test")
        for _ in range(5):
            self.checkpoint()
        self.assertEqual(self.escalated("repair-stalled"), [])

    def test_turning_detection_off_releases_a_stall_at_once(self):
        for _ in range(3):
            self.checkpoint()
        self.assertIsNone(self.controller.claim_task("w", "admin-worker", worker_revision=self.REVISION))
        self.controller.set_setting("repair_stall_checkpoints", 0, "test")
        self.assertIsNotNone(self.controller.claim_task("w", "admin-worker", worker_revision=self.REVISION))
        self.tick()
        self.assertEqual(self.controller.status()["loop_termination"]["repair_stalls"], [])
        self.assertEqual(len(self.escalated("repair-stall-ended")), 1)

    def test_every_revision_in_the_identical_run_has_tried_the_task(self):
        self.checkpoint(revision="b" * 40)
        self.checkpoint()
        self.checkpoint()
        stall = self.controller.status()["loop_termination"]["repair_stalls"][0]
        self.assertEqual(stall["revisions"], ["b" * 40, self.REVISION])
        self.assertIsNone(self.controller.claim_task("w", "admin-worker", worker_revision="b" * 40))
        self.assertIsNotNone(self.controller.claim_task("w", "admin-worker", worker_revision="c" * 64))

    def test_a_stall_that_recurs_is_told_again(self):
        other = ({"kind": "lint", "module": "TauCeti.A", "message": "simpNF"},)
        for findings in ((DRIVER_FAILURE,), other, (DRIVER_FAILURE,)):
            for _ in range(3):
                self.clock.advance(4 * 3600)
                self.checkpoint(findings=findings)
        self.tick()
        self.assertEqual(len(self.escalated("repair-stalled")), 3)
        self.assertEqual(self.controller.store.count("outbox", {"kind": "progress-alert"}), 5, "3 starts, 2 ends")

    def test_a_configured_target_receives_the_progress_alert(self):
        target = self.controller.progress_notifier = RecordingNotifier()
        for _ in range(3):
            self.checkpoint()
        self.tick()
        self.assertEqual(len(target.messages), 1)
        self.assertIn("The agent's own conclusion", target.messages[0])
        self.assertEqual(self.notifier.messages, [], "never the escalation direct messages")

    def test_turning_the_head_alert_off_clears_an_active_one(self):
        self.tick()
        self.clock.advance(3 * 3600)
        self.tick()
        self.assertEqual(len(self.escalated("head-no-progress")), 1)
        self.controller.set_setting("head_progress_alert_seconds", 0, "test")
        self.tick()
        self.assertFalse(any(repair_stall.HEAD_ALERT_TEXT in a for a in self.controller.status()["alerts"]))
        self.assertIsNone(self.controller.status()["loop_termination"]["head_progress"])

    def test_revisions_from_a_long_identical_run_are_all_remembered(self):
        for n in range(25):
            self.clock.advance(4 * 3600)
            self.checkpoint(revision=f"{n:040x}")
        stall = self.controller.status()["loop_termination"]["repair_stalls"][0]
        self.assertEqual(len(stall["revisions"]), 25)
        self.assertIsNone(self.controller.claim_task("w", "admin-worker", worker_revision=f"{0:040x}"))

    def test_a_restart_starts_the_head_progress_period_again(self):
        self.tick()
        self.clock.advance(4 * 3600)  # the controller was down
        self.controller._head_progress_anchored = False
        self.tick()
        self.assertEqual(self.escalated("head-no-progress"), [])
        self.clock.advance(3 * 3600)
        self.tick()
        self.assertEqual(len(self.escalated("head-no-progress")), 1)

    def test_a_head_without_progress_alerts_once_and_recovers(self):
        self.controller.set_setting("repair_stall_checkpoints", 0, "test")
        self.tick()
        for _ in range(3 * 3600 // 780 + 1):
            self.checkpoint()
            self.tick()
        self.assertEqual(len(self.escalated("head-no-progress")), 1)
        self.assertTrue(any(repair_stall.HEAD_ALERT_TEXT in a for a in self.controller.status()["alerts"]))
        self.tick(3)
        self.assertEqual(len(self.escalated("head-no-progress")), 1, "one alert per period")
        task = self.controller.claim_task("w", "admin-worker")
        self.github.ci_policy = lambda *_: ("pass", [])
        agent = self.agent(
            script=lambda _task: {f"{PROJECT}#101": Resolution({"Bad.lean": "ok\n"}, explanation="Fixed.")}
        )
        self.assertTrue(self.controller.submit_task(task.task_id, task.claim_token, agent.produce(task)).accepted)
        self.tick()
        self.assertEqual(len(self.escalated("head-progress-resumed")), 1)
        self.assertFalse(any(repair_stall.HEAD_ALERT_TEXT in a for a in self.controller.status()["alerts"]))


class WithdrawnAtAdoption(QueueFixture):
    """An adopted repair that empties an ordinary member sends it back to its author at once."""

    def emptied(self, numbers=(102,), explanation="Dropped {file}: it cannot compile against #101."):
        self.github.ci_policy = fail_members(*numbers)
        self.approve(101, {"A.lean": "a\n"})
        for number in numbers:
            self.approve(number, {f"F{number}.lean": "broken\n"})
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#{number}": Resolution(empty=True, explanation=explanation.format(file=f"F{number}.lean"))
                for number in numbers
            }
        )
        self.github.ci_policy = verdicts()
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        return adoption

    def test_the_author_gets_one_explanatory_comment_at_adoption_and_the_pr_stays_open(self):
        admission = self.controller.admission = unittest.mock.Mock()
        adoption = self.emptied()
        self.assertEqual(adoption.provenance[f"{PROJECT}#102"], "withdrawn")
        row = self.controller.input_for(PROJECT, 102)
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "empty-commit"))
        admission.withdraw.assert_called_once_with(102, row["head_sha"])
        self.controller.admission = None  # the stand-in only records the approval withdrawal
        member = next(m for m in self.members(self.head()) if m["number"] == 102)
        self.assertEqual(member["withdrawn"], 1)
        self.tick()  # drains the outbox
        [comment] = self.github.comments_for(102)
        body = comment["body"]
        self.assertIn("withdrew this PR from batch", body)
        self.assertIn("What failed (repair task", body)
        self.assertIn("TauCeti/Pr102.lean", body)
        self.assertIn("error in PR 102", body)
        self.assertIn("https://github.invalid/actions/runs/", body)
        self.assertIn(f"Accepted revision: `{row['head_sha']}`", body)
        self.assertIn("> Dropped F102.lean: it cannot compile against #101.", body)
        self.assertIn("What to do next: rebase onto `main`", body)
        self.assertNotIn("bors-staging", self.github.labels[102])
        self.assertEqual(self.controller.store.journal_entries("member-withdrawn-empty")[0]["detail"]["number"], 102)
        # The batch lands without it: no second comment, no close, not counted as landed or rejected.
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head())
        self.assertEqual(len(self.github.comments_for(102)), 1)
        self.assertNotIn(102, self.github.closed)
        self.assertEqual(self.github.prs[102]["state"], "open")
        counters = self.controller.status()["counters"]
        self.assertEqual((counters["prs_landed"], counters.get("prs_rejected", 0)), (1, 0))
        self.assertEqual(self.controller.input_for(PROJECT, 102)["reason"], "empty-commit")

    def test_a_later_repair_cannot_restore_a_withdrawn_member(self):
        self.emptied()
        self.github.ci_policy = verdicts("fail")
        self.tick()
        self.run_ci()
        self.tick()
        restorer = self.agent(
            name="restorer",
            script=lambda task: {
                f"{PROJECT}#102": Resolution({"F102.lean": "fixed\n"}, explanation="Restored a working contribution.")
            },
        )
        rejection = restorer.run(self.controller)
        self.assertFalse(rejection.accepted)
        self.assertEqual(rejection.reason, "no-repair-edited")

    def test_comment_delivery_survives_a_lost_response_and_restart_without_duplicates(self):
        post = self.github.post_comment

        def post_then_fail(*args):
            post(*args)
            raise RuntimeError("lost comment response")

        with patch.object(self.github, "post_comment", side_effect=post_then_fail):
            self.emptied()
            self.tick()
        self.assertEqual(len(self.github.comments_for(102)), 1)
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        self.clock.advance(restarted.setting("outbox_retry_max_seconds"))
        restarted.tick()
        self.assertEqual(restarted.store.count("outbox", {"key": "withdrawn-comment:2", "state": "done"}), 1)
        self.assertEqual(len(self.github.comments_for(102)), 1)

    def test_a_withdrawn_member_merged_outside_the_queue_lands_with_its_batch(self):
        self.emptied()
        self.repo.advance_main({"F102.lean": "fixed elsewhere\n"}, message="Change 102 (#102)")
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head())
        self.assertEqual(self.controller.input_for(PROJECT, 102)["state"], "landed")
        self.assertEqual(self.controller.status()["counters"]["prs_landed"], 2)

    def test_disabled_setting_defers_the_decision_to_landing(self):
        self.controller.set_setting("withdraw_emptied_at_adoption", False, "test")
        self.emptied()
        self.assertEqual(self.controller.input_for(PROJECT, 102)["state"], "inserted")
        self.tick()
        self.assertEqual(self.github.comments_for(102), [])
        self.run_ci()
        self.tick(2)
        [comment] = self.github.comments_for(102)
        self.assertIn("rejected during staging repair", comment["body"])
        self.assertIn("What failed (repair task", comment["body"])
        self.assertIn("What to do next", comment["body"])
        self.assertEqual(self.github.closed[102], "Change 102")


class EmptyContributions(QueueFixture):
    """The landing-time backstop: an empty final commit not withdrawn at adoption is rejected when its batch lands."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("withdraw_emptied_at_adoption", False, "test")

    def assert_unrelated_history_still_rejects(self, message, *, empty=False):
        self.repaired_empty()
        self.repo.advance_main({} if empty else {"Other.lean": "other\n"}, message=message)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "empty-commit")
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 1)
        self.assertNotIn("successfully merged", self.github.comments_for(101)[0]["body"])

    def test_a_pr_reference_in_a_commit_body_is_not_landing_evidence(self):
        self.assert_unrelated_history_still_rejects("Other change\n\nFollow-up to cleanup (#101)")

    def test_a_foreign_trailer_takes_precedence_over_the_subject(self):
        self.assert_unrelated_history_still_rejects("Other change (#101)\n\nSource-PR: other/repo#101")

    def test_a_native_trailer_does_not_land_a_github_pr_with_the_same_number(self):
        self.assert_unrelated_history_still_rejects(
            "Other change (#101)\n\nSource-PR: forgejo:example.test/library#101"
        )

    def test_an_earlier_empty_placeholder_is_not_landing_evidence(self):
        self.assert_unrelated_history_still_rejects("Change 101 (#101)", empty=True)

    def test_external_merge_is_recognized_without_a_staging_landing_note(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution(empty=True, explanation="Dropped A during repair.")}
        )
        self.assertTrue(agent.run(self.controller).accepted)
        # An independent merge is already in the batch base when its repaired empty commit lands.
        landing = self.repo.advance_main({"A.lean": "a\n"}, message="Change 101 (#101)")
        self.github.ci_policy = verdicts()
        with patch.object(chain, "landed_message", side_effect=lambda message, sha: message):
            self.tick()
        head = self.head()
        self.assertEqual(head["base"], landing)
        commit = next(c for c in self.chain_commits(head) if c.source_pr == f"{PROJECT}#101")
        self.assertFalse(chain.carries_landed_note(commit.message))
        self.assertTrue(self.controller._commit_empty(commit.sha))
        self.run_ci()
        self.tick(2)
        row = self.controller.input_for(PROJECT, 101)
        self.assertEqual(row["state"], "landed")
        self.assertEqual(self.controller.status()["counters"]["prs_landed"], 2)
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 0)
        self.assertIn("successfully merged", self.github.comments_for(101)[0]["body"])
        self.assertNotIn("repaired-in-queue", self.github.labels[101])
        self.assertFalse(self.controller.store.rows("outbox", {"key": f"rejected-close:{head['id']}:101"}))

    def test_external_merge_lookup_failure_defers_landing_instead_of_rejecting(self):
        head = self.repaired_empty()
        self.github.update_ref(PROJECT, "refs/heads/main", head["tip"])
        with patch.object(chain, "landed_prs", side_effect=chain.ChainError("history temporarily unavailable")):
            with self.assertRaises(chain.ChainError):
                self.tick()
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "inserted")
        self.assertFalse(self.controller.store.rows("outbox", {"key": f"rejected-close:{head['id']}:101"}))
        self.tick(2)
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "empty-commit")

    def repaired_empty(self, count=1):
        self.github.ci_policy = verdicts("fail")
        for number in range(101, 101 + count):
            self.approve(number, {f"F{number}.lean": "broken\n"})
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {
                m.source_pr: Resolution(empty=True, explanation=f"Dropped F{m.number}.lean: could not repair it.")
                for m in task.members
            }
        )
        self.assertTrue(agent.run(self.controller).accepted)
        self.assertEqual({i["state"] for i in self.controller.inputs()}, {"inserted"})
        self.assertEqual(self.github.comments, [], "no rejection until the final batch lands")
        return self.head()

    def test_an_all_empty_batch_finishes_without_claiming_any_pr_merged(self):
        head = self.repaired_empty(count=2)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head())
        self.assertEqual(self.repo.main_sha(), head["tip"])
        self.assertEqual({i["state"] for i in self.controller.inputs()}, {"cancelled"})
        self.assertEqual(self.controller.status()["counters"]["prs_landed"], 0)
        self.assertEqual(self.controller.status()["counters"]["prs_rejected"], 2)
        self.assertEqual(self.github.closed, {101: "Change 101", 102: "Change 102"})
        for number in (101, 102):
            [comment] = self.github.comments_for(number)
            self.assertIn(f"Dropped F{number}.lean", comment["body"])
            self.assertNotIn("successfully merged", comment["body"])
            self.assertNotIn("bors-staging", self.github.labels[number])
        outcomes = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        self.assertEqual({o.state for o in outcomes}, {"canceled"})

    def test_a_later_repair_restoring_content_is_merged(self):
        self.repaired_empty()
        self.github.ci_policy = verdicts("fail")
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            name="restorer",
            script=lambda task: {
                f"{PROJECT}#101": Resolution({"F101.lean": "fixed\n"}, explanation="Restored a working contribution.")
            },
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.inputs()[0]["state"], "landed")
        self.assertEqual(self.repo.file_at("main", "F101.lean"), "fixed\n")
        self.assertEqual(self.github.closed[101], MERGED_TITLE_PREFIX + "Change 101")
        [comment] = self.github.comments_for(101)
        self.assertIn("successfully merged", comment["body"])
        self.assertIn("Restored a working contribution.", comment["body"])
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 0)

    def test_rejection_effects_survive_lost_responses_and_restart_without_duplicates(self):
        self.repaired_empty()
        self.tick()
        self.run_ci()
        post = self.github.post_comment
        close = self.github.close_pr

        def post_then_fail(*args):
            post(*args)
            raise RuntimeError("lost comment response")

        def close_then_fail(*args):
            close(*args)
            raise RuntimeError("lost close response")

        self.github.fail_next["set_labels"] = 1
        with (
            patch.object(self.github, "post_comment", side_effect=post_then_fail),
            patch.object(self.github, "close_pr", side_effect=close_then_fail),
        ):
            self.tick(2)
        self.assertEqual(self.controller.store.count("outbox", {"state": "pending"}), 3)
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        self.clock.advance(restarted.setting("outbox_retry_max_seconds"))
        restarted.tick()
        restarted.tick()
        self.assertEqual(restarted.store.count("outbox", {"state": "pending"}), 0)
        self.assertEqual(len(self.github.comments_for(101)), 1)
        self.assertEqual(self.github.closed[101], "Change 101")
        self.assertNotIn("bors-staging", self.github.labels[101])
        self.assertEqual(restarted.status()["counters"]["prs_rejected"], 1)
        self.assertEqual(restarted.status()["counters"]["prs_landed"], 0)
        self.assertEqual(len(restarted.store.journal_entries("landed")), 1)

    def test_delayed_rejection_preserves_a_new_members_membership_label(self):
        head = self.repaired_empty()
        self.tick()
        self.run_ci()
        self.github.fail_next.update(close_pr=1, set_labels=1)
        self.tick(2)
        self.assertNotIn(101, self.github.closed)
        new_head = self.github.push_pr(101, {"F101.lean": "fixed\n"})
        self.assertTrue(self.controller.approve(PROJECT, 101, new_head, "reviewer")["accepted"])
        self.tick()
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        self.tick()
        self.assertEqual(self.github.prs[101]["state"], "open")
        self.assertIn("bors-staging", self.github.labels[101])
        for kind, result in (("close", "stale-head"), ("labels", "new-member")):
            entry = self.controller.store.row("outbox", {"key": f"rejected-{kind}:{head['id']}:101"})
            self.assertEqual(entry["result"], result)
        outcomes = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        old = next(o for o in outcomes if o.batch_id == head["id"])
        self.assertEqual(old.state, "canceled")

    def test_delayed_rejection_removes_staging_label_after_an_unapproved_push(self):
        head = self.repaired_empty()
        self.tick()
        self.run_ci()
        self.github.fail_next.update(close_pr=1, set_labels=1)
        self.tick(2)
        self.github.push_pr(101, {"F101.lean": "fixed\n"})
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        self.tick()
        self.assertEqual(self.github.prs[101]["state"], "open")
        self.assertNotIn("bors-staging", self.github.labels[101])
        self.assertEqual(
            self.controller.store.row("outbox", {"key": f"rejected-labels:{head['id']}:101"})["result"], "labels"
        )

    def test_delayed_close_does_not_override_fresh_approval_of_the_same_revision(self):
        head = self.repaired_empty()
        self.tick()
        self.run_ci()
        self.github.fail_next["close_pr"] = 1
        self.tick(2)
        accepted_head = self.controller.input_for(PROJECT, 101)["head_sha"]
        self.assertTrue(self.controller.approve(PROJECT, 101, accepted_head, "reviewer")["accepted"])
        self.tick()
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        self.tick()
        self.assertEqual(self.github.prs[101]["state"], "open")
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "inserted")
        self.assertEqual(
            self.controller.store.row("outbox", {"key": f"rejected-close:{head['id']}:101"})["result"], "new-approval"
        )

    def test_external_landing_retries_after_a_transient_git_failure(self):
        head = self.repaired_empty()
        self.approve(102, {"Outside.lean": "outside\n"})
        self.github.update_ref(PROJECT, "refs/heads/main", head["tip"])
        self.repo.advance_main({"Outside.lean": "outside\n"}, message="Change 102 (#102)")
        with patch.object(chain, "read_commit", side_effect=chain.ChainError("transient git failure")):
            with self.assertRaises(chain.ChainError):
                self.tick()
        self.assertIsNotNone(self.head())
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        restarted.tick()
        restarted.tick()
        self.assertIsNone(restarted.store.row("batches", {"state": "head"}))
        self.assertEqual(restarted.input_for(PROJECT, 101)["reason"], "empty-commit")
        self.assertEqual(restarted.input_for(PROJECT, 102)["reason"], "landed-externally")
        self.assertEqual(self.github.closed[101], "Change 101")

    def test_failed_reapproval_preserves_the_rejected_revision(self):
        self.repaired_empty()
        self.tick()
        self.run_ci()
        self.tick(2)
        row = self.controller.input_for(PROJECT, 101)
        result = self.controller.approve(PROJECT, 101, row["head_sha"], "reviewer")
        self.assertEqual((result["accepted"], result["reason"]), (False, "closed"))
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "empty-commit")

    def test_external_canonical_advance_does_not_count_empty_placeholders_as_landed(self):
        head = self.repaired_empty()
        self.github.update_ref(PROJECT, "refs/heads/main", head["tip"])
        with patch.object(self.controller, "_interacting_landed") as observe:
            self.tick(2)
        self.assertTrue(observe.call_args_list)
        self.assertTrue(all(101 not in call.args[0] for call in observe.call_args_list))
        self.assertEqual(self.controller.inputs()[0]["reason"], "empty-commit")
        self.assertEqual(self.github.closed[101], "Change 101")


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
        self.controller.set_setting("repair_concurrency", 2, "t")
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
        self.assertIsNone(second.claim(self.controller), "a held head task has one owner despite spare capacity")
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
        self.controller.set_setting("repair_concurrency", 2, "t")
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
        # An emptied member is not labelled as repaired, even when an agent's edit marks it `edited`.
        self.assertTrue(self.controller._landed_outside_queue({"commit_sha": landed_member.sha}))
        self.assertFalse(self.controller._landed_outside_queue({"commit_sha": other.sha}))
        self.assertFalse(
            self.controller.store.rows("outbox", {"kind": "labels", "key": f"repaired-label:{head['id']}:101"})
        )
        self.assertEqual(self.repo.file_at("main", "B.lean"), "b\n")
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "landed")
        self.assertEqual(self.github.closed[101], MERGED_TITLE_PREFIX + "Change 101")
        self.assertIn("successfully merged", self.github.comments_for(101)[0]["body"])
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 0)
        self.assertEqual(
            sorted(c.source_pr for c in self.first_parent_chain(admin, self.repo.main_sha())),
            [f"{PROJECT}#101", f"{PROJECT}#102"],
        )

        old_input = self.controller.input_for(PROJECT, 101)
        self.github.prs[101]["state"] = "open"
        new_head = self.github.push_pr(101, {"Followup.lean": "followup\n"})
        self.assertTrue(self.controller.approve(PROJECT, 101, new_head, "reviewer")["accepted"])
        self.assertEqual(self.controller.store.row("inputs", {"id": old_input["id"]})["state"], "landed")
        outcomes = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        self.assertEqual(next(o for o in outcomes if o.pr == 101 and o.batch_id == head["id"]).state, "ok")
        self.controller.unapprove(PROJECT, 101, "reviewer")
        self.github.publish_check(101)
        poller = ApprovalPoller(self.controller, self.github, PROJECT, clock=self.clock)
        self.assertEqual(poller.run_once()["approved"], 0, "an old landed row must not hide a newer withdrawal")
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "unapproved")
        self.assertEqual(self.controller._interacting_state(101), "gone")

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
        self.assertEqual(attempt["outcome"], "pending", "the stale status does not bypass publication grace")
        self.clock.advance(LOG_GRACE_SECONDS)
        self.tick()
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

    def test_infrastructure_failures_retry_with_capped_backoff_until_recovery(self):
        self.github.ci_policy = verdicts("infrastructure", "lost", "no-status", "infrastructure")
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
        self.tick()  # the second run is observed queued: bound to the attempt, never lost while queued
        self.github.start_runs()  # the `lost` run vanishes from GitHub instead of starting
        self.clock.advance(self.controller.setting("polling_interval_seconds") + 1)
        self.tick()
        self.assertIn("lost-run", self.controller.status()["head"]["ci"]["outcome_detail"])
        self.clock.advance(2 * backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 3)
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["ci"]["outcome"], "pending")
        self.clock.advance(LOG_GRACE_SECONDS)
        self.tick()
        self.assertIn("missing-status", self.controller.status()["head"]["ci"]["outcome_detail"])
        self.clock.advance(4 * backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 4)
        self.run_ci()
        self.clock.advance(8 * backoff + 1)
        reasons = self.tick()
        self.assertEqual(len(self.github.dispatches), 4, "the fourth failure was just observed")
        self.assertNotIn("exhausted", " ".join(reasons))
        self.assertEqual(self.head()["id"], head["id"], "the head waits with its membership intact")
        self.assertEqual(
            self.controller.status()["counters"].get("tasks_repair", 0), 0, "infrastructure never makes a repair task"
        )
        self.clock.advance(8 * backoff + 1)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 5, "retries continue without an operator")
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


class MissingSource(QueueFixture):
    """Scenario 25: an insertion that strands an importer is refused from the import headers."""

    def setUp(self):
        super().setUp()
        self.repo.advance_main(
            {
                "TauCeti/Provider.lean": "module\ndef provider := 1\n",
                "TauCeti/Importer.lean": "module\npublic import TauCeti.Provider\ndef importer := provider\n",
            }
        )

    def refused(self, number):
        rows = [r for r in self.controller.inputs() if r["number"] == number]
        return rows[-1]["state"] == "cancelled" and rows[-1]["reason"] == "missing-source"

    def test_s25_removal_that_strands_an_importer_is_refused_at_insertion(self):
        self.approve(101, {"TauCeti/Provider.lean": None})
        self.tick()
        self.assertTrue(self.refused(101))
        self.assertIsNone(self.head())
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source"}), 1)
        self.assertEqual(self.controller.status()["counters"].get("missing_source_refusals"), 1)
        entry = self.controller.store.rows("journal", {"kind": "probe-missing-source"})[0]
        self.assertEqual(
            json.loads(entry["detail"])["gaps"],
            [{"path": "TauCeti/Importer.lean", "module": "TauCeti.Provider", "changed": False}],
        )
        self.tick()
        body = self.github.comments_for(101)[0]["body"]
        self.assertIn("removes or renames a module that other sources still import", body)
        self.assertIn(
            "`TauCeti/Importer.lean` still imports `TauCeti.Provider`, whose source `TauCeti/Provider.lean` this PR removes.",
            body,
        )
        self.assertIn("Keep the module or update every importer, then approve it again.", body)
        self.assertEqual(self.controller.store.count("members", {}), 0)
        # The same removal with every importer updated inserts.
        self.approve(102, {"TauCeti/Provider.lean": None, "TauCeti/Importer.lean": "module\ndef importer := 1\n"})
        self.tick()
        self.assertEqual([r["state"] for r in self.controller.inputs() if r["number"] == 102], ["inserted"])

    def test_removal_is_not_a_dependency_on_a_head_that_inherited_the_source(self):
        self.approve(101, {"TauCeti/Unrelated.lean": "module\ndef unrelated := 1\n"})
        self.tick()
        head = self.head()
        self.approve(102, {"TauCeti/Provider.lean": None})
        self.tick()
        self.assertTrue(self.refused(102))
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source-waiting"}), 0)
        self.assertEqual(self.head()["tip"], head["tip"], "the running head is left alone")
        self.assertIn("this PR removes", self.github.comments_for(102)[0]["body"])
        self.run_ci()
        self.tick()
        self.assertEqual(self.repo.main_sha(), head["tip"])
        self.assertTrue(self.refused(102), "landing the inherited module cannot fix the removal")

    def test_import_after_a_batch_mate_removed_the_source_is_not_a_head_dependency(self):
        self.approve(101, {"TauCeti/Unrelated.lean": "module\ndef unrelated := 1\n"})
        self.tick()
        self.approve(102, {"TauCeti/Provider.lean": None, "TauCeti/Importer.lean": "module\ndef importer := 1\n"})
        self.tick()
        self.approve(103, {"TauCeti/Consumer.lean": "module\nimport TauCeti.Provider\n"})
        self.tick()
        self.assertTrue(self.refused(103))
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source-waiting"}), 0)

    def test_import_on_an_old_open_base_waits_for_a_module_already_landed(self):
        old_base = self.repo.main_sha()
        self.approve(101, {"TauCeti/New.lean": "module\ndef new := 1\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two by 102\nline three\n"})
        self.tick()
        self.approve(103, {"Shared.lean": "line one\nline two by 103\nline three\n"})
        self.tick()
        self.assertEqual(len(self.controller.batches("open")), 2)
        self.run_ci()
        self.tick(2)
        # The next head inherited New from its new base, but the second open batch has not rebased.
        self.assertIsNotNone(self.repo.file_at("main", "TauCeti/New.lean"))
        self.assertNotEqual(self.open_batch()["base"], self.repo.main_sha())
        self.approve(104, {"TauCeti/Consumer.lean": "module\nimport TauCeti.New\n"}, start=old_base)
        self.tick()
        row = next(r for r in self.controller.inputs() if r["number"] == 104)
        self.assertEqual(row["state"], "waiting", "refreshing the batch base will supply the source")
        self.assertEqual(self.github.comments_for(104), [])
        self.run_ci()
        self.tick(3)
        row = next(r for r in self.controller.inputs() if r["number"] == 104)
        self.assertEqual(row["state"], "inserted")

    def test_old_open_batch_does_not_supply_a_module_deleted_on_main(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.controller.set_setting("prepare_min_members", 1, "t")
        self.controller.set_setting("prepare_quiet_seconds", 0, "t")
        self.approve(101, {"TauCeti/Provider.lean": None, "TauCeti/Importer.lean": None})
        self.tick()
        self.approve(102, {"TauCeti/Unrelated.lean": "module\ndef unrelated := 1\n"})
        self.tick()
        claim = self.controller.claim_task("preparer", "admin-worker")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.kind, "prepare")
        self.run_ci()
        self.tick()
        self.assertIsNone(self.repo.file_at("main", "TauCeti/Provider.lean"))
        # Preparation keeps the old open batch on its old base; insertion uses new open capacity.
        self.approve(103, {"TauCeti/Consumer.lean": "module\nimport TauCeti.Provider\n"})
        self.tick()
        self.assertTrue(self.refused(103), "the old batch only inherited the deleted source")
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source-waiting"}), 0)

    def test_s25_bad_import_is_refused_and_a_batch_mates_module_is_a_source(self):
        self.approve(101, {"TauCeti/New.lean": "module\ndef new := 1\n"})
        self.approve(102, {"TauCeti/Consumer.lean": "module\nimport TauCeti.New\nimport TauCeti.Gone\n"})
        self.tick()
        # The gap no batch provides decides: 102 is refused even though a batch-mate provides New.
        self.assertTrue(self.refused(102))
        self.tick()
        body = self.github.comments_for(102)[0]["body"]
        self.assertIn("imports a module that no source provides", body)
        self.assertIn(
            "`TauCeti/Consumer.lean` imports `TauCeti.Gone`, but no source provides it (`TauCeti/Gone.lean`).", body
        )
        # Importing only a batch-mate's module waits while that module is in the head, then lands.
        self.approve(103, {"TauCeti/Other.lean": "module\nimport TauCeti.New\n"})
        for _ in range(3):
            self.drive()
        self.assertEqual([r["state"] for r in self.controller.inputs() if r["number"] == 103], ["landed"])
        self.assertIsNotNone(self.repo.file_at("main", "TauCeti/Other.lean"))

    def test_s25_import_of_a_module_in_the_head_waits_and_inserts_once_it_lands(self):
        self.approve(101, {"TauCeti/New.lean": "module\ndef new := 1\n"})
        self.tick()
        self.assertEqual(self.head()["size"] if "size" in self.head() else len(self.members(self.head())), 1)
        self.approve(102, {"TauCeti/Consumer.lean": "module\nimport TauCeti.New\n"})
        self.tick()
        rows = [r for r in self.controller.inputs() if r["number"] == 102]
        self.assertEqual(rows[-1]["state"], "waiting")
        # One entry per open batch probed in the pass (a second open batch is tried, as for conflicts).
        waited = self.controller.store.count("journal", {"kind": "probe-missing-source-waiting"})
        self.assertGreaterEqual(waited, 1)
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source"}), 0)
        self.assertEqual(self.github.comments_for(102), [])
        # Not probed again while the open batches keep their base; inserted and landed once the head lands.
        self.tick()
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source-waiting"}), waited)
        for _ in range(3):
            self.drive()
        self.assertEqual([r["state"] for r in self.controller.inputs() if r["number"] == 102], ["landed"])
        self.assertIsNotNone(self.repo.file_at("main", "TauCeti/Consumer.lean"))

    def states(self, number):
        return [r["state"] for r in self.controller.inputs() if r["number"] == number]

    def rechecks(self):
        return self.controller.store.count("journal", {"kind": "missing-source-recheck"})

    def test_refused_import_is_readmitted_at_its_age_once_main_gains_the_source(self):
        self.approve(101, {"TauCeti/Consumer.lean": "module\nimport TauCeti.Later\n"})
        self.tick(2)
        self.assertTrue(self.refused(101))
        refused = next(r for r in self.controller.inputs() if r["number"] == 101)
        # Unrelated landings change nothing the refusal depends on: no recheck, no probe.
        self.repo.advance_main({"TauCeti/Unrelated.lean": "module\ndef unrelated := 1\n"})
        self.tick(2)
        self.assertTrue(self.refused(101))
        self.assertEqual(self.rechecks(), 0)
        self.repo.advance_main({"TauCeti/Later.lean": "module\ndef later := 1\n"})
        self.tick()
        row = next(r for r in self.controller.inputs() if r["number"] == 101)
        self.assertEqual((row["id"], row["approved_at"]), (refused["id"], refused["approved_at"]))
        self.assertEqual(row["state"], "inserted")
        self.assertEqual(self.rechecks(), 1)
        self.assertEqual(self.controller.status()["counters"].get("missing_source_rechecks"), 1)
        for _ in range(3):
            self.drive()
        self.assertEqual(self.states(101), ["landed"])
        self.assertEqual(len(self.github.comments_for(101)), 2, "the refusal and the landing")

    def test_removal_is_readmitted_once_main_drops_the_import(self):
        self.approve(101, {"TauCeti/Provider.lean": None})
        self.tick(2)
        self.assertTrue(self.refused(101))
        # The importer changes but still imports the module: rechecked, refused again, no new comment.
        self.repo.advance_main(
            {"TauCeti/Importer.lean": "module\npublic import TauCeti.Provider\ndef importer := provider + 0\n"}
        )
        self.tick(2)
        self.assertTrue(self.refused(101))
        self.assertEqual(self.rechecks(), 1)
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source"}), 2)
        self.assertEqual(len(self.github.comments_for(101)), 1, "one refusal notice per head")
        self.tick(2)
        self.assertEqual(self.rechecks(), 1, "a repeated refusal is not rechecked until something changes")
        self.repo.advance_main({"TauCeti/Importer.lean": "module\ndef importer := 1\n"})
        self.tick()
        self.assertEqual(self.states(101), ["inserted"])
        self.assertEqual(self.rechecks(), 2)

    def test_source_entering_a_batch_readmits_the_refusal_to_wait_for_it(self):
        self.approve(101, {"TauCeti/Consumer.lean": "module\nimport TauCeti.New\n"})
        self.tick(2)
        self.assertTrue(self.refused(101))
        self.approve(102, {"TauCeti/New.lean": "module\ndef new := 1\n"})
        self.tick()
        self.assertEqual(self.states(102), ["inserted"])
        self.tick()
        self.assertEqual(self.rechecks(), 1)
        self.assertEqual(self.states(101), ["waiting"])
        for _ in range(4):
            self.drive()
        self.assertEqual(self.states(101), ["landed"])
        self.assertEqual(len(self.github.comments_for(101)), 2, "the refusal and the landing")

    def test_closed_or_pushed_refusal_is_not_readmitted(self):
        self.approve(101, {"TauCeti/Consumer.lean": "module\nimport TauCeti.Later\n"})
        self.approve(102, {"TauCeti/Other.lean": "module\nimport TauCeti.Later\n"})
        self.tick(2)
        self.assertTrue(self.refused(101) and self.refused(102))
        self.github.close_pr(PROJECT, 101, "Change 101")
        self.controller.handle_event(
            "pull_request", {"action": "closed", "number": 101, "pull_request": {"number": 101, "merged": False}}
        )
        new_head = self.github.push_pr(102, {"TauCeti/Other.lean": "module\n"})
        self.controller.handle_event(
            "pull_request", {"action": "synchronize", "number": 102, "pull_request": {"head": {"sha": new_head}}}
        )
        self.assertEqual([r["reason"] for r in self.controller.inputs()], ["closed", "head-changed"])
        self.repo.advance_main({"TauCeti/Later.lean": "module\ndef later := 1\n"})
        self.tick(2)
        self.assertEqual(self.states(101) + self.states(102), ["cancelled", "cancelled"])
        self.assertEqual(self.rechecks(), 0)

    def test_refusal_recorded_before_the_fingerprint_compares_against_its_batch_base(self):
        self.approve(101, {"TauCeti/Consumer.lean": "module\nimport TauCeti.Later\n"})
        self.tick(2)
        row = next(r for r in self.controller.inputs() if r["number"] == 101)
        evidence = json.loads(row["evidence"])
        evidence.pop(MISSING_SOURCE_WATCH)
        self.controller.store.update("inputs", {"id": row["id"]}, evidence=json.dumps(evidence))
        self.controller._missing_source_signature = None
        self.repo.advance_main({"TauCeti/Unrelated.lean": "module\ndef unrelated := 1\n"})
        self.tick()
        self.assertEqual(self.rechecks(), 0)
        self.assertIn(MISSING_SOURCE_WATCH, json.loads(self.controller.inputs()[0]["evidence"]))
        self.repo.advance_main({"TauCeti/Later.lean": "module\ndef later := 1\n"})
        self.tick()
        self.assertEqual(self.states(101), ["inserted"])

    def test_s25_gap_already_on_main_and_unavailable_scan_do_not_refuse(self):
        self.repo.advance_main({"TauCeti/Stale.lean": "module\nimport TauCeti.Gone\n"})
        self.approve(101, {"TauCeti/A.lean": "module\ndef a := 1\n"})
        self.tick()
        self.assertEqual([r["state"] for r in self.controller.inputs() if r["number"] == 101], ["inserted"])
        self.assertEqual(self.controller.store.count("journal", {"kind": "probe-missing-source"}), 0)
        with patch.object(admission_planning, "import_gaps", return_value=(None, "scan broken")):
            self.approve(102, {"TauCeti/Provider.lean": None})
            self.tick()
        self.assertEqual([r["state"] for r in self.controller.inputs() if r["number"] == 102], ["inserted"])
        entry = self.controller.store.rows("journal", {"kind": "probe-import-scan-unavailable"})[-1]
        self.assertEqual(json.loads(entry["detail"])["reason"], "scan broken")


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


class Preparation(QueueFixture):
    """Item 8 of the pipeline acceleration: an open batch is built and repaired on its own base while
    the head waits on CI, so it seals carrying the repairs and lands without a failed CI round."""

    def broken_content_policy(self):
        """CI fails a candidate whose member module still says `BROKEN`, whatever the member numbers."""

        def policy(head, _base, members):
            broken = [n for n in members if "BROKEN" in (self.repo.file_at(head, f"TauCeti/Pr{n}.lean") or "")]
            if broken:
                return "fail", [
                    {"kind": "compiler", "module": f"TauCeti/Pr{n}.lean", "message": f"error in PR {n}"} for n in broken
                ]
            return "pass", []

        return policy

    def scenario(self, prepare):
        """A head in CI and an open batch with one broken member behind it; returns the batch and the agent."""
        self.controller.set_setting("prepare_open_batches", prepare, "t")
        self.controller.set_setting("prepare_min_members", 3, "t")
        self.controller.set_setting("repair_concurrency", 2, "t")  # room for one background claim
        self.github.ci_policy = self.broken_content_policy()
        self.approve(101, {"TauCeti/Pr101.lean": "head\n"})
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "ci")
        for number in (201, 202, 203):
            body = "BROKEN\n" if number == 202 else f"{number}\n"
            self.approve(number, {f"TauCeti/Pr{number}.lean": body})
        self.tick()
        batch = self.open_batch()
        self.assertEqual(len(self.members(batch)), 3)
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#202": Resolution({"TauCeti/Pr202.lean": "202\n"}, explanation="Replaced BROKEN.")
            }
        )
        return batch, agent

    def drive_until_landed(self, batch, agents, limit=12):
        """Tick, run CI and let the agents work until `batch` has landed; returns the rounds taken."""
        for rounds in range(limit):
            self.tick()
            if self.controller.store.row("batches", {"id": batch["id"]})["state"] == "landed":
                return rounds
            head = self.controller.status()["head"]
            if head is not None and head["stage"] == "ci":
                self.run_ci()
                continue
            for agent in agents:
                agent.run(self.controller)
        self.fail(f"batch {batch['id']} did not land in {limit} rounds")

    def source_failures(self, batch):
        return self.controller.store.count("ci_attempts", {"batch_id": batch["id"], "outcome": "source-failure"})

    def test_a_prepared_batch_seals_with_its_repairs_and_lands_without_a_failed_round(self):
        batch, agent = self.scenario(prepare=True)
        result = agent.run(self.controller)
        self.assertEqual(agent.tasks[0].kind, "prepare")
        self.assertEqual((agent.tasks[0].base, agent.tasks[0].onto), (self.head()["tip"], self.head()["tip"]))
        self.assertTrue(result.accepted, result)
        prepared = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual((prepared["state"], prepared["pre_rebase_prepared"]), ("open", result.tip))
        self.drive_until_landed(batch, [agent])
        landed = self.controller.store.row("batches", {"id": batch["id"]})
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(self.source_failures(batch), 0, "the head's CI round found nothing left to repair")
        self.assertEqual([t.kind for t in agent.tasks], ["prepare"], "one intended-parent preparation, no repair round")
        self.assertEqual(
            ["pre_rebased" in t.extra for t in agent.tasks],
            [True],
            "prepared directly on the head's tip",
        )
        stacked = agent.results[0]
        self.assertTrue(stacked.accepted, stacked)
        attempts = self.controller.store.rows("ci_attempts", {"batch_id": batch["id"]}, order="id")
        self.assertEqual(attempts[0]["head_sha"], stacked.tip, "CI ran on the exact prepared tip")
        self.assertEqual(self.repo.file_at(self.repo.main_sha(), "TauCeti/Pr202.lean"), "202\n")
        member = {m["number"]: m for m in self.members(landed)}[202]
        self.assertEqual(member["provenance"], "edited")
        self.assertIn("Replaced BROKEN.", member["explanation"])

    def test_without_preparation_the_same_batch_fails_its_first_ci_round(self):
        batch, agent = self.scenario(prepare=False)
        self.assertEqual(self.controller.store.count("tasks", {"kind": "prepare"}), 0)
        self.drive_until_landed(batch, [agent])
        self.assertEqual(self.source_failures(batch), 1)
        self.assertEqual([t.kind for t in agent.tasks], ["repair"])

    def test_insertion_during_preparation_goes_to_the_next_batch_and_the_head_task_comes_first(self):
        batch, agent = self.scenario(prepare=True)
        task = agent.claim(self.controller)
        self.assertEqual(task.kind, "prepare")
        self.approve(301, {"TauCeti/Pr301.lean": "301\n"})
        self.tick()
        row = self.controller.input_for(PROJECT, 301)
        self.assertNotEqual(row["batch_id"], batch["id"], "inflow continues into the next open batch")
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["version"], batch["version"])
        tip = agent.produce(task)
        self.assertTrue(self.controller.submit_task(task.task_id, task.claim_token, tip).accepted)
        # The head fails CI: its repair task is the next claim, ahead of the newer batch's preparation.
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        self.assertEqual(
            self.controller.store.count("tasks", {"kind": "prepare", "state": "open", "pre_rebase_source": None}),
            0,
            "below 3",
        )
        head = self.head()
        self.github.runs[-1].verdict = "fail"
        self.github.runs[-1].diagnostics = [{"kind": "compiler", "module": "TauCeti/Pr101.lean", "message": "x"}]
        self.run_ci()
        self.tick()
        repair = self.agent(
            "repairer", script=lambda task: {f"{PROJECT}#101": Resolution({"TauCeti/Pr101.lean": "ok\n"})}
        )
        self.assertEqual(repair.claim(self.controller).kind, "repair")
        self.assertEqual(repair.tasks[0].batch_id, head["id"])

    def test_a_second_worker_prepares_the_open_batch_while_the_first_repairs_the_head(self):
        self.controller.set_setting("prepare_min_members", 3, "t")
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.github.ci_policy = self.broken_content_policy()
        self.approve(101, {"TauCeti/Pr101.lean": "BROKEN\n"})
        self.tick()
        head = self.head()
        for number in (201, 202, 203):
            self.approve(number, {f"TauCeti/Pr{number}.lean": "BROKEN\n" if number == 202 else f"{number}\n"})
        self.tick()
        batch = self.open_batch()
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        self.assertEqual(
            self.controller.store.count("tasks", {"kind": "prepare", "state": "open"}), 1, "beside the head's task"
        )
        repairer = self.agent(
            "repairer", script=lambda task: {f"{PROJECT}#101": Resolution({"TauCeti/Pr101.lean": "101\n"})}
        )
        preparer = self.agent(
            "preparer", script=lambda task: {f"{PROJECT}#202": Resolution({"TauCeti/Pr202.lean": "202\n"})}
        )
        repair = repairer.claim(self.controller)
        preparation = preparer.claim(self.controller)
        self.assertEqual((repair.kind, repair.batch_id), ("repair", head["id"]))
        self.assertEqual((preparation.kind, preparation.batch_id), ("prepare", batch["id"]))
        tip = preparer.produce(preparation)
        self.assertTrue(self.controller.submit_task(preparation.task_id, preparation.claim_token, tip).accepted)
        tip = repairer.produce(repair)
        self.assertTrue(self.controller.submit_task(repair.task_id, repair.claim_token, tip).accepted)
        self.drive_until_landed(batch, [repairer, preparer])
        self.assertEqual(self.source_failures(batch), 0, "prepared while the head was repaired")
        self.assertEqual([t.kind for t in preparer.tasks], ["prepare"])


class OpenBatchRebaseOntoMain(QueueFixture):
    """An open batch whose replay onto `main` conflicts gets a background `rebase_conflict` task onto
    `main`; adopting it re-bases the batch, which is then prepared on `main`."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 2, "t")  # room for one background claim
        self.controller.set_setting("prepare_min_members", 2, "t")
        self.controller.set_setting("pre_rebase_enabled", False, "t")

    def test_a_conflicting_open_batch_is_rebased_onto_main_by_a_worker_then_prepared_there(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(202, {"Shared.lean": "one\ntwo by 202\nthree\n"})
        self.approve(203, {"TauCeti/P203.lean": "203\n"})
        self.tick()
        batch = self.open_batch()
        main = self.repo.advance_main({"Shared.lean": "one\ntwo on main\nthree\n"})
        self.tick()
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        [task] = self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"})
        self.assertEqual((task["kind"], task["onto"], task["base"]), ("rebase_conflict", main, batch["base"]))
        self.tick()
        self.assertEqual(
            len(self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"})), 1, "one task"
        )

        def resolve(claimed):
            self.assertEqual(
                (claimed.kind, claimed.batch_id, claimed.extra.get("background")),
                ("rebase_conflict", batch["id"], True),
            )
            return {
                f"{PROJECT}#202": Resolution(
                    {"Shared.lean": "one\ntwo on main and by 202\nthree\n"}, explanation="Kept both line two edits."
                )
            }

        resolver = self.agent("resolver", script=resolve)
        self.assertTrue(resolver.run(self.controller).accepted)
        rebased = self.open_batch()
        self.assertEqual(rebased["id"], batch["id"])
        self.assertEqual(rebased["base"], main)
        self.assertGreater(rebased["version"], batch["version"])
        self.assertIsNone(rebased["prepared_version"], "a rebase is not a preparation")
        self.tick()
        [prepare] = self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"})
        self.assertEqual((prepare["kind"], prepare["base"]), ("prepare", main))

    def prepared_batch(self):
        """A head in CI and an open batch 202/203 prepared clean on its base."""
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(202, {"Shared.lean": "one\ntwo by 202\nthree\n"})
        self.approve(203, {"TauCeti/P203.lean": "203\n"})
        self.tick()
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        task = self.controller.claim_task("preparer", "admin-worker")
        self.assertEqual(task.kind, "prepare")
        self.controller.release_task(task.task_id, task.claim_token, "prepared_clean")
        batch = self.open_batch()
        self.assertEqual(batch["prepared_version"], batch["version"])
        return batch

    def test_a_prepared_batch_whose_replay_onto_main_conflicts_is_rebased_onto_main(self):
        batch = self.prepared_batch()
        main = self.repo.advance_main({"Shared.lean": "one\ntwo on main\nthree\n"})
        self.tick()
        self.tick()
        [task] = self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"})
        self.assertEqual((task["kind"], task["onto"]), ("rebase_conflict", main))

    def test_a_prepared_batch_whose_replay_onto_main_is_clean_keeps_its_preparation(self):
        batch = self.prepared_batch()
        self.repo.advance_main({"Elsewhere.lean": "x\n"})
        self.tick()
        self.tick()
        kept = self.open_batch()
        self.assertEqual(
            (kept["version"], kept["base"], kept["prepared_version"]),
            (batch["version"], batch["base"], batch["version"]),
        )
        self.assertEqual(self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"}), [])
        rebase_claims = self.controller.store.rows("claims", {"batch_id": batch["id"], "operation": "rebase"})
        self.assertEqual(len(rebase_claims), 1, "replayed once, then remembered")

    def test_insertion_goes_around_an_open_batch_being_rebased(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(202, {"Shared.lean": "one\ntwo by 202\nthree\n"})
        self.approve(203, {"TauCeti/P203.lean": "203\n"})
        self.tick()
        batch = self.open_batch()
        self.repo.advance_main({"Shared.lean": "one\ntwo on main\nthree\n"})
        self.tick()
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        claimed = self.agent("holder").claim(self.controller)
        self.assertEqual((claimed.kind, claimed.batch_id), ("rebase_conflict", batch["id"]))
        self.approve(204, {"TauCeti/P204.lean": "204\n"})
        self.tick()
        self.assertNotIn(
            f"{PROJECT}#204",
            [m["source_pr"] for m in self.members(self.controller.store.row("batches", {"id": batch["id"]}))],
        )


class SequencedBackgroundTasks(QueueFixture):
    """An open batch is prepared before it is pre-rebased: a preparation adopted under a running
    pre-rebase would rewrite its chain and discard the agent's conflict resolutions."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 2, "t")  # room for one background claim

    def test_the_pre_rebase_follows_the_preparation_and_seals_with_both(self):
        self.controller.set_setting("prepare_min_members", 2, "t")
        self.controller.set_setting("pre_rebase_enabled", False, "t")
        self.github.ci_policy = lambda head, _base, members: (
            ("fail", [{"kind": "compiler", "module": "TauCeti/P203.lean", "message": "error in PR 203"}])
            if "BROKEN" in (self.repo.file_at(head, "TauCeti/P203.lean") or "")
            else ("pass", [])
        )
        self.approve(101, {"Shared.lean": "line one\nline two by 101\nline three\n"})
        self.tick()
        head = self.head()
        self.approve(202, {"Shared.lean": "line one\nline two by 202\nline three\n"})
        self.approve(203, {"TauCeti/P203.lean": "BROKEN\n"})
        self.tick()
        batch = self.open_batch()
        self.assertEqual(batch["pre_rebase_state"], "")
        self.assertEqual(self.controller.store.count("tasks", {"kind": "pre_rebase"}), 0, "not before preparation")
        self.clock.advance(self.controller.setting("prepare_quiet_seconds"))
        self.tick()
        open_kinds = [
            t["kind"] for t in self.controller.store.rows("tasks", {"batch_id": batch["id"], "state": "open"})
        ]
        self.assertEqual(open_kinds, ["prepare"])
        preparer = self.agent(
            "preparer",
            script=lambda task: {
                f"{PROJECT}#203": Resolution({"TauCeti/P203.lean": "203\n"}, explanation="Replaced BROKEN.")
            },
        )
        self.assertTrue(preparer.run(self.controller).accepted)
        self.assertEqual(preparer.tasks[0].kind, "prepare")
        self.controller.set_setting("pre_rebase_enabled", True, "t")
        self.tick()
        prepared = self.open_batch()
        pre_rebase = self.controller.store.row("tasks", {"kind": "pre_rebase", "state": "open"})
        self.assertEqual((pre_rebase["tip"], pre_rebase["onto"]), (prepared["tip"], head["tip"]))

        def resolve(task):
            self.assertEqual(task.kind, "pre_rebase")
            return {
                f"{PROJECT}#202": Resolution(
                    {"Shared.lean": "line one\nline two by 101 and 202\nline three\n"},
                    explanation="Merged the line two edits of #101 and this PR.",
                )
            }

        resolver = self.agent("resolver", script=resolve)
        self.assertTrue(resolver.run(self.controller).accepted)
        self.assertEqual(self.open_batch()["pre_rebase_state"], "adopted")
        self.assertEqual(self.controller.store.count("tasks", {"batch_id": batch["id"], "state": "open"}), 0)
        self.run_ci()
        self.tick(2)
        sealed = self.head()
        self.assertEqual(sealed["id"], batch["id"])
        self.assertTrue(self.controller.store.journal_entries("rebased")[-1]["detail"]["pre_rebased"])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "landed")
        self.assertEqual(self.repo.file_at("main", "Shared.lean"), "line one\nline two by 101 and 202\nline three\n")
        self.assertEqual(self.repo.file_at("main", "TauCeti/P203.lean"), "203\n")


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


class RepairOrReject(QueueFixture):
    """Repair or reject, the author's choice: edit summaries, the `dont-repair-in-queue` label, `problem-anticipated`."""

    NOTE = (
        "Emptied: this PR's lemma duplicates one #101 adds.\n"
        "Interacting-PR: #101\n"
        "Interacting-File: Dup.lean\n"
        "Finding: duplicate declaration: `foo` is declared by both"
    )

    def edited_landing(self):
        """A head of #101 and #102; CI fails on #102, the agent fixes it, and the head lands."""
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Broken.lean": "line one\nbroken\nline three\n"})
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    {"Broken.lean": "line one\nfixed\nline three\n", "Extra.lean": "extra\n"},
                    explanation="Fixed the broken line against #101.",
                )
            }
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        return head

    def test_landing_comment_carries_the_edit_summary_and_the_diff(self):
        head = self.edited_landing()
        landed = self.controller.store.row("members", {"batch_id": head["id"], "number": 102})
        comment = self.github.comments_for(102)[0]["body"]
        self.assertIn("> Fixed the broken line against #101.", comment)
        self.assertIn("Diff of the landed commit against the accepted head", comment)
        self.assertIn("```diff\n", comment)
        self.assertIn("-broken\n+fixed", comment)
        self.assertIn("+extra", comment, "a file the agent added to the PR's commit is shown")
        self.assertIn(f"/commit/{landed['commit_sha']}", comment)
        # The clean member's comment is the plain notice.
        self.assertNotIn("Diff of the landed commit", self.github.comments_for(101)[0]["body"])
        summary = self.controller.store.journal_entries("edit-summary")
        self.assertEqual([(e["detail"]["number"], e["detail"]["truncated"]) for e in summary], [(102, False)])
        self.assertEqual(self.controller.status()["counters"]["edit_summaries"], 1)

    def test_landing_git_work_runs_without_the_locks_and_once_per_member(self):
        controller = self.controller
        diffs, git_reads = [], []
        real_diff, real_fact = controller._landed_diff, controller._landing_fact

        def landed_diff(member):
            diffs.append((member["number"], controller._lock.owned(), controller.store._depth))
            return real_diff(member)

        def landing_fact(*args, **kwargs):
            git_reads.append((args[1]["number"], controller._lock.owned()))
            return real_fact(*args, **kwargs)

        with (
            patch.object(controller, "_landed_diff", landed_diff),
            patch.object(controller, "_landing_fact", landing_fact),
        ):
            head = self.edited_landing()
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertEqual(diffs, [(102, False, 0)], "one diff, with neither the controller nor the store lock")
        self.assertEqual(sorted(git_reads), [(101, False), (102, False)])
        self.assertIn("-broken\n+fixed", self.github.comments_for(102)[0]["body"])
        self.assertIn(f"repaired-label:{head['id']}:102", [e["key"] for e in controller.store.rows("outbox")])

    def test_a_landing_recorded_while_the_lock_was_free_is_not_recorded_again(self):
        controller = self.controller
        real_facts = controller._landing_facts
        raced = []

        def landing_facts(batch, members, ranges, **kwargs):
            facts = real_facts(batch, members, ranges, **kwargs)
            if kwargs.get("partial") and not raced:
                raced.append(batch["id"])
                with controller._lock:  # another path lands the same head meanwhile
                    controller._landed(controller.store.row("batches", {"id": batch["id"]}), "promoted", 0)
            return facts

        with patch.object(controller, "_landing_facts", landing_facts):
            head = self.edited_landing()
        self.assertEqual(raced, [head["id"]])
        landed = [e for e in controller.store.journal_entries("landed") if e["batch_id"] == head["id"]]
        self.assertEqual(len(landed), 1)
        self.assertEqual(len(self.github.comments_for(102)), 1)

    def test_a_revision_replaced_while_the_lock_was_free_is_not_recorded_as_landed(self):
        controller = self.controller
        real_facts = controller._landing_facts
        replaced = []

        def landing_facts(batch, members, ranges, **kwargs):
            facts = real_facts(batch, members, ranges, **kwargs)
            if kwargs.get("partial") and not replaced:
                replaced.append(batch["id"])
                # As a rebuild or repair submission would, meanwhile: a revision not on canonical.
                tree = chain.tree_of(controller.clone, batch["base"])
                ident = {
                    f"GIT_{who}_{key}": value
                    for who in ("AUTHOR", "COMMITTER")
                    for key, value in (("NAME", "t"), ("EMAIL", "t@example.invalid"))
                }
                tip = chain.git(
                    controller.clone, "commit-tree", tree, "-p", batch["base"], "-m", "rebuilt", env=ident
                ).strip()
                replaced.append(tip)
                with controller._lock:
                    controller.store.update("batches", {"id": batch["id"]}, version=batch["version"] + 1, tip=tip)
            return facts

        with patch.object(controller, "_landing_facts", landing_facts):
            head = self.edited_landing()
        (superseded,) = controller.store.journal_entries("landing-superseded")
        self.assertEqual(superseded["batch_id"], head["id"])
        self.assertEqual(superseded["detail"]["current_tip"], replaced[1])
        landed = [e for e in controller.store.journal_entries("landed") if e["batch_id"] == head["id"]]
        self.assertEqual(landed, [], "the replacement revision never reached canonical")
        self.assertNotEqual(controller.store.row("batches", {"id": head["id"]})["state"], "landed")

    def test_a_member_changed_while_the_lock_was_free_is_read_again_under_it(self):
        controller = self.controller
        real_facts = controller._landing_facts

        def landing_facts(batch, members, ranges, **kwargs):
            facts = real_facts(batch, members, ranges, **kwargs)
            if kwargs.get("partial"):
                for fact in facts.values():
                    fact["commit"] = "0" * 40  # as if each member's commit moved meanwhile
            return facts

        with patch.object(controller, "_landing_facts", landing_facts):
            head = self.edited_landing()
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertIn("-broken\n+fixed", self.github.comments_for(102)[0]["body"])

    def test_edited_member_is_labelled_repaired_in_queue(self):
        head = self.edited_landing()
        keys = [e["key"] for e in self.controller.store.rows("outbox", {"kind": "labels"})]
        self.assertIn(f"repaired-label:{head['id']}:102", keys)
        self.assertNotIn(f"repaired-label:{head['id']}:101", keys, "a clean member is not labelled")
        self.assertIn("repaired-in-queue", self.github.labels[102])
        self.assertNotIn("repaired-in-queue", self.github.labels.get(101, set()))
        self.assertEqual(
            self.github.repository_labels["repaired-in-queue"][1],
            "The merge queue modified this contribution while integrating it; see the landing comment",
        )
        self.assertEqual([c for c, _ in self.github.calls].count("ensure_label"), 1)

    def test_empty_repaired_label_disables_it(self):
        self.controller.set_setting("repaired_label", "", "operator")
        head = self.edited_landing()
        keys = [e["key"] for e in self.controller.store.rows("outbox", {"kind": "labels"})]
        self.assertFalse([k for k in keys if k.startswith("repaired-label:")])
        self.assertNotIn("repaired-in-queue", self.github.labels.get(102, set()))
        self.assertNotIn("ensure_label", [c for c, _ in self.github.calls])
        self.assertTrue(self.controller.store.journal_entries("edit-summary"), f"head {head['id']} still summarised")

    def withdrawn_head(self, interacting=101):
        """A head of #101 and #102, #102 labelled `dont-repair-in-queue`; CI fails on #102 and the agent empties it."""
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"Dup.lean": "theorem foo : True := trivial\n"})
        self.approve(102, {"Other.lean": "theorem foo : True := trivial\n"}, labels=["dont-repair-in-queue"])
        self.assertEqual(self.controller.status()["counters"]["no_repair_opt_outs"], 1)
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        seen = {}

        def script(task):
            seen.update({m.number: (m.no_repair, m.withdrawn) for m in task.members})
            return {f"{PROJECT}#102": Resolution(empty=True, explanation=self.NOTE.replace("#101", f"#{interacting}"))}

        agent = self.agent(script=script)
        self.github.ci_policy = verdicts()
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual(seen, {101: (False, False), 102: (True, False)})
        self.assertEqual(adoption.provenance[f"{PROJECT}#102"], "withdrawn")
        return head

    def test_a_labelled_member_the_agent_empties_is_withdrawn_and_not_closed_as_merged(self):
        head = self.withdrawn_head()
        row = {i["number"]: i for i in self.controller.inputs()}[102]
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "problem-anticipated"))
        member = self.controller.store.row("members", {"batch_id": head["id"], "number": 102})
        self.assertEqual((member["withdrawn"], member["removed"]), (1, 0), "the empty commit stays in the chain")
        entry = self.controller.store.journal_entries("problem-anticipated")[0]["detail"]
        self.assertEqual(
            (entry["interacting"], entry["file"], entry["finding"]),
            (101, "Dup.lean", "duplicate declaration: `foo` is declared by both"),
        )
        self.tick()
        comment = self.github.comments_for(102)[0]["body"]
        self.assertIn("carries `dont-repair-in-queue`, so the merge queue does not edit it", comment)
        self.assertIn("- Interacting PR: #101\n- File: `Dup.lean`\n- Finding: duplicate declaration", comment)
        self.assertIn("You cannot reproduce this on your branch yet: #101 is still in staging", comment)
        self.assertEqual(self.github.labels[102], {"dont-repair-in-queue", "bors-staging"}, "no label change yet")
        self.assertEqual(self.controller.status()["anticipated"][0]["interacting"], 101)
        # A later task on the head sees it withdrawn; an edit giving it content back is refused.
        self.assertEqual(self.controller.status()["counters"]["no_repair_withdrawals"], 1)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": head["id"]})["tip"])
        self.assertIsNone(self.repo.file_at("main", "Other.lean"))
        self.assertEqual(self.github.prs[102]["state"], "open")
        self.assertNotIn(102, self.github.closed)
        self.assertIsNone(self.controller.store.row("outbox", {"key": f"landed-close:{head['id']}:102"}))
        self.assertEqual(self.controller.store.journal_entries("landed")[0]["detail"]["withdrawn"], [102])
        self.assertEqual({i["number"]: i["state"] for i in self.controller.inputs()}[102], "cancelled")
        outcomes = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        self.assertEqual({o.pr: o.state for o in outcomes}, {101: "ok", 102: "canceled"})
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 0)

    def test_the_interacting_pr_landing_converts_the_withdrawal_to_needs_rebase(self):
        head = self.withdrawn_head()
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertEqual([e["detail"]["number"] for e in self.controller.store.journal_entries("needs-rebase")], [102])
        bodies = [c["body"] for c in self.github.comments_for(102)]
        self.assertEqual(len(bodies), 2)
        self.assertIn("#101 has landed on `main`, so the problem the merge queue anticipated", bodies[1])
        self.assertIn("now reproducible", bodies[1])
        self.assertIn("needs-rebase", self.github.labels[102])
        self.assertEqual(self.controller.status()["anticipated"], [])
        self.assertEqual(self.controller.status()["counters"]["problem_anticipated_needs_rebase"], 1)
        # The PR re-enters through ordinary re-approval: the author rebases and is approved again.
        new_head = self.github.push_pr(102, {"Other.lean": "theorem foo' : True := trivial\n"})
        self.assertTrue(self.controller.approve(PROJECT, 102, new_head, "reviewer")["accepted"])
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "Other.lean"), "theorem foo' : True := trivial\n")
        self.assertIn(102, self.github.closed)

    def test_a_rejected_interacting_pr_readmits_the_opt_out_withdrawal(self):
        self.withdrawn_head()
        self.github.ci_policy = verdicts("fail")
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            name="second-repair",
            script=lambda task: {
                f"{PROJECT}#101": Resolution(empty=True, explanation="Dropped A: no contribution remained.")
            },
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "empty-commit")
        row = self.controller.input_for(PROJECT, 102)
        self.assertEqual(json.loads(row["anticipated"])["stage"], "readmitted")
        self.assertNotIn("needs-rebase", self.github.labels[102])
        self.assertNotIn(102, self.github.closed)
        self.assertEqual(self.controller.store.journal_entries("needs-rebase"), [])

    def test_the_interacting_pr_leaving_the_queue_readmits_the_withdrawn_pr_at_its_original_age(self):
        approved_at = None
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Other.lean": "other\n"}, labels=["dont-repair-in-queue"])
        approved_at = {i["number"]: i for i in self.controller.inputs()}[102]["approved_at"]
        self.tick()
        head = self.head()
        self.run_ci()
        self.tick()
        self.approve(105, {"E.lean": "e\n"})  # waiting: the agent names it as the other side
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#102": Resolution(empty=True, explanation=self.NOTE.replace("#101", "#105"))
            }
        )
        self.github.ci_policy = verdicts()
        self.assertTrue(agent.run(self.controller).accepted)
        self.assertTrue(self.controller.cancel(PROJECT, 105, "author")["withdrawn"])
        self.clock.advance(60)
        self.tick()
        row = {i["number"]: i for i in self.controller.inputs()}[102]
        self.assertIn(row["state"], ("waiting", "inserted"))
        self.assertEqual(row["approved_at"], approved_at)
        self.assertEqual([e["detail"]["number"] for e in self.controller.store.journal_entries("readmitted")], [102])
        self.assertIn("left the merge queue without landing", self.github.comments_for(102)[-1]["body"])
        self.assertEqual(self.controller.status()["counters"]["problem_anticipated_readmitted"], 1)
        # The head lands with #102's empty commit; the re-admitted #102 lands with its content afterwards
        # (the empty commit on `main` does not count as #102 having landed) and is closed once.
        for _ in range(4):
            self.tick()
            if self.head() is not None and self.controller.status()["head"]["stage"] == "ci":
                self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertEqual(self.repo.file_at("main", "Other.lean"), "other\n")
        self.assertEqual({i["number"]: i["state"] for i in self.controller.inputs()}[102], "landed")
        self.assertIn(102, self.github.closed)

    def test_an_edit_that_gives_a_no_repair_member_content_is_refused(self):
        self.github.ci_policy = fail_members(102)
        self.approve(101, {"A.lean": "a\n"})
        self.approve(102, {"Broken.lean": "broken\n"}, labels=["dont-repair-in-queue"])
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#102": Resolution({"Broken.lean": "fixed\n"}, explanation="Fixed it.")}
        )
        adoption = agent.run(self.controller)
        self.assertFalse(adoption.accepted)
        self.assertEqual(adoption.reason, "no-repair-edited")
        self.assertEqual(adoption.violations[0]["source_pr"], f"{PROJECT}#102")


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
    def test_polling_uses_remaining_capacity_without_a_fixed_reserve(self):
        self.github.remaining = 1
        self.approve(101, {"A.lean": "a\n"})
        loop = ReconciliationLoop(self.controller)
        loop.run_once(woken=False)
        self.assertEqual(len(self.github.dispatches), 1)
        self.run_ci()
        loop.run_once(woken=False)
        self.assertEqual(self.batch_state(1), "landed")

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
            if c
            in (
                "dispatch_candidate",
                "fast_forward",
                "post_comment",
                "set_labels",
                "ensure_label",
                "close_pr",
                "update_ref",
            )
        ]
        self.assertEqual(writes, [])
        # Bors lands the same PR; the service observes the canonical advance and records its head as landed.
        member_commit = self.members(self.head())[0]["commit_sha"]
        self.github.update_ref(PROJECT, "refs/heads/main", member_commit)
        self.tick()
        landed = self.controller.store.row("batches", {"id": 1})
        self.assertEqual((landed["state"], landed["landed_how"]), ("landed", "external"))


class PreRebase(QueueFixture):
    """The next open batch is replayed onto the head's tip once the head's chain is submitted (item 9)."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("repair_concurrency", 2, "t")  # room for one background claim

    def journal(self, kind):
        return self.controller.store.journal_entries(kind)

    def test_pre_rebase_follows_the_submitted_head_is_redone_on_a_tip_change_and_seals_without_a_replay(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.assertEqual(self.controller.status()["head"]["stage"], "ci")
        self.approve(102, {"B.lean": "b\n"})
        self.tick()
        batch = self.open_batch()
        # The head is dispatched (its chain is the controller's clean build): the open batch is replayed onto it.
        self.assertEqual(batch["base"], self.repo.main_sha(), "the open batch stays on its own base")
        self.assertEqual((batch["pre_rebase_state"], batch["pre_rebase_onto"]), ("clean", head["tip"]))
        self.assertEqual(batch["pre_rebase_source"], batch["tip"])
        self.assertEqual(
            [c.source_pr for c in chain.chain_commits(self.repo.clone, head["tip"], batch["pre_rebase_tip"])],
            [f"{PROJECT}#102"],
        )
        self.assertEqual(
            self.controller.status()["open_batches"][0]["pre_rebase"]["state"], "clean", "status reports it"
        )
        self.assertTrue(self.controller.status()["open_batches"][0]["pre_rebase"]["current"])
        # CI fails: while the head is being repaired nothing is replayed onto it, though insertion goes on.
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        batch = self.open_batch()
        self.assertEqual([m["number"] for m in self.members(batch)], [102, 103])
        self.assertNotEqual(batch["pre_rebase_source"], batch["tip"], "not extended while the head has a task")
        self.assertEqual(len(self.journal("pre-rebased")), 1)
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "a fixed\n"}, explanation="Fixed A.")}
        )
        self.assertEqual(agent.run(self.controller).accepted, True)
        self.tick()
        repaired = self.head()
        self.assertNotEqual(repaired["tip"], head["tip"])
        # The head's tip changed: the pre-rebase is redone onto it, carrying the earlier one forward.
        batch = self.open_batch()
        self.assertEqual((batch["pre_rebase_onto"], batch["pre_rebase_source"]), (repaired["tip"], batch["tip"]))
        self.assertEqual([e["detail"]["onto"] for e in self.journal("pre-rebased")], [head["tip"], repaired["tip"]])
        self.assertTrue(self.journal("pre-rebased")[-1]["detail"]["carried"])
        pre_tip = batch["pre_rebase_tip"]
        # The head lands; the next head seals from the pre-rebased chain without replaying it.
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), repaired["tip"])
        sealed = self.head()
        self.assertEqual(sealed["id"], batch["id"])
        self.assertEqual((sealed["base"], sealed["tip"]), (repaired["tip"], pre_tip))
        rebased = self.journal("rebased")[-1]
        self.assertTrue(rebased["detail"]["pre_rebased"])
        self.assertEqual([m["commit_sha"] for m in self.members(sealed)], [c.sha for c in self.chain_commits(sealed)])
        self.assertEqual(self.controller.status()["counters"]["pre_rebases_used"], 1)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), pre_tip)
        self.assertEqual(
            [self.repo.file_at("main", name) for name in ("A.lean", "B.lean", "C.lean")], ["a fixed\n", "b\n", "c\n"]
        )
        self.assertEqual({row["state"] for row in self.controller.inputs()}, {"landed"})

    def test_pre_rebase_conflict_is_a_task_with_cross_batch_ripples_resolved_and_sealed_from(self):
        self.repo.advance_main({"TauCeti/Basic.lean": "theorem foo_bar : True := trivial\n"}, "declare foo_bar")
        self.approve(
            101,
            {
                "Shared.lean": "line one\nline two changed by 101\nline three\n",
                "TauCeti/Basic.lean": "theorem foo_baz : True := trivial\n\ntheorem both_add : True := trivial\n",
            },
        )
        self.tick()
        head = self.head()
        self.approve(
            102,
            {
                "Shared.lean": "line one\nline two changed by 102\nline three\n",
                "TauCeti/Use.lean": "theorem uses : True := foo_bar\n",
            },
        )
        self.approve(103, {"TauCeti/Also.lean": "theorem both_add : True := trivial\n"})
        self.tick()
        batch = self.open_batch()
        self.assertEqual([m["number"] for m in self.members(batch)], [102, 103])
        self.assertEqual((batch["pre_rebase_state"], batch["pre_rebase_tip"]), ("conflict", None))
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "ci", "the head's CI round is not disturbed")
        task = next(t for t in status["tasks"] if t["kind"] == "pre_rebase")
        self.assertEqual(
            (task["batch_id"], status["open_batches"][0]["pre_rebase"]["task_id"]), (batch["id"], task["task_id"])
        )
        conflict, cross = task["diagnostics"]
        self.assertEqual((conflict["kind"], conflict["source_pr"]), ("conflict", f"{PROJECT}#102"))
        self.assertEqual((conflict["paths"], conflict["onto"]), (["Shared.lean"], head["tip"]))
        # The head renames foo_bar, which 102 uses, and adds both_add, which 103 adds too.
        self.assertEqual(cross["kind"], "cross_batch")
        self.assertEqual(
            [
                (r["name"], r["removed_by"][0]["renamed_to"], [u["file"] for u in r["references"]])
                for r in cross["removed_used"]
            ],
            [("foo_bar", "foo_baz", ["TauCeti/Use.lean"])],
        )
        self.assertEqual(
            [(a["name"], [w["pr"] for w in a["head"]], [w["pr"] for w in a["next"]]) for a in cross["added_by_both"]],
            [("both_add", [101], [103])],
        )

        def script(task):
            self.assertEqual((task.kind, task.onto, task.base), ("pre_rebase", head["tip"], batch["base"]))
            self.assertEqual([m.number for m in task.members], [102, 103])
            return {
                f"{PROJECT}#102": Resolution(
                    {
                        "Shared.lean": "line one\nline two changed by 101 and 102\nline three\n",
                        "TauCeti/Use.lean": "theorem uses : True := foo_baz\n",
                    },
                    explanation="Merged the line two edits of #101 and this PR; foo_bar is foo_baz after #101.",
                ),
                f"{PROJECT}#103": Resolution(
                    {"TauCeti/Also.lean": None}, empty=True, explanation="#101 adds both_add."
                ),
            }

        agent = self.agent(script=script)
        adoption = agent.run(self.controller)
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual(adoption.provenance, {f"{PROJECT}#102": "edited", f"{PROJECT}#103": "edited"})
        adopted = self.open_batch()
        self.assertEqual(
            (adopted["state"], adopted["base"], adopted["tip"], adopted["version"]),
            ("open", batch["base"], batch["tip"], batch["version"]),
            "the batch stays open on its own base with its own chain",
        )
        self.assertEqual((adopted["pre_rebase_state"], adopted["pre_rebase_tip"]), ("adopted", adoption.tip))
        self.assertEqual(self.controller.input_for(PROJECT, 103)["state"], "inserted")
        self.assertEqual(self.github.comments_for(103), [], "speculative repair does not reject a PR")
        # Insertion goes on; the next pass replays the new member onto the adopted chain.
        self.approve(104, {"D.lean": "d\n"})
        self.tick()
        extended = self.open_batch()
        self.assertEqual([m["number"] for m in self.members(extended)], [102, 103, 104])
        self.assertEqual((extended["pre_rebase_state"], extended["pre_rebase_source"]), ("adopted", extended["tip"]))
        self.assertTrue(chain.is_ancestor(self.repo.clone, adoption.tip, extended["pre_rebase_tip"]))
        self.run_ci()
        self.tick(2)
        sealed = self.head()
        self.assertEqual(sealed["id"], batch["id"])
        self.assertEqual((sealed["base"], sealed["tip"]), (head["tip"], extended["pre_rebase_tip"]))
        self.assertTrue(self.journal("rebased")[-1]["detail"]["pre_rebased"])
        self.assertEqual(self.controller.store.count("tasks", {"kind": "rebase_conflict"}), 0)
        self.assertEqual(
            {m["number"]: m["provenance"] for m in self.members(sealed)}, {102: "edited", 103: "edited", 104: "clean"}
        )
        self.run_ci()
        self.tick(2)
        self.assertEqual(
            self.repo.file_at("main", "Shared.lean"), "line one\nline two changed by 101 and 102\nline three\n"
        )
        self.assertEqual(self.repo.file_at("main", "TauCeti/Use.lean"), "theorem uses : True := foo_baz\n")
        self.assertIn("edited this contribution", self.github.comments_for(102)[0]["body"])
        self.assertNotIn("edited this contribution", self.github.comments_for(104)[0]["body"])
        self.assertEqual(self.controller.input_for(PROJECT, 103)["reason"], "empty-commit")
        [comment] = self.github.comments_for(103)
        self.assertIn("rejected during staging repair", comment["body"])
        self.assertIn("#101 adds both_add.", comment["body"])
        self.assertEqual(self.github.closed[103], "Change 103")

    def test_a_head_tip_change_supersedes_the_open_pre_rebase_task_and_redoes_it(self):
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        first = self.controller.store.row("tasks", {"kind": "pre_rebase", "state": "open"})
        self.assertIsNotNone(first)
        self.run_ci()
        self.tick()
        # The head's repair task comes first for a worker; its adoption moves the head's tip.
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "a\n"}, explanation="Added A.")}
        )
        self.assertEqual(agent.run(self.controller).accepted, True)
        self.assertEqual(agent.tasks[-1].kind, "repair")
        self.tick()
        tip = self.head()["tip"]
        self.assertEqual(self.controller.store.row("tasks", {"id": first["id"]})["state"], "superseded")
        second = self.controller.store.row("tasks", {"kind": "pre_rebase", "state": "open"})
        self.assertEqual(second["onto"], tip)
        self.assertEqual([e["detail"]["onto"] for e in self.journal("pre-rebase-conflict")][-1], tip)

    def test_a_claimed_pre_rebase_survives_a_head_repair_round_and_is_carried_onto_its_new_tip(self):
        self.controller.set_setting("repair_concurrency", 2, "kim")
        self.github.ci_policy = verdicts("fail")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        resolver = self.agent(
            "resolver",
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"},
                    explanation="Merged the line two edits.",
                )
            },
        )
        task = resolver.claim(self.controller)
        self.assertEqual(task.kind, "pre_rebase")
        old_tip = self.head()["tip"]
        self.run_ci()
        self.tick()
        repairer = self.agent(script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "a\n"}, explanation="A.")})
        self.assertTrue(repairer.run(self.controller).accepted)
        self.tick()
        new_tip = self.head()["tip"]
        self.assertNotEqual(new_tip, old_tip)
        row = self.controller.store.row("tasks", {"id": task.task_id})
        self.assertEqual(row["state"], "open", "the claimed resolution is not restarted")
        self.assertEqual(self.journal("pre-rebase-preserved")[-1]["detail"]["onto"], old_tip)
        self.assertEqual(self.controller.store.count("tasks", {"kind": "pre_rebase"}), 1)
        result = self.controller.submit_task(task.task_id, task.claim_token, resolver.produce(task))
        self.assertTrue(result.accepted, result)
        self.tick()
        batch = self.open_batch()
        self.assertEqual((batch["pre_rebase_state"], batch["pre_rebase_onto"]), ("adopted", new_tip))
        self.assertTrue(self.journal("pre-rebased")[-1]["detail"]["carried"])
        self.assertEqual(
            chain.git(self.repo.clone, "show", f"{batch['pre_rebase_tip']}:Shared.lean"),
            "line one\nline two changed by 101 and 102\nline three\n",
        )

    def test_a_preserved_pre_rebase_submitted_after_the_repaired_head_lands_is_carried_onto_main(self):
        self.controller.set_setting("repair_concurrency", 2, "kim")
        self.controller.set_setting("claim_ttl_seconds", 86400, "kim")  # the resolver outlasts two head rounds
        self.github.ci_policy = verdicts("fail", "pass")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        resolver = self.agent(
            "resolver",
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"},
                    explanation="Merged the line two edits.",
                )
            },
        )
        task = resolver.claim(self.controller)
        head = self.head()
        self.run_ci()
        self.tick()
        repairer = self.agent(script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "a\n"}, explanation="A.")})
        self.assertTrue(repairer.run(self.controller).accepted)
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": head["id"]})["state"], "landed")
        self.assertIsNone(self.head(), "sealing waits for the claimed resolution")
        result = self.controller.submit_task(task.task_id, task.claim_token, resolver.produce(task))
        self.assertTrue(result.accepted, result)
        self.tick()
        sealed = self.head()
        self.assertEqual(sealed["base"], self.repo.main_sha())
        self.assertTrue(self.journal("rebased")[-1]["detail"]["pre_rebased"])
        self.assertEqual(self.controller.store.count("tasks", {"kind": "rebase_conflict"}), 0)
        self.assertEqual(
            chain.git(self.repo.clone, "show", f"{sealed['tip']}:Shared.lean"),
            "line one\nline two changed by 101 and 102\nline three\n",
        )


class PublishedBaseCache(QueueFixture):
    """Staging is dispatched only on a base whose Lake cache is published (`_base_cache_ready`)."""

    TOOLCHAIN = "leanprover/lean4:v4.36.0-rc2"
    MAPS = "https://cache.taucetiproject.org/revisions/TauCetiProject/TauCeti/tc/leanprover--lean4---v4.36.0-rc2/"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        files = {"README.md": "readme\n", "Shared.lean": "line one\n", "lean-toolchain": self.TOOLCHAIN + "\n"}
        self.controller, self.github, self.repo, self.clock = make_controller(self.root, files=files)
        self.addCleanup(self.controller.store.close)
        self.github.admins.add("admin-worker")
        self.published = set()
        self.probed = []
        self.controller.revision_map_probe = self.probe
        self.notifier = self.controller.notifier = RecordingNotifier()

    def probe(self, url):
        self.probed.append(url)
        self.assertTrue(url.startswith(self.MAPS) and url.endswith(".jsonl"), url)
        return url[len(self.MAPS) : -len(".jsonl")] in self.published

    def journal(self, kind):
        return self.controller.store.journal_entries(kind)

    def test_published_base_dispatches(self):
        base = self.repo.main_sha()
        self.published.add(base)
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        self.tick()
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(self.probed, [self.MAPS + base + ".jsonl"])
        self.assertEqual(self.journal("base-cache-wait"), [])
        self.assertIsNone(self.controller.status()["head"]["base_cache_wait"])

    def test_unpublished_base_waits_then_dispatches_once_published(self):
        base = self.repo.main_sha()
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        reasons = self.tick()
        self.assertEqual(self.github.dispatches, [])
        self.assertIn(f"head 1 waiting for base {base} cache to be published", " ".join(reasons))
        status = self.controller.status()
        self.assertEqual(status["head"]["stage"], "dispatch")
        self.assertEqual(status["head"]["base_cache_wait"]["base"], base)
        # A miss is remembered for `base_cache_recheck_seconds`: further passes neither probe nor journal.
        self.clock.advance(60)
        self.tick(3)
        self.assertEqual(len(self.probed), 1)
        self.assertEqual(self.github.dispatches, [])
        self.assertEqual([e["detail"]["base"] for e in self.journal("base-cache-wait")], [base])
        self.published.add(base)
        self.clock.advance(60)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(self.github.dispatches[0]["client_payload"]["base_sha"], base)
        [published] = self.journal("base-cache-published")
        self.assertEqual(published["detail"]["revision"], base)
        self.assertEqual(published["detail"]["waited_seconds"], 120)
        self.assertIsNone(self.controller.status()["head"]["base_cache_wait"])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.main_sha(), self.controller.store.row("batches", {"id": 1})["tip"])

    def test_base_differing_from_a_published_ancestor_outside_build_inputs_is_covered(self):
        published = self.repo.main_sha()
        self.published.add(published)
        self.repo.advance_main({".github/workflows/ci.yml": "jobs: {}\n"}, "harness change")
        harness = self.repo.advance_main({"scripts/check.sh": "true\n", "README.md": "new\n"}, "another")
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        self.tick()
        self.assertEqual(self.head()["base"], harness)
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(self.probed[-1], self.MAPS + published + ".jsonl")
        self.assertEqual(len(self.probed), 3)

    def test_build_input_change_after_the_published_ancestor_waits(self):
        published = self.repo.main_sha()
        self.published.add(published)
        lakefile = self.repo.advance_main({"lakefile.toml": 'name = "TauCeti"\n'}, "lakefile change")
        base = self.repo.advance_main({"README.md": "new\n"}, "harness change")
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        reasons = self.tick()
        self.assertEqual(self.github.dispatches, [])
        # The lakefile commit has the base's build inputs; the walk stops before the published commit.
        self.assertEqual(self.probed, [self.MAPS + base + ".jsonl", self.MAPS + lakefile + ".jsonl"])
        self.assertIn("itself or its 1 first-parent ancestors with the same build inputs", " ".join(reasons))

    def test_lake_build_inputs(self):
        from tauceti_queue.controller import lake_build_input

        for path in ("TauCeti.lean", "TauCeti/A/B.lean", "scripts/Axioms.lean", "lakefile.toml", "lakefile.lean"):
            self.assertTrue(lake_build_input(path), path)
        for path in ("lake-manifest.json", "lean-toolchain"):
            self.assertTrue(lake_build_input(path), path)
        for path in (".github/workflows/ci.yml", "scripts/lint.sh", "docs/x.md", "web/lean-toolchain", "tauceti.toml"):
            self.assertFalse(lake_build_input(path), path)

    def test_emergency_batch_dispatches_on_an_unpublished_base(self):
        base = self.repo.main_sha()
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        self.tick()
        self.assertEqual(self.github.dispatches, [])
        self.controller.store.update("batches", {"id": self.head()["id"]}, urgent=2)
        self.tick()
        self.assertEqual(len(self.github.dispatches), 1)
        [exempt] = self.journal("dispatch-unpublished-base")
        self.assertEqual(exempt["detail"]["base"], base)

    def test_long_wait_alerts_once_and_a_new_base_is_journaled_again(self):
        base = self.repo.main_sha()
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        self.tick()
        self.clock.advance(7200)
        self.tick()
        alerts = self.controller.status()["alerts"]
        self.assertTrue(any(f"for base {base} cache to be published" in alert for alert in alerts), alerts)
        self.clock.advance(600)
        self.tick(2)
        self.assertEqual(len(self.journal("base-cache-alert")), 1)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn(base, self.notifier.messages[0])
        # Main advances while the head waits: the head is rebased and waits for the new base, journaled once.
        moved = self.repo.advance_main({"TauCeti/Other.lean": "o\n"})
        self.tick(2)
        self.assertEqual(self.head()["base"], moved)
        self.assertEqual([e["detail"]["base"] for e in self.journal("base-cache-wait")], [base, moved])
        self.assertEqual(self.github.dispatches, [])
        self.assertEqual(self.controller.status()["head"]["base_cache_wait"]["base"], moved)


class EmergencyLane(QueueFixture):
    """Slice 1 of the emergency lane: an admin's request makes an approved PR the next head on its own."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("emergency_enabled", True, "t")

    def emergency(self, number, actor="admin-worker", **kwargs):
        return self.controller.request_emergency(PROJECT, number, actor, surface="test", **kwargs)

    def journal(self, kind):
        return [e["detail"] for e in self.controller.store.journal_entries(kind)]

    def emergency_batches(self):
        return self.controller.store.rows("batches", {"urgent": 2}, order="id")

    def outcomes(self, number):
        rows = feeds.ControllerFeedSource(self.controller).outcomes(PROJECT, "main")
        return [(o.batch_id, o.state) for o in rows if o.pr == number]

    def running_head_with_successor(self):
        """A dispatched head (101) and an open batch (102, 103) on the old main."""
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        head = self.head()
        self.approve(102, {"B.lean": "b\n"})
        self.approve(103, {"C.lean": "c\n"})
        self.tick()
        return head, self.open_batch()

    def test_disabled_by_default_and_needs_a_live_input_at_the_live_head(self):
        self.controller.set_setting("emergency_enabled", False, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.assertEqual(self.emergency(101)["reason"], "disabled")
        self.controller.set_setting("emergency_enabled", True, "t")
        self.github.create_pr(102, {"B.lean": "b\n"})
        self.assertEqual(self.emergency(102)["reason"], "not-approved", "the flag is never an approval")
        self.github.push_pr(101, {"A.lean": "a2\n"})
        self.assertEqual(self.emergency(101)["reason"], "head-moved")
        self.assertEqual(
            [d["reason"] for d in self.journal("emergency-refused")], ["disabled", "not-approved", "head-moved"]
        )
        self.assertEqual(self.emergency_batches(), [])

    def test_next_head_ahead_of_the_sealed_successor_and_a_second_request_queues(self):
        head, successor = self.running_head_with_successor()
        self.approve(104, {"D.lean": "d\n"})
        accepted = self.emergency(104)
        self.assertEqual((accepted["accepted"], accepted["behind"]), (True, None))
        self.tick()
        [batch] = self.emergency_batches()
        self.assertEqual((batch["state"], [m["number"] for m in self.members(batch)]), ("open", [104]))
        self.assertEqual([m["number"] for m in self.members(successor)], [102, 103], "never drawn into a batch")
        self.assertEqual(self.head()["id"], head["id"], "the running head is not evicted")
        self.assertEqual(self.github.runs[0].status, "queued", "nor its run cancelled")
        self.assertEqual(self.controller.store.count("tasks", {"batch_id": batch["id"]}), 0)
        self.assertIn(batch["id"], [b["id"] for b in self.controller._live_batches()], "its refs are republished")
        status = self.controller.status()["emergency"]
        self.assertEqual((status["batch"]["batch_id"], status["batch"]["number"]), (batch["id"], 104))
        self.assertEqual(status["batch"]["waiting_for"], f"head {head['id']} to land")
        self.assertEqual(self.journal("emergency-waits"), [{"why": f"head {head['id']}"}])
        self.approve(105, {"E.lean": "e\n"})
        self.assertEqual(self.emergency(105)["behind"], 104)
        self.assertEqual(self.emergency(105)["reason"], "already-requested")
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.head()["id"], batch["id"], "the emergency batch seals before the open batch")
        self.assertEqual(self.controller.store.row("batches", {"id": successor["id"]})["state"], "open")
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "D.lean"), "d\n")
        self.assertEqual(self.controller.input_for(PROJECT, 104)["state"], "landed")
        self.assertEqual(self.journal("emergency-landed")[0]["members"], [104])
        # The second request is served next, still ahead of the ordinary open batch.
        second = self.emergency_batches()[-1]
        self.assertEqual([m["number"] for m in self.members(second)], [105])
        self.assertEqual(self.head()["id"], second["id"])
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.head()["id"], successor["id"])
        self.assertEqual(
            [c for c, _ in self.github.calls].count("fast_forward"), 3, "every landing went through the queue"
        )

    def test_an_inserted_pr_is_built_alone_lands_once_and_its_old_slot_never_acts(self):
        head, successor = self.running_head_with_successor()
        self.emergency(103)
        self.tick()
        [batch] = self.emergency_batches()
        row = self.controller.input_for(PROJECT, 103)
        self.assertEqual((row["state"], row["batch_id"]), ("inserted", batch["id"]))
        slot = next(m for m in self.members(successor) if m["number"] == 103)
        self.assertEqual(
            json.loads(row["evidence"])["emergency"]["displaced"],
            {"batch_id": successor["id"], "member_id": slot["id"]},
        )
        self.assertEqual(self.controller.store.row("batches", {"id": successor["id"]})["urgent"], 0)
        self.assertEqual(self.outcomes(103), [(batch["id"], "waiting")], "the owning batch reports the PR")
        self.run_ci()
        self.tick(2)
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.repo.file_at("main", "C.lean"), "c\n")
        self.assertEqual(self.controller.input_for(PROJECT, 103)["landed_sha"], self.members(batch)[0]["commit_sha"])
        self.assertEqual(self.outcomes(103)[0], (batch["id"], "ok"))
        # The open batch lands next; the displaced slot is empty on the new main and is not landed again.
        self.assertEqual(self.head()["id"], successor["id"])
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head())
        self.assertEqual(self.repo.file_at("main", "B.lean"), "b\n")
        self.assertEqual(len(self.github.comments_for(103)), 1, "one landing comment")
        self.assertEqual(self.controller.status()["counters"]["prs_landed"], 3)
        self.assertEqual(self.controller.input_for(PROJECT, 103)["landed_sha"], self.members(batch)[0]["commit_sha"])

    def test_cancel_returns_a_displaced_pr_to_its_batch_and_a_waiting_one_to_its_age(self):
        head, successor = self.running_head_with_successor()
        self.emergency(103)
        self.tick()
        [batch] = self.emergency_batches()
        self.assertTrue(self.controller.cancel_emergency(PROJECT, 103, "admin-worker", surface="test")["cancelled"])
        row = self.controller.input_for(PROJECT, 103)
        self.assertEqual((row["state"], row["batch_id"], row["urgent"]), ("inserted", successor["id"], 1))
        self.assertNotIn("emergency", json.loads(row["evidence"]))
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "removed")
        self.assertEqual(self.journal("emergency-ended")[-1]["cause"], "cancelled")
        self.assertEqual(self.outcomes(103)[0], (successor["id"], "waiting"))
        self.tick()
        self.assertEqual(len(self.emergency_batches()), 1, "the request is not rebuilt")
        self.assertIn("did not land this PR", self.github.comments_for(103)[0]["body"])
        # Without a displaced slot the PR waits again at its original age.
        self.approve(104, {"D.lean": "d\n"})
        approved_at = self.controller.input_for(PROJECT, 104)["approved_at"]
        self.emergency(104)
        self.tick()
        self.assertEqual(self.controller.input_for(PROJECT, 104)["state"], "inserted")
        self.controller.cancel_emergency(PROJECT, 104, "admin-worker", surface="test")
        row = self.controller.input_for(PROJECT, 104)
        self.assertEqual((row["state"], row["approved_at"], row["urgent"]), ("waiting", approved_at, 1))
        self.assertEqual(
            self.controller.cancel_emergency(PROJECT, 104, "admin-worker", surface="test")["reason"], "not-requested"
        )

    def test_adoption_emptying_returns_ownership_and_the_empty_batch_is_never_dispatched(self):
        head, successor = self.running_head_with_successor()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.head()["id"], successor["id"])
        self.approve(104, {"D.lean": "d\n"})
        self.tick()
        later = self.open_batch()
        self.emergency(104)
        self.github.ci_policy = fail_members(104)
        self.tick()
        [batch] = self.emergency_batches()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.head()["id"], batch["id"])
        dispatched = sum(d["client_payload"]["batch_id"] == str(batch["id"]) for d in self.github.dispatches)
        self.run_ci()
        self.tick()
        self.assertEqual(self.controller.status()["head"]["stage"], "repair")
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#104": Resolution(empty=True, explanation="Cannot build on its own.")}
        )
        self.assertTrue(agent.run(self.controller).accepted)
        row = self.controller.input_for(PROJECT, 104)
        self.assertEqual((row["state"], row["batch_id"]), ("inserted", later["id"]), "back in its batch")
        self.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "removed")
        self.assertEqual(self.journal("emergency-ended")[-1]["cause"], "emptied")
        mine = [d for d in self.github.dispatches if d["client_payload"]["batch_id"] == str(batch["id"])]
        self.assertEqual(len(mine), dispatched, "the emptied batch is never dispatched again")
        self.assertEqual(self.head()["id"], later["id"])
        self.assertNotIn(104, self.github.closed)

    def test_withdrawal_without_a_displaced_slot_cancels(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.github.ci_policy = fail_members(101)
        self.tick()
        [batch] = self.emergency_batches()
        self.assertEqual(self.head()["id"], batch["id"], "no head: it seals at once")
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution(empty=True, explanation="Cannot build on its own.")}
        )
        self.assertTrue(agent.run(self.controller).accepted)
        row = self.controller.input_for(PROJECT, 101)
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "empty-commit"))
        self.tick()
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "removed")
        self.assertIsNone(self.head())

    def test_emptied_member_at_landing_returns_to_its_slot_without_rejection(self):
        self.controller.set_setting("withdraw_emptied_at_adoption", False, "t")
        head, successor = self.running_head_with_successor()
        self.emergency(103)
        self.tick()
        [batch] = self.emergency_batches()
        self.run_ci()
        self.github.ci_policy = fail_members(103)
        self.tick(2)
        self.assertEqual(self.head()["id"], batch["id"])
        self.run_ci()
        self.tick()
        agent = self.agent(script=lambda task: {f"{PROJECT}#103": Resolution(empty=True, explanation="Dropped.")})
        self.assertTrue(agent.run(self.controller).accepted)
        self.github.ci_policy = verdicts()
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "landed")
        row = self.controller.input_for(PROJECT, 103)
        self.assertEqual((row["state"], row["batch_id"]), ("inserted", successor["id"]))
        self.assertNotIn(103, self.github.closed)
        self.assertEqual(self.controller.status()["counters"].get("prs_rejected", 0), 0)
        self.assertFalse(any("rejected" in c["body"] for c in self.github.comments_for(103)))

    def test_emptied_member_at_landing_without_a_slot_is_rejected(self):
        self.controller.set_setting("withdraw_emptied_at_adoption", False, "t")
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.github.ci_policy = fail_members(101)
        self.tick()
        self.run_ci()
        self.tick()
        agent = self.agent(script=lambda task: {f"{PROJECT}#101": Resolution(empty=True, explanation="Dropped.")})
        self.assertTrue(agent.run(self.controller).accepted)
        self.github.ci_policy = verdicts()
        self.tick()
        self.run_ci()
        self.tick(2)
        self.assertEqual(self.controller.input_for(PROJECT, 101)["reason"], "empty-commit")
        self.assertEqual(self.github.closed[101], "Change 101")

    def test_cancel_supersedes_tasks_releases_claims_and_is_refused_once_landing(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.github.ci_policy = fail_members(101)
        self.tick()
        [batch] = self.emergency_batches()
        self.run_ci()
        self.tick()
        task = self.agent().claim(self.controller)
        self.assertIsNotNone(task)
        self.controller.cancel_emergency(PROJECT, 101, "admin-worker", surface="test")
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "superseded")
        self.assertEqual(self.controller.store.count("claims", {"batch_id": batch["id"], "released_at": None}), 0)
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "waiting")
        # A batch whose promotion is queued cannot be cancelled.
        self.github.ci_policy = verdicts()
        self.emergency(101)
        self.github.fail_next["fast_forward"] = 10
        self.tick()
        second = self.emergency_batches()[-1]
        self.run_ci()
        self.tick()
        self.assertTrue(self.controller.store.rows("outbox", {"kind": "promote", "state": "pending"}))
        refused = self.controller.cancel_emergency(PROJECT, 101, "admin-worker", surface="test")
        self.assertEqual(refused["reason"], "promotion-pending")
        self.assertEqual(self.controller.store.row("batches", {"id": second["id"]})["state"], "head")
        # A removed batch is never promoted, even by an entry queued for it.
        self.controller.store.update("batches", {"id": second["id"]}, state="removed")
        payload = json.loads(self.controller.store.rows("outbox", {"kind": "promote"})[-1]["payload"])
        self.assertEqual(self.controller._perform_promotion(payload, self.clock()), "not-head")

    def test_an_input_reserved_by_an_ordinary_probe_never_makes_an_emergency_batch(self):
        fetch = self.controller._fetch_head
        requested = []

        def fetch_then_request(number, sha):
            if number == 102 and not requested:
                requested.append(self.emergency(102))
            return fetch(number, sha)

        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.approve(103, {"C.lean": "c\n"})
        with patch.object(self.controller, "_fetch_head", side_effect=fetch_then_request):
            self.tick()
        self.assertTrue(requested[0]["accepted"])
        ordinary = self.open_batch()
        self.assertIn(102, [m["number"] for m in self.members(ordinary)])
        self.assertEqual(self.controller.store.row("batches", {"id": ordinary["id"]})["urgent"], 1)
        self.assertEqual(self.controller.input_for(PROJECT, 102)["urgent"], 2)
        self.tick()
        [batch] = self.emergency_batches()
        self.assertEqual([m["number"] for m in self.members(batch)], [102])
        self.assertTrue(self.controller.mark_urgent(PROJECT, 102, "operator")["urgent"])
        self.assertEqual(self.controller.input_for(PROJECT, 102)["urgent"], 2, "mark_urgent never lowers 2")
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["urgent"], 2)

    def test_cancel_during_its_own_probe_and_transient_probe_failures(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.emergency(102)
        fetch = self.controller._fetch_head

        def cancel_then_fetch(number, sha):
            if number == 102:
                self.controller.cancel_emergency(PROJECT, 102, "admin-worker", surface="test")
            return fetch(number, sha)

        with patch.object(self.controller, "_fetch_head", side_effect=cancel_then_fetch):
            self.tick()
        row = self.controller.input_for(PROJECT, 102)
        self.assertEqual((row["state"], row["urgent"]), ("waiting", 1))
        self.assertNotIn("emergency", json.loads(row["evidence"]))
        self.assertEqual([b["state"] for b in self.emergency_batches()], ["removed"])
        self.tick()
        self.assertEqual(len(self.emergency_batches()), 1, "the cancelled request is not resurrected")
        # A transient probe failure ends the allocation but keeps the request, which the next pass serves.
        self.approve(103, {"C.lean": "c\n"})
        self.emergency(103)
        with patch.object(self.controller, "_fetch_head", return_value=False):
            self.tick()
        self.assertEqual(self.journal("emergency-ended")[-1]["cause"], "empty-allocation")
        self.assertEqual(self.controller.input_for(PROJECT, 103)["urgent"], 2)
        self.tick()
        live = self.controller._emergency_batch()
        self.assertEqual([m["number"] for m in self.members(live)], [103])

    def test_crash_after_allocation_and_a_probe_refusal(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"B.lean": "b\n"})
        self.emergency(102)
        self.controller.store.insert(
            "batches", repository=PROJECT, base=self.repo.main_sha(), tip=self.repo.main_sha(), state="open",
            created_at=self.clock(), urgent=2,
        )  # fmt: skip
        self.tick()
        self.assertEqual(self.journal("emergency-ended")[0]["cause"], "empty-allocation")
        live = self.controller._emergency_batch()
        self.assertEqual([m["number"] for m in self.members(live)], [102])
        # A conflict with main refuses the request (and cancels the input as any probe would).
        self.approve(103, {"A.lean": "other\n"}, start=None)
        self.repo.advance_main({"A.lean": "main\n"})
        self.emergency(103)
        self.controller.cancel_emergency(PROJECT, 102, "admin-worker", surface="test")
        self.tick()
        self.assertIn("conflicts-with-main", [d["reason"] for d in self.journal("emergency-refused")])

    def test_waiting_emergency_batch_supplies_a_module_ordinary_prs_wait_for(self):
        self.approve(101, {"A.lean": "a\n"})
        self.tick()
        self.approve(102, {"TauCeti/New.lean": "module\ndef new := 1\n"})
        self.emergency(102)
        self.tick()
        self.approve(103, {"TauCeti/User.lean": "module\npublic import TauCeti.New\ndef user := new\n"})
        self.tick()
        row = self.controller.input_for(PROJECT, 103)
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "missing-source-in-flight"))

    def test_an_inert_emergency_batch_is_not_sealed_and_ends(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        with patch.object(self.controller, "_mixed_inert_batch", side_effect=lambda b: bool(b and b["urgent"] == 2)):
            self.tick()
            self.assertIsNone(self.head(), "not selected by _seal")
            self.tick()
        self.assertIsNone(self.head())
        self.assertIn("inert", [d["cause"] for d in self.journal("emergency-ended")])

    def test_dispatches_on_an_unpublished_base(self):
        self.controller.revision_map_probe = lambda url: False
        self.approve(101, {"TauCeti/A.lean": "a\n"})
        self.emergency(101)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertEqual(len(self.journal("dispatch-unpublished-base")), 1)

    def test_admin_reapproval_is_recorded_beside_the_original_evidence(self):
        head = self.github.create_pr(101, {"A.lean": "a\n"})
        original = {"kind": "review_app", "actor_login": "review-bot", "actor_id": 7, "admin": False}
        self.controller.approve(PROJECT, 101, head, "review-bot", evidence=original)
        approved_at = self.controller.input_for(PROJECT, 101)["approved_at"]
        admin = {"kind": "issue_comment", "actor_login": "boss", "actor_id": 9, "admin": True}
        admin |= {"merge_base_sha": "m" * 40, "head_sha": head, "comment_id": 5, "command": "bors r+"}
        self.controller.approve(PROJECT, 101, head, "boss", evidence=admin)
        row = self.controller.input_for(PROJECT, 101)
        evidence = json.loads(row["evidence"])
        self.assertEqual({k: evidence[k] for k in original}, original, "the original evidence is untouched")
        self.assertEqual((evidence["admin_reapproval"]["actor_id"], row["approved_at"]), (9, approved_at))
        source = feeds.ControllerFeedSource(self.controller)
        self.assertEqual(
            source.scope_approvals(PROJECT), [{"pr": 101, "actor_id": 9, "head_sha": head, "merge_base_sha": "m" * 40}]
        )
        self.tick()
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "inserted")
        self.controller.approve(PROJECT, 101, head, "boss", evidence=dict(admin, actor_id=11))
        evidence = json.loads(self.controller.input_for(PROJECT, 101)["evidence"])
        self.assertEqual(evidence["admin_reapproval"]["actor_id"], 11, "an inserted input is re-approved too")
        [view] = [m for b in source.active_batches(PROJECT, "main") for m in b.members]
        self.assertEqual(view.scope_approval["actor_id"], 11)
        self.assertEqual(len(self.journal("admin-reapproved")), 2)

    def test_successor_claimed_pre_rebase_survives_the_emergency_head_and_is_carried(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        successor = self.open_batch()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"}, explanation="Merged."
                )
            }
        )
        task = agent.claim(self.controller)
        self.assertEqual(task.kind, "pre_rebase")
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.run_ci()
        self.tick(2)
        batch = self.controller._emergency_batch()
        self.assertEqual(self.head()["id"], batch["id"])
        self.assertEqual(self.journal("emergency-preserved"), [{"tasks": [task.task_id]}])
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "open")
        adoption = self.controller.submit_task(task.task_id, task.claim_token, agent.produce(task))
        self.assertTrue(adoption.accepted, adoption)
        self.tick()
        carried = self.controller.store.row("batches", {"id": successor["id"]})
        self.assertEqual((carried["pre_rebase_state"], carried["pre_rebase_onto"]), ("adopted", self.head()["tip"]))
        self.assertIn(f"{PROJECT}#102", json.loads(carried["pre_rebase_edits"]))

    def test_a_github_singleton_is_not_held_to_the_source_candidate_scope(self):
        head, successor = self.running_head_with_successor()
        self.emergency(103)
        owner = self.controller.store.row("batches", {"id": successor["id"]})
        mixed = types.SimpleNamespace(mode="admit", primary="github", supports_candidate=lambda base, tip: False)

        def singleton():
            nonlocal row
            row = self.controller.store.row("inputs", {"id": row["id"]})
            with self.controller._lock:
                return self.controller._emergency_singleton(row, owner, self.clock())

        row = self.controller.input_for(PROJECT, 103)
        with patch.object(self.controller, "mixed", mixed), patch.object(self.controller, "mixed_ci", object()):
            mixed.primary = "forgejo"  # source-candidate CI would run it: the scope applies
            self.assertIsNone(singleton())
        self.assertEqual(self.journal("emergency-refused")[-1]["reason"], "source-ci-scope")
        self.emergency(103)
        row = self.controller.input_for(PROJECT, 103)
        mixed.primary = "github"  # GitHub CI runs it: no source-candidate scope, as for an ordinary probe
        with patch.object(self.controller, "mixed", mixed), patch.object(self.controller, "mixed_ci", object()):
            batch = singleton()
        self.assertEqual([m["number"] for m in self.members(batch)], [103])

    def test_an_empty_allocation_leaves_successor_work_onto_main_alone(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        task = self.agent().claim(self.controller)
        self.assertEqual(task.kind, "pre_rebase")
        self.run_ci()
        self.tick(2)
        self.assertIsNone(self.head(), "the claimed successor waits")
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["onto"], self.repo.main_sha())
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        with patch.object(self.controller, "_fetch_head", return_value=False):
            self.tick()
        self.assertEqual(self.journal("emergency-ended")[-1]["cause"], "empty-allocation")
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "open")
        self.assertEqual(self.controller.store.count("claims", {"task_id": task.task_id, "released_at": None}), 1)

    def test_an_emergency_landing_cancels_its_other_attempts_durably(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.github.ci_policy = fail_members(101)
        self.tick()
        [batch] = self.emergency_batches()
        [first] = self.controller.store.rows("ci_attempts", {"batch_id": batch["id"]})
        stray = self.github.runs[-1]
        self.run_ci()
        self.tick()
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "fixed\n"}, explanation="Fixed.")}
        )
        self.assertTrue(agent.run(self.controller).accepted)
        self.github.ci_policy = verdicts()
        self.controller.store.update(
            "ci_attempts",
            {"id": first["id"]},
            outcome="superseded",
            run_id=None,
            run_status="",
            dispatch_state="uncertain",
        )
        self.tick()
        second = self.controller.store.rows("ci_attempts", {"batch_id": batch["id"]}, order="id")[-1]
        self.assertNotEqual(second["id"], first["id"])
        self.run_ci()
        stray.status, stray.conclusion = "in_progress", None  # the superseded dispatch's run is still live
        self.tick(2)
        self.assertEqual(self.controller.store.row("batches", {"id": batch["id"]})["state"], "landed")
        self.assertEqual((stray.status, stray.conclusion), ("completed", "cancelled"), "found and cancelled")
        self.assertEqual(self.controller.status()["emergency"]["cancel_intents"], [])
        self.assertEqual(self.controller.store.row("ci_attempts", {"id": second["id"]})["cancel_result"], "")

    def test_cancel_intents_survive_lookup_failures_and_uncertain_dispatches(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.tick()
        [batch] = self.emergency_batches()
        [attempt] = self.controller.store.rows("ci_attempts", {"batch_id": batch["id"]})
        self.controller.store.update(
            "ci_attempts", {"id": attempt["id"]}, run_id=None, dispatch_state="uncertain", run_status=""
        )
        run = self.github.runs[-1]
        run.vanished = True
        self.controller.cancel_emergency(PROJECT, 101, "admin-worker", surface="test")
        self.assertEqual(
            [i["attempt_id"] for i in self.controller.status()["emergency"]["cancel_intents"]], [attempt["id"]]
        )
        self.clock.advance(3600)
        self.tick()
        self.assertTrue(self.controller.status()["emergency"]["cancel_intents"], "never retired by age")
        run.vanished = False
        self.github.fail_next["find_candidate_run"] = 1
        self.tick()
        self.assertEqual(run.status, "queued", "a failed lookup is retried after its backoff")
        self.tick()
        self.assertEqual(run.status, "queued", "the retry waits out its backoff")
        self.clock.advance(self.controller.setting("outbox_retry_seconds"))
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        restarted.tick()
        self.assertEqual((run.status, run.conclusion), ("completed", "cancelled"))
        self.assertEqual(restarted.status()["emergency"]["cancel_intents"], [])

    def test_ending_after_an_adoption_cancels_the_previous_versions_run(self):
        self.approve(101, {"A.lean": "a\n"})
        self.emergency(101)
        self.github.ci_policy = fail_members(101)
        self.tick()
        [batch] = self.emergency_batches()
        self.run_ci()
        self.tick()
        self.github.ci_policy = verdicts()
        self.controller.store.update(
            "ci_attempts", {"batch_id": batch["id"]}, outcome="pending", run_status="in_progress"
        )
        self.github.runs[-1].status, self.github.runs[-1].conclusion = "in_progress", None
        agent = self.agent(
            script=lambda task: {f"{PROJECT}#101": Resolution({"A.lean": "fixed\n"}, explanation="Fixed.")}
        )
        self.assertTrue(agent.run(self.controller).accepted)
        self.assertIsNone(self.controller._current_attempt(self.controller._emergency_batch()))
        self.controller.cancel_emergency(PROJECT, 101, "admin-worker", surface="test")
        self.assertEqual((self.github.runs[-1].status, self.github.runs[-1].conclusion), ("completed", "cancelled"))


BUILD_STEP = "sandboxed-build: Build exact candidate under bwrap with workflow-pinned audits, offline"
HANDOFF_STEP = "sandboxed-build: Hand the merge-group oleans to the isolated publisher"


class EmergencyEviction(QueueFixture):
    """Slice 2 of the emergency lane: the emergency batch takes the head from a running ordinary head,
    unless that head is about to land or publishing its final oleans."""

    def setUp(self):
        super().setUp()
        self.controller.set_setting("emergency_enabled", True, "t")
        self.controller.set_setting("emergency_evict", True, "t")

    def emergency(self, number, actor="admin-worker"):
        return self.controller.request_emergency(PROJECT, number, actor, surface="test")

    def journal(self, kind):
        return [e["detail"] for e in self.controller.store.journal_entries(kind)]

    def batch(self, batch_id):
        return self.controller.store.row("batches", {"id": batch_id})

    def attempts(self, batch_id):
        return self.controller.store.rows("ci_attempts", {"batch_id": batch_id}, order="id")

    def running_head(self, *numbers):
        """A head (101, or `numbers`) whose run is building, and an open batch (102, 103) behind it."""
        for number in numbers or (101,):
            self.approve(number, {f"F{number}.lean": f"{number}\n"})
        self.tick()
        head = self.head()
        self.approve(102 if 102 not in numbers else 112, {"B.lean": "b\n"})
        self.approve(103 if 103 not in numbers else 113, {"C.lean": "c\n"})
        self.tick()
        self.github.start_runs()
        self.clock.advance(60)
        self.github.progress_runs("sandboxed-build", "building")
        return head, self.open_batch()

    def run_of(self, batch_id):
        return [run for run in self.github.runs if run.batch_id == str(batch_id)][-1]

    def set_phases(self, run, *, built=True, publisher=None, conclusion="success"):
        """The run's phases: the build step (concluded `success` when `built`), then a publisher that is
        `queued` (waiting for a runner), `started`, or `completed` with `conclusion`."""
        now = self.clock()
        phases = [
            CandidatePhase("sandboxed-build", now - 600, None, "in_progress"),
            CandidatePhase(
                BUILD_STEP, now - 600, now - 60 if built else None, "completed" if built else "in_progress",
                "success" if built else "",
            ),
        ]  # fmt: skip
        if publisher == "queued":
            phases.append(CandidatePhase(f"{CANDIDATE_PUBLICATION_JOB} / publish", None, None, "queued"))
        elif publisher == "started":
            phases.append(CandidatePhase(HANDOFF_STEP, now - 30, now - 20, "completed", "success"))
            phases.append(CandidatePhase(f"{CANDIDATE_PUBLICATION_JOB} / publish", now - 10, None, "in_progress"))
        elif publisher == "completed":
            phases.append(
                CandidatePhase(f"{CANDIDATE_PUBLICATION_JOB} / publish", now - 10, now, "completed", conclusion)
            )
        run.status, run.updated_at, run.phases = "in_progress", now, phases

    def land_all(self, rounds=6):
        for _ in range(rounds):
            self.run_ci()
            self.tick(2)
            if self.head() is None:
                return

    def test_publication_phases_are_pinned(self):
        self.assertTrue(BUILD_STEP.split(": ", 1)[1].startswith(CANDIDATE_BUILD_STEP))
        self.assertTrue(HANDOFF_STEP.split(": ", 1)[1].startswith(CANDIDATE_HANDOFF_STEP))
        build = {"name": BUILD_STEP, "started_at": 1, "completed_at": 2, "status": "completed", "detail": "success"}
        job = {"name": f"pr-build / {CANDIDATE_PUBLICATION_JOB} / publish", "started_at": 3, "status": "in_progress"}
        queued = dict(job, started_at=None, status="queued")
        failed = dict(job, completed_at=4, status="completed", detail="failure")
        self.assertTrue(publishing([build, job]))
        prefixed = dict(build, name=f"pr-build / {BUILD_STEP}")  # a stub caller of a reusable pr-build
        self.assertTrue(publishing([prefixed, dict(job, name=f"pr-build / {HANDOFF_STEP}")]))
        self.assertFalse(publishing([build, queued]), "a publisher waiting for a runner has not started")
        self.assertFalse(publishing([build, failed]))
        self.assertFalse(publishing([dict(build, detail="failure"), job]))
        self.assertFalse(publishing([job]))

    def test_evicts_a_building_head_and_cancels_its_run_then_the_head_returns_and_lands(self):
        head, successor = self.running_head()
        run = self.run_of(head["id"])
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        emergency = self.controller._emergency_batch()
        self.assertEqual(self.head()["id"], emergency["id"])
        evicted = self.batch(head["id"])
        self.assertEqual((evicted["state"], evicted["sealed"], evicted["version"]), ("open", 1, head["version"]))
        self.assertEqual(evicted["prepared_version"], head["version"])
        self.assertEqual((run.status, run.conclusion), ("completed", "cancelled"))
        [attempt] = self.attempts(head["id"])
        self.assertEqual(attempt["outcome"], "superseded")
        [entry] = self.journal("evicted")
        self.assertEqual((entry["by"], entry["why"], entry["held"]), (emergency["id"], "run in_progress", False))
        self.assertEqual(self.controller.status()["emergency"]["cancel_intents"], [])
        self.land_all()
        self.assertEqual([self.repo.file_at("main", f) for f in ("D.lean", "F101.lean")], ["d\n", "101\n"])
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "landed")
        self.assertEqual(self.batch(head["id"])["state"], "landed")
        self.assertIsNone(self.controller._state(f"emergency_evicted:{head['id']}"), "the marker is cleared")
        fast_forwards = [c for c, _ in self.github.calls].count("fast_forward")
        self.assertEqual(fast_forwards, 3, "every landing went through the queue")

    def test_a_queued_publisher_is_evicted_but_a_started_one_waits_until_it_fails(self):
        head, _ = self.running_head()
        run = self.run_of(head["id"])
        self.set_phases(run, publisher="started")
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.assertEqual(self.head()["id"], head["id"], "a started publisher is protected")
        self.assertEqual(run.status, "in_progress")
        self.assertIn("publishing its final oleans", self.journal("emergency-waits")[-1]["why"])
        reads = len([c for c, _ in self.github.calls if c == "find_candidate_run"])
        self.tick()
        self.assertEqual(
            len([c for c, _ in self.github.calls if c == "find_candidate_run"]), reads, "no read on every pass"
        )
        self.set_phases(run, publisher="completed", conclusion="failure")
        self.clock.advance(self.controller.setting("polling_interval_seconds"))
        self.tick()
        self.assertEqual(self.head()["urgent"], 2, "a failed publication is no longer protected")
        self.assertEqual(run.conclusion, "cancelled")
        # A publisher still waiting for a runner has not started: evicted at once.
        self.land_all()
        self.approve(105, {"E.lean": "e\n"})
        self.tick()
        second = self.head()
        self.github.start_runs()
        self.set_phases(self.run_of(second["id"]), publisher="queued")
        self.approve(106, {"G.lean": "g\n"})
        self.emergency(106)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(self.batch(second["id"])["state"], "open")

    def test_a_pending_promotion_a_pass_and_a_successful_run_are_protected(self):
        head, _ = self.running_head()
        run = self.run_of(head["id"])
        # P3: the run completed `success` and its verdicts are still being bound.
        run.status, run.conclusion, run.completed_at = "completed", "success", self.clock()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.assertEqual(self.head()["id"], head["id"])
        self.assertIn("verdict is being bound", self.journal("emergency-waits")[-1]["why"])
        # P1 and P2: it passed and its promotion is queued (and here keeps failing).
        self.github.fail_next["fast_forward"] = 10
        for context in ("build", "bump-guard", "scope"):
            self.github.statuses[(run.head_sha, context)] = Status(
                "success", "ok", run.to_candidate_run().html_url, self.clock(), run.run_id
            )
        self.clock.advance(self.controller.setting("polling_interval_seconds"))
        self.tick()
        self.assertEqual(self.head()["id"], head["id"])
        self.assertEqual(self.attempts(head["id"])[-1]["outcome"], "pass")
        self.assertTrue(self.controller.store.rows("outbox", {"kind": "promote", "state": "pending"}))
        self.tick()
        self.assertEqual(self.head()["id"], head["id"])
        self.assertIn(self.journal("emergency-waits")[-1]["why"], (
            f"head {head['id']} (its promotion is pending)", f"head {head['id']} (it passed and is about to land)",
        ))  # fmt: skip
        self.assertEqual(self.journal("evicted"), [])

    def test_a_stale_pending_attempt_refreshed_as_a_failed_publication_is_evicted(self):
        head, _ = self.running_head()
        run = self.run_of(head["id"])
        self.set_phases(run, publisher="started")
        self.tick()  # observed publishing
        self.set_phases(run, publisher="completed", conclusion="failure")
        run.status, run.conclusion, run.completed_at = "completed", "failure", self.clock()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.clock.advance(self.controller.setting("polling_interval_seconds"))
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(self.journal("evicted")[0]["why"], "run completed")

    def test_strict_observation_failure_does_not_evict_and_a_prepared_attempt_is_not_sent(self):
        head, _ = self.running_head()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.github.fail_next["find_candidate_run"] = 1
        self.tick()
        self.assertEqual(self.head()["id"], head["id"])
        self.assertIn("could not be read", self.journal("emergency-waits")[-1]["why"])
        [attempt] = self.attempts(head["id"])
        self.controller.store.update(
            "ci_attempts", {"id": attempt["id"]}, dispatch_state="prepared", run_id=None, run_status=""
        )
        dispatches = len(self.github.dispatches)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(self.journal("evicted")[0]["why"], "dispatch prepared")
        mine = [d for d in self.github.dispatches[dispatches:] if d["client_payload"]["batch_id"] == str(head["id"])]
        self.assertEqual(mine, [], "the eviction check never sends a prepared attempt")

    def test_a_promotion_refusal_hold_is_evicted_and_the_head_returns_to_it(self):
        head, _ = self.running_head()
        [attempt] = self.attempts(head["id"])
        hold = f"source-ci:promotion-hold:{attempt['id']}"
        self.controller.store.set_setting(hold, {"kind": "PromotionRefused"})
        self.controller._enqueue("promote", f"promote:{head['id']}:{head['version']}:{head['tip']}", {})
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2, "a hold is never protection, even with a promote entry")
        kept = self.controller.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(kept["outcome"], "pending", "its attempt is kept")
        self.assertIsNone(self.controller._state(f"cancel_intent:{attempt['id']}"))
        self.assertTrue(self.journal("evicted")[0]["held"])
        self.controller.cancel_emergency(PROJECT, 104, "admin-worker", surface="test")
        self.tick()
        self.assertEqual(self.head()["id"], head["id"], "E ending returns the evicted head at once")
        self.assertEqual(self.controller._current_attempt(self.head())["id"], attempt["id"], "back to its hold")
        self.assertIsNotNone(self.controller.store.get_setting(hold))

    def test_a_silent_started_publisher_gets_no_lost_run_verdict(self):
        self.controller.set_setting("emergency_evict", False, "t")
        head, _ = self.running_head()
        run = self.run_of(head["id"])
        self.set_phases(run, publisher="started")
        self.tick()
        run.updated_at = self.clock()
        self.clock.advance(self.controller.setting("lost_run_seconds") + 60)
        reasons = self.tick()
        self.assertEqual(self.attempts(head["id"])[-1]["outcome"], "pending")
        self.assertTrue(any("publishing its final oleans, not treated as lost" in r for r in reasons), reasons)
        self.assertEqual(run.status, "in_progress")

    def test_crash_after_the_eviction_transaction_cancels_on_restart(self):
        head, _ = self.running_head()
        run = self.run_of(head["id"])
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        with patch.object(self.controller, "_settle_cancel_intents"):
            self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(run.status, "in_progress", "the crash came before the cancel")
        restarted = reopen_controller(self.root, self.github, self.repo, self.clock)
        self.addCleanup(restarted.store.close)
        restarted.recover()
        restarted.tick()
        self.assertEqual((run.status, run.conclusion), ("completed", "cancelled"))

    def test_displacing_the_heads_own_pr_moves_ownership_only_with_the_swap(self):
        head, successor = self.running_head(101, 102)
        run = self.run_of(head["id"])
        self.set_phases(run, publisher="started")
        self.emergency(102)
        self.tick()
        self.assertIsNone(self.controller._emergency_batch(), "nothing is allocated while the head is protected")
        row = self.controller.input_for(PROJECT, 102)
        self.assertEqual((row["state"], row["batch_id"]), ("inserted", head["id"]))
        self.set_phases(run)
        self.clock.advance(self.controller.setting("polling_interval_seconds"))
        self.tick()
        emergency = self.head()
        self.assertEqual((emergency["urgent"], [m["number"] for m in self.members(emergency)]), (2, [102]))
        row = self.controller.input_for(PROJECT, 102)
        self.assertEqual(row["batch_id"], emergency["id"])
        self.assertEqual(json.loads(row["evidence"])["emergency"]["displaced"]["batch_id"], head["id"])
        self.assertEqual(self.batch(head["id"])["state"], "open")
        self.land_all()
        self.assertEqual(
            self.controller.input_for(PROJECT, 102)["landed_sha"], self.members(emergency)[0]["commit_sha"]
        )
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "landed")
        self.assertEqual(len(self.github.comments_for(102)), 1, "one landing comment")

    def test_the_evicted_heads_retained_repair_is_adopted_and_e_keeps_its_head_slot(self):
        self.controller.set_setting("repair_concurrency", 1, "t")
        self.github.ci_policy = fail_members(101)
        head, _ = self.running_head()
        self.run_ci()
        self.tick()
        [repair] = self.controller.store.rows("tasks", {"batch_id": head["id"], "state": "open"})
        retained = self.agent(
            "retained",
            script=lambda task: {f"{PROJECT}#101": Resolution({"F101.lean": "fixed\n"}, explanation="Fixed.")},
        )
        task = retained.claim(self.controller)
        self.assertEqual(task.task_id, repair["id"])
        self.github.ci_policy = fail_members(101, 104)
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        emergency = self.head()
        self.assertEqual(emergency["urgent"], 2, "a source failure under repair is evicted")
        self.assertEqual(self.journal("evicted")[0]["tasks"], [repair["id"]])
        self.run_ci()
        self.tick()
        [head_task] = self.controller.store.rows("tasks", {"batch_id": emergency["id"], "state": "open"})
        second = self.agent("second", login="second-admin").claim(self.controller)
        self.assertIsNotNone(second, "E's repair is claimable beside the retained claim")
        self.assertEqual(second.task_id, head_task["id"])
        adoption = self.controller.submit_task(task.task_id, task.claim_token, retained.produce(task))
        self.assertTrue(adoption.accepted, adoption)
        evicted = self.batch(head["id"])
        self.assertEqual((evicted["state"], evicted["prepared_version"]), ("open", evicted["version"]))
        self.tick()
        kinds = {t["kind"] for t in self.controller.store.rows("tasks", {"batch_id": head["id"], "state": "open"})}
        self.assertNotIn("prepare", kinds, "own-base preparation is not repeated")

    def test_an_unclaimed_retained_repair_is_background_work(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.controller.set_setting("prepare_min_members", 1, "t")
        self.controller.set_setting("prepare_quiet_seconds", 0, "t")
        self.github.ci_policy = fail_members(101)
        head, _ = self.running_head()
        self.run_ci()
        self.tick()
        [repair] = self.controller.store.rows("tasks", {"batch_id": head["id"], "state": "open"})
        self.github.ci_policy = verdicts()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertTrue(self.controller.store.rows("tasks", {"kind": "prepare", "state": "open"}))
        task = self.agent().claim(self.controller)
        self.assertEqual(task.task_id, repair["id"], "still claimable while its batch waits, ahead of preparation")
        self.assertIsNone(
            self.agent("other", login="second-admin").claim(self.controller), "it takes the one background slot"
        )

    def test_ending_the_emergency_batch_returns_the_head_without_readopting_a_run_being_cancelled(self):
        head, _ = self.running_head()
        old = self.run_of(head["id"])
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        with patch.object(self.controller, "_settle_cancel_intents"):
            # The cancel intent stays unsettled (a crash, a lookup or cancel still being retried).
            self.tick()
            [attempt] = self.attempts(head["id"])
            self.assertIsNotNone(self.controller._state(f"cancel_intent:{attempt['id']}"))
            self.controller.cancel_emergency(PROJECT, 104, "admin-worker", surface="test")
            self.tick()
        self.assertEqual(self.head()["id"], head["id"], "E ending returns the evicted head at once")
        self.assertEqual(len(self.attempts(head["id"])), 2, "a new attempt instead of readopting the old run")
        self.assertEqual(self.journal("run-readopted"), [])
        self.tick()
        new = self.run_of(head["id"])
        self.assertNotEqual(new.run_id, old.run_id)
        self.assertEqual((old.status, old.conclusion), ("completed", "cancelled"))
        self.assertNotEqual(new.conclusion, "cancelled", "the intent cancels only the old run")
        self.land_all()
        self.assertEqual(self.controller.input_for(PROJECT, 101)["state"], "landed")

    def test_an_old_intent_without_a_run_never_cancels_the_returning_heads_new_run(self):
        head, _ = self.running_head()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        with patch.object(self.controller, "_settle_cancel_intents"):
            self.tick()
            [attempt] = self.attempts(head["id"])
            self.controller.store.update(
                "ci_attempts", {"id": attempt["id"]}, run_id=None, run_status="", dispatch_state="uncertain"
            )
            self.controller.cancel_emergency(PROJECT, 104, "admin-worker", surface="test")
            self.tick()
        self.assertEqual(len(self.attempts(head["id"])), 2)
        self.controller.store.update("ci_attempts", {"id": self.attempts(head["id"])[-1]["id"]}, run_id=None)
        self.tick()
        self.assertNotEqual(self.run_of(head["id"]).conclusion, "cancelled", "the new run is not taken")
        self.assertIsNone(self.controller._state(f"cancel_intent:{attempt['id']}"))

    def test_displacing_a_head_member_respects_the_sealing_barriers(self):
        head, _ = self.running_head(101, 102)
        self.controller._set_state("inference_hold", {"since": self.clock(), "last_error": "t"})
        self.emergency(102)
        self.tick()
        self.assertEqual(self.head()["id"], head["id"])
        self.assertIsNone(self.controller._emergency_batch())
        self.assertIn("inference hold", self.journal("emergency-waits")[-1]["why"])

    def test_zero_repair_concurrency_still_refuses_every_claim(self):
        self.controller.set_setting("repair_concurrency", 0, "t")
        self.github.ci_policy = fail_members(101)
        self.running_head()
        self.run_ci()
        self.tick()
        self.assertTrue(self.controller.store.rows("tasks", {"kind": "repair", "state": "open"}))
        self.assertIsNone(self.agent().claim(self.controller))

    def test_successor_claimed_pre_rebase_survives_eviction_and_is_carried_onto_the_returning_head(self):
        self.controller.set_setting("repair_concurrency", 2, "t")
        self.approve(101, {"Shared.lean": "line one\nline two changed by 101\nline three\n"})
        self.tick()
        head = self.head()
        self.approve(102, {"Shared.lean": "line one\nline two changed by 102\nline three\n"})
        self.tick()
        successor = self.open_batch()
        agent = self.agent(
            script=lambda task: {
                f"{PROJECT}#102": Resolution(
                    {"Shared.lean": "line one\nline two changed by 101 and 102\nline three\n"}, explanation="Merged."
                )
            }
        )
        task = agent.claim(self.controller)
        self.assertEqual((task.kind, task.onto), ("pre_rebase", head["tip"]))
        self.github.start_runs()
        self.approve(104, {"D.lean": "d\n"})
        self.emergency(104)
        self.tick()
        self.assertEqual(self.head()["urgent"], 2)
        self.assertEqual(self.controller.store.row("tasks", {"id": task.task_id})["state"], "open")
        adoption = self.controller.submit_task(task.task_id, task.claim_token, agent.produce(task))
        self.assertTrue(adoption.accepted, adoption)
        self.assertEqual(self.batch(successor["id"])["pre_rebase_onto"], head["tip"])
        self.land_all()
        self.assertEqual(
            self.repo.file_at("main", "Shared.lean"), "line one\nline two changed by 101 and 102\nline three\n"
        )
        self.assertEqual(self.controller.input_for(PROJECT, 102)["state"], "landed")


if __name__ == "__main__":
    unittest.main()

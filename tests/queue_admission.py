"""Primary intake with real controller/SQLite/git: safe decisions, fairness and crash recovery."""

import json
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import webhook
from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.fakes import make_controller, reopen_controller
from tauceti_queue.github_adapter import GitHubClient, RateLimited
from tauceti_queue.ports import Status

REPO = "TauCetiProject/TauCeti"


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller, self.github, self.repo, self.clock = make_controller(self.tmp.name)
        self.addCleanup(lambda: self.controller.store.close())
        self.config = AdmissionPolicy(rubrics=["correctness", "reuse"], prefix="TauCeti/", allow_files=[])
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)

    def review(self, number=1, *, state="green", round=1, comment=10):
        head = self.github.prs[number]["head_sha"]
        base = self.github.pr_merge_base(REPO, number, head)
        ts = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        previous = self.github.review_comments.get(number, [{}])[0]
        meta = {
            "head_sha": head,
            "merge_base_sha": base,
            "round": round,
            "ts": ts,
            "mode": "commit",
            "states": {r: state for r in self.config.rubrics},
        }
        self.github.review_comments[number] = [
            {
                "id": comment,
                "created_at": previous.get("created_at", ts) if previous.get("id") == comment else ts,
                "updated_at": ts,
                "body": "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->",
            }
        ]

    def ready(self, number=1):
        head = self.github.create_pr(number, {f"TauCeti/P{number}.lean": f"-- {number}\n"})
        self.review(number)
        for context in ("build", "scope"):
            self.github.statuses[(head, context)] = Status("success", "", "", self.clock(), None)
        return head

    def event(self, kind, payload, delivery=""):
        receiver = webhook.WebhookReceiver(self.controller, self.github, repositories=[REPO])
        return receiver.handle(webhook.Delivery(kind, delivery, {**payload, "repository": {"full_name": REPO}}))

    def restart(self):
        self.controller.store.close()
        self.controller = reopen_controller(self.tmp.name, self.github, self.repo, self.clock)
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)

    def test_primary_intake_uses_published_review_and_unchanged_inputs_cost_no_evaluation(self):
        self.ready()
        result = self.intake.run_once()
        self.assertEqual(result["approved"], 1)
        row = self.controller.inputs()[0]
        self.assertEqual(json.loads(row["evidence"])["kind"], "admission_policy")
        count = sum(call[0] == "admission_snapshot" for call in self.github.calls)
        self.clock.advance(1000)
        self.assertEqual(self.intake.run_once()["evaluated"], 0)
        self.assertEqual(sum(call[0] == "admission_snapshot" for call in self.github.calls), count)

    def test_build_change_without_pr_updated_at_creates_work(self):
        head = self.ready()
        self.github.statuses[(head, "build")] = Status("pending", "", "", self.clock(), None)
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.github.statuses[(head, "build")] = Status("success", "", "", self.clock(), None)
        self.event("status", {"sha": head, "context": "build"})
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_queue_output_labels_and_title_edits_do_not_dirty_completed_work(self):
        head = self.ready()
        self.intake.run_once()
        completed = self.intake.state.get(1)
        snapshots = sum(call[0] == "admission_snapshot" for call in self.github.calls)
        for label in ("bors-staging", "repaired-in-queue", "unrelated-label"):
            for action in ("labeled", "unlabeled"):
                with self.subTest(label=label, action=action):
                    self.event(
                        "pull_request",
                        {
                            "number": 1,
                            "action": action,
                            "label": {"name": label},
                            "pull_request": {"head": {"sha": head}},
                        },
                    )
                    self.assertEqual(self.intake.state.get(1), completed)
        self.event(
            "pull_request",
            {"number": 1, "action": "edited", "changes": {"title": {"from": "Old title"}}},
        )
        self.assertEqual(self.intake.state.get(1), completed)
        self.assertEqual(self.intake.run_once()["evaluated"], 0)
        self.assertEqual(sum(call[0] == "admission_snapshot" for call in self.github.calls), snapshots)

    def test_policy_label_body_and_unknown_pr_events_still_dirty_work(self):
        self.ready()
        self.intake.run_once()
        events = [
            {"action": action, "label": {"name": label}}
            for label in ("hold", "dont-repair-in-queue", "needs-rebase")
            for action in ("labeled", "unlabeled")
        ] + [
            {"action": "edited", "changes": {"body": {"from": "Old body"}}},
            {"action": "edited", "changes": {"title": {"from": "Old title"}, "base": {"ref": {"from": "main"}}}},
            {"action": "edited"},
            {"action": "labeled"},
            {"action": "synchronize"},
            {"action": "closed"},
            {"action": "converted_to_draft"},
            {"action": "unknown-action"},
        ]
        for event in events:
            with self.subTest(event=event):
                generation = self.intake.state.get(1)["generation"]
                self.event("pull_request", {"number": 1, **event})
                self.assertEqual(self.intake.state.get(1)["generation"], generation + 1)

    def test_custom_no_repair_labels_match_case_insensitively_in_events_and_versions(self):
        head = self.ready()
        self.intake.run_once()
        self.controller.set_setting("no_repair_labels", ["CUSTOM-NO-REPAIR"], "test")
        self.github.labels[1].add("custom-no-repair")
        generation = self.intake.state.get(1)["generation"]
        self.event("pull_request", {"number": 1, "action": "labeled", "label": {"name": "custom-no-repair"}})
        self.assertEqual(self.intake.state.get(1)["generation"], generation + 1)
        self.assertEqual(self.controller.input_for(REPO, 1)["no_repair"], 1)
        source = {"state": "open", "head": head}
        self.assertNotEqual(
            self.intake._version(source), self.intake._version(source | {"labels": ["custom-no-repair"]})
        )

    def test_inference_outage_preserves_primary_intake_and_resumes_the_same_queue(self):
        for _ in range(self.controller.setting("inference_failure_threshold")):
            self.controller.report_inference("worker", False, "all providers unavailable")
        self.ready()
        self.assertEqual(self.intake.run_once()["approved"], 1)
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")
        self.assertEqual(self.github.dispatches, [])
        hold = self.controller.inference_hold()
        membership = self.controller.store.rows("members")
        self.restart()
        self.assertEqual(self.controller.inference_hold(), hold)
        self.assertEqual(self.controller.store.rows("members"), membership)
        self.controller.report_inference("worker", True)
        self.controller.tick()
        self.assertEqual(len(self.github.dispatches), 1)
        self.assertIsNone(self.controller.inference_hold())

    def test_unsafe_review_revokes_waiting_but_neutral_build_does_not(self):
        head = self.ready()
        self.intake.run_once()
        self.github.statuses[(head, "build")] = Status("failure", "", "", self.clock(), None)
        self.intake.invalidate(1)
        self.intake.run_once()
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")
        self.review(state="blocking_block", round=2)
        self.intake.invalidate(1)
        self.intake.run_once()
        self.assertEqual(self.controller.inputs()[0]["state"], "cancelled")
        self.assertEqual(self.controller.inputs()[0]["reason"], "review-unsafe")

    def test_cancel_survives_restart_ci_change_incidental_review_edit_and_late_legacy_check(self):
        head = self.ready()
        self.intake.run_once()
        self.controller.unapprove(REPO, 1, "kim-em")
        self.restart()
        self.clock.advance(1000)
        self.github.review_comments[1][0]["body"] += "\nAccounting corrected."
        self.github.review_comments[1][0]["updated_at"] = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        check = self.github.publish_check(1)
        self.event("check_run", {"check_run": check})
        self.event("status", {"sha": head, "context": "build"})
        self.intake.run_once()
        self.assertEqual(len(self.controller.inputs()), 1)
        self.assertEqual(self.controller.inputs()[0]["state"], "cancelled")
        self.clock.advance(1)
        self.review(round=2)
        self.intake.invalidate(1)
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_cancel_before_initial_evaluation_is_durable(self):
        head = self.ready()
        self.controller.cancel(REPO, 1, "kim-em", head_sha=head)
        self.restart()
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(self.controller.inputs(), [])
        self.clock.advance(1)
        self.review(round=2)
        self.intake.invalidate(1)
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_cutover_uses_the_legacy_withdrawal_time_without_rejecting_a_new_comment(self):
        head = self.ready()
        self.controller.admission = None
        self.controller.approve(REPO, 1, head, "kim-em", 0, {"kind": "issue_comment"})
        self.clock.advance(1)
        self.controller.cancel(REPO, 1, "kim-em")
        self.clock.advance(60)
        self.review(round=2, comment=11)
        self.clock.advance(60)
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_cutover_does_not_treat_an_edit_of_the_cancelled_review_as_fresh_intent(self):
        head = self.ready()
        self.controller.admission = None
        self.controller.approve(REPO, 1, head, "kim-em", 0, {"kind": "issue_comment"})
        self.clock.advance(1)
        self.controller.cancel(REPO, 1, "kim-em")
        self.clock.advance(60)
        self.github.review_comments[1][0]["body"] += "\nAccounting updated."
        self.github.review_comments[1][0]["updated_at"] = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "cancelled")

    def test_event_during_read_prevents_old_commit_without_holding_controller_lock(self):
        head = self.ready()
        self.intake.state.dirty(1, head=head)
        self.intake._discovery_at = self.clock() + 1000
        original = self.github.admission_snapshot
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            started.set()
            self.assertTrue(release.wait(3))
            return snapshot

        self.github.admission_snapshot = slow
        thread = threading.Thread(target=self.intake.run_once)
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertFalse(self.controller.status().get("snapshot"))
            self.controller.cancel(REPO, 1, "kim-em", head_sha=head)
            self.event("status", {"sha": head, "context": "build"})
            self.assertEqual(self.intake.state.summary()["pending"], 1)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.controller.inputs(), [])
        self.assertEqual(self.intake.state.summary()["pending"], 0)
        self.github.admission_snapshot = original
        self.intake.run_once()
        self.assertEqual(self.controller.inputs(), [])

    def test_real_quota_deadline_survives_restart_and_resumes_same_owner(self):
        self.ready()
        original = self.github.admission_snapshot
        until = self.clock() + 7200

        def limited(*args, **kwargs):
            raise RateLimited("core quota", retry_at=until, resource="core")

        self.github.admission_snapshot = limited
        self.intake.run_once()
        self.restart()
        self.assertEqual(self.intake.run_once()["evaluated"], 0)
        self.assertEqual(self.intake.state.summary()["next_attempt_at"], until)
        self.github.admission_snapshot = original
        self.clock.advance(7200)
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_discovery_server_cooldown_protects_new_dirty_work_after_restart(self):
        self.ready()
        until = self.clock() + 7200
        calls = []

        def limited_transport(*args):
            calls.append(args)
            return 403, {"retry-after": "7200"}, b'{"message":"secondary rate limit"}'

        self.github.client = GitHubClient(None, limited_transport, clock=self.clock)
        original_listing = self.github.list_open_pull_requests

        def discovery(*args, **kwargs):
            self.github.client.get_json("discovery")
            return []

        self.github.list_open_pull_requests = discovery
        self.assertEqual(self.intake.run_once()["deferred"], 1)
        self.assertIsNone(self.intake.state.get(1), "discovery failed before observing any PR")
        self.assertEqual(self.controller.store.get_setting(self.intake.state.resource_key), {"secondary": until})

        def recovered_transport(*args):
            calls.append(args)
            return 200, {}, b"{}"

        self.github.client = GitHubClient(None, recovered_transport, clock=self.clock)
        self.restart()
        original_snapshot = self.github.admission_snapshot

        def snapshot(*args, **kwargs):
            self.github.client.get_json("new-dirty-head")
            return original_snapshot(*args, **kwargs)

        self.github.admission_snapshot = snapshot
        self.intake.invalidate(1)
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(len(calls), 1, "new PR reads must respect the restored discovery cooldown")
        self.assertEqual(self.intake.state.summary()["next_attempt_at"], until)
        self.assertEqual(self.controller.store.get_setting(self.intake.state.resource_key), {"secondary": until})
        self.clock.advance(7200)
        self.github.list_open_pull_requests = original_listing
        self.assertEqual(self.intake.run_once()["approved"], 1)
        self.assertEqual(len(calls), 2)

    def test_poison_pr_does_not_block_other_prs_and_failure_is_not_completed(self):
        self.ready(1)
        self.ready(2)
        self.github.fail_next["admission_snapshot"] = 1
        result = self.intake.run_once()
        self.assertEqual((result["approved"], result["pending"]), (1, 1))
        self.assertEqual(self.intake.state.get(1)["evaluated_generation"], 0)
        self.clock.advance(31)
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_inference_hold_does_not_prevent_deterministic_intake(self):
        self.ready()
        self.controller._set_state("inference_hold", {"since": self.clock(), "last_error": "providers down"})
        self.assertEqual(self.intake.run_once()["approved"], 1)
        self.assertIsNotNone(self.controller.status()["inference_hold"])

    def test_manual_priority_is_not_reset_by_policy_reevaluation(self):
        self.ready()
        self.intake.run_once()
        self.controller.set_priority(REPO, 1, 42)
        self.intake.invalidate(1)
        self.intake.run_once()
        self.assertEqual(self.controller.inputs()[0]["priority"], 42)

    def test_edit_removing_scoreboard_marker_invalidates_and_revokes(self):
        self.ready()
        self.intake.run_once()
        old_body = self.github.review_comments[1][0]["body"]
        self.github.review_comments[1][0]["body"] = "Review withdrawn."
        self.event(
            "issue_comment",
            {
                "action": "edited",
                "issue": {"number": 1, "pull_request": {}},
                "comment": {"body": "Review withdrawn."},
                "changes": {"body": {"from": old_body}},
            },
        )
        self.assertEqual(self.intake.run_once()["revoked"], 1)
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "cancelled")

    def test_policy_change_is_not_fresh_authorization_after_withdrawal(self):
        self.ready()
        self.intake.run_once()
        self.controller.cancel(REPO, 1, "kim-em")
        changed = AdmissionPolicy(rubrics=self.config.rubrics, prefix="TauCeti/", allow_files=["TauCeti.lean"])
        self.intake = AdmissionPoller(self.controller, self.github, REPO, changed, clock=self.clock)
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(self.controller.inputs()[0]["state"], "cancelled")

    def test_manual_reapproval_clears_withdrawal_intent(self):
        head = self.ready()
        self.intake.run_once()
        self.controller.cancel(REPO, 1, "kim-em")
        self.controller.approve(REPO, 1, head, "kim-em", evidence={"kind": "issue_comment"})
        self.assertEqual(self.intake.state.get(1)["cancelled_head_sha"], "")
        self.intake.run_once()
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

    def test_main_event_during_source_read_fences_the_decision(self):
        head = self.ready()
        self.intake.state.dirty(1, head=head)
        self.intake._discovery_at = self.clock() + 1000
        original = self.github.admission_snapshot
        reads = []

        def changed(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            reads.append(snapshot)
            if len(reads) == 1:
                self.event("push", {"ref": "refs/heads/main", "after": "f" * 40})
            return snapshot

        self.github.admission_snapshot = changed
        self.intake.run_once()
        self.assertEqual(len(reads), 2, "the pre-event result must be replaced by a fresh read")
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

    def test_normal_main_advance_does_not_reevaluate_an_unchanged_diff(self):
        self.ready()
        self.intake.run_once()
        self.repo.commit_branch(
            {"README.md": "new main\n"}, "advance", start=self.repo.main_sha(), push_to="refs/heads/main"
        )
        self.event("push", {"ref": "refs/heads/main", "after": self.repo.main_sha()})
        self.assertEqual(self.intake.run_once()["evaluated"], 0)

    def test_failed_atomic_admission_rolls_back_and_retries_without_duplicate_input(self):
        self.ready()
        complete = self.intake.state.complete

        def interrupted(*args, **kwargs):
            raise RuntimeError("interruption after input insert")

        self.intake.state.complete = interrupted
        self.intake.run_once()
        self.assertEqual(self.controller.inputs(), [])
        self.intake.state.complete = complete
        self.clock.advance(31)
        self.assertEqual(self.intake.run_once()["approved"], 1)
        self.restart()
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(len(self.controller.inputs()), 1)

    def test_primary_evaluation_progresses_beyond_the_old_invocation_cap(self):
        self.ready()
        for number in range(2, 152):
            self.github.prs[number] = dict(self.github.prs[1], number=number)
            self.github.labels[number] = set()
            self.review(number, comment=1000 + number)
        result = self.intake.run_once()
        self.assertEqual((result["approved"], result["pending"]), (151, 0))
        self.assertEqual(len(self.controller.inputs("waiting")), 151)

    def recovery(self, *, head=None, author="tau-ceti-bors[bot]", stalled=False):
        head = head or self.github.prs[1]["head_sha"]
        marker = "tauceti-merge-stalled" if stalled else "tauceti-rebase"
        self.github.review_comments[1].append(
            {
                "id": 50,
                "user": {"login": author},
                "body": f"Merge-queue recovery for head `{head}`: repair needed\n\n<!--{marker}:v1 {head}-->",
            }
        )

    def test_trusted_current_head_recovery_holds_survive_the_move_from_sweep(self):
        self.ready()
        self.recovery()
        self.assertEqual(self.intake.run_once()["approved"], 0)
        self.assertEqual(self.controller.inputs(), [])
        labels = [json.loads(row["payload"]) for row in self.controller.store.rows("outbox", {"kind": "labels"})]
        self.assertTrue(any("needs-rebase" in row["add"] for row in labels))
        self.assertIn("trusted recovery", json.loads(self.intake.state.get(1)["decision"])["reason"])

    def test_an_untrusted_recovery_marker_does_not_create_a_hold(self):
        self.ready()
        self.recovery(author="contributor")
        self.assertEqual(self.intake.run_once()["approved"], 1)

    def test_stale_recovery_label_cleanup_is_bound_to_the_current_head_and_generation(self):
        self.ready()
        self.recovery(head="a" * 40)
        self.github.labels[1].add("needs-rebase")
        self.assertEqual(self.intake.run_once()["approved"], 1)
        entry = next(
            row
            for row in self.controller.store.rows("outbox", {"kind": "labels"})
            if "needs-rebase" in json.loads(row["payload"])["remove"]
        )
        self.intake.invalidate(1)
        result = self.controller._perform(entry["kind"], json.loads(entry["payload"]), self.clock())
        self.assertEqual(result, "stale-admission")
        self.assertIn("needs-rebase", self.github.labels[1])

    def test_new_review_revocation_is_selected_ahead_of_an_existing_backlog(self):
        self.ready(1)
        self.intake.run_once()
        self.ready(2)
        self.ready(3)
        self.ready(4)
        original = self.github.admission_snapshot
        reads = []

        def observe(repo, number, **kwargs):
            snapshot = original(repo, number, **kwargs)
            reads.append(number)
            if number == 2:
                self.review(1, state="blocking_block", round=2)
                self.intake.invalidate(1)
            return snapshot

        self.github.admission_snapshot = observe
        self.intake.request_discovery()
        self.intake.run_once()
        self.assertLess(reads.index(1), reads.index(4), "new revocations must not wait for the old pass list")
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "cancelled")

    def test_pending_review_change_cannot_be_inserted_while_its_read_is_quota_blocked(self):
        self.ready()
        self.intake.run_once()
        self.review(state="blocking_block", round=2)
        self.intake.invalidate(1)
        until = self.clock() + 7200

        def limited(*args, **kwargs):
            raise RateLimited("review quota", retry_at=until, resource="core")

        self.github.admission_snapshot = limited
        self.intake.run_once()
        self.controller.tick()
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")
        self.assertEqual(self.controller.store.count("members"), 0)


if __name__ == "__main__":
    unittest.main()

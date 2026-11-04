#!/usr/bin/env python3
"""The approval reconciliation poll (tauceti_queue.approval_poll) over the fake GitHub.

Covered: an eligible `merge eligibility` check the webhook never delivered is discovered by the poll
and counted in `status.approvals.polled` (a webhook-delivered one counts under `webhook`); the next
pass finds it unchanged and spends no PR read; a revocation (review unsafe) withdraws a waiting
input; a head that moved without a `synchronize` delivery withdraws the input the way the webhook
path does, and the new head is approved once it carries an eligible check; a PR closed or turned
draft behind the webhook's back withdraws its input; a member PR's revocation or head change is
journaled and leaves the membership alone, once per decision; the pass is skipped below the quota
reserve and stops early when the quota runs out mid-way; `due` follows
`approval_poll_interval_seconds`; the loop runs at start, on the interval and on wake; and the
`reconcile-approvals` command.
"""

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import __main__ as cli  # noqa: E402
from tauceti_queue import webhook  # noqa: E402
from tauceti_queue.approval_poll import ApprovalPoller, ApprovalPollLoop  # noqa: E402
from tauceti_queue.fakes import make_controller  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"


class PollCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root, publication=False)
        self.addCleanup(self.controller.store.close)
        self.poller = ApprovalPoller(self.controller, self.github, PROJECT, clock=self.clock)

    def calls(self, name):
        return sum(1 for called, _ in self.github.calls if called == name)

    def approvals(self):
        return self.controller.status()["approvals"]

    def journal(self, kind):
        return self.controller.store.journal_entries(kind)


class Discovery(PollCase):
    def test_poll_discovers_an_approval_the_webhook_never_delivered(self):
        head = self.github.create_pr(1, {"A.lean": "a\n"})
        self.github.create_pr(2, {"B.lean": "b\n"})  # no check at all
        self.github.publish_check(1)  # TauCetiReview's check run; no webhook delivery reaches the service
        self.assertEqual(self.controller.inputs(), [])
        summary = self.poller.run_once(reason="start")
        self.assertEqual(
            {k: summary[k] for k in ("listed", "with_check", "reconciled", "approved", "revoked", "skipped")},
            {"listed": 2, "with_check": 1, "reconciled": 1, "approved": 1, "revoked": 0, "skipped": False},
        )
        (row,) = self.controller.inputs("waiting")
        self.assertEqual((row["number"], row["head_sha"], row["reviewer"]), (1, head, "tauceti-review-bot[bot]"))
        self.assertEqual(json.loads(row["evidence"])["kind"], "eligibility_check")
        approvals = self.approvals()
        self.assertEqual((approvals["polled"], approvals["webhook"]), (1, 0))
        self.assertEqual((approvals["last_poll"]["reason"], approvals["last_poll"]["approved"]), ("start", 1))
        reads = self.calls("fetch_pr")
        again = self.poller.run_once()
        self.assertEqual((again["approved"], again["unchanged"], again["reconciled"]), (0, 1, 0))
        self.assertEqual(self.calls("fetch_pr"), reads, "an approval already recorded costs no PR read")
        self.assertEqual(self.approvals()["polled"], 1)

    def test_webhook_approvals_count_separately(self):
        self.github.create_pr(1, {"A.lean": "a\n"})
        check = self.github.publish_check(1)
        receiver = webhook.WebhookReceiver(self.controller, self.github, repositories=[PROJECT])
        delivery = webhook.Delivery(
            "check_run",
            "d1",
            {"action": "completed", "check_run": check, "repository": {"full_name": PROJECT}},
        )
        self.assertEqual(receiver.handle(delivery), "ok")
        self.assertEqual(len(self.controller.inputs("waiting")), 1)
        self.assertEqual((self.approvals()["webhook"], self.approvals()["polled"]), (1, 0))
        summary = self.poller.run_once()
        self.assertEqual((summary["approved"], summary["unchanged"]), (0, 1))
        self.assertEqual((self.approvals()["webhook"], self.approvals()["polled"]), (1, 0))

    def test_a_settled_decision_is_not_retried_but_a_new_check_is(self):
        self.github.create_pr(1, {"A.lean": "a\n"})
        self.github.labels[1].add("hold")
        self.github.publish_check(1)
        self.assertEqual(self.poller.run_once()["reconciled"], 1)
        self.assertEqual(self.controller.inputs(), [], "a keep label blocks")
        self.assertEqual(self.poller.run_once()["reconciled"], 1, "a blocked decision is retried (the label may go)")
        self.github.labels[1].discard("hold")
        self.assertEqual(self.poller.run_once()["approved"], 1)
        # A neutral check (waiting) decides nothing and costs nothing.
        self.github.create_pr(2, {"B.lean": "b\n"})
        self.github.publish_check(2, eligible=False, merge_base=None)
        summary = self.poller.run_once()
        self.assertEqual((summary["with_check"], summary["reconciled"]), (2, 0))


class Revocation(PollCase):
    def approved(self, number=1):
        head = self.github.create_pr(number, {f"P{number}.lean": "x\n"})
        self.github.publish_check(number)
        self.assertEqual(self.poller.run_once()["approved"], 1)
        return head

    def test_revocation_withdraws_a_waiting_input(self):
        self.approved()
        self.github.publish_check(1, eligible=False, review_safe=False, merge_base=None)
        summary = self.poller.run_once()
        self.assertEqual((summary["revoked"], summary["reconciled"]), (1, 1))
        (row,) = self.controller.inputs()
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "unapproved"))
        self.assertEqual(self.approvals()["revoked_by_poll"], 1)
        self.assertEqual(self.poller.run_once()["reconciled"], 0, "the same revocation is not applied twice")

    def test_a_withdrawn_approval_is_not_readmitted_until_a_newer_check(self):
        self.approved()
        self.controller.unapprove(PROJECT, 1, "kim-em")  # `bors r-` by a human
        self.assertEqual(self.controller.inputs()[0]["reason"], "unapproved")
        self.poller._applied.clear()  # as after a service restart
        summary = self.poller.run_once()
        self.assertEqual((summary["with_check"], summary["reconciled"], summary["approved"]), (1, 0, 0))
        self.assertEqual(len(self.controller.inputs()), 1, "the eligible check was consumed before the withdrawal")
        self.github.publish_check(1)  # TauCetiReview decides again: a higher check id
        self.assertEqual(self.poller.run_once()["approved"], 1)
        self.assertEqual([r["state"] for r in self.controller.inputs()], ["cancelled", "waiting"])

    def test_head_change_withdraws_then_the_new_head_is_approved(self):
        old = self.approved()
        new = self.github.push_pr(1, {"P1.lean": "y\n"})  # no `synchronize` delivery
        summary = self.poller.run_once()
        self.assertEqual((summary["head_changed"], summary["approved"]), (1, 0))
        rows = self.controller.inputs()
        self.assertEqual([(r["head_sha"], r["state"], r["reason"]) for r in rows], [(old, "cancelled", "head-changed")])
        self.assertEqual(self.journal("withdrawn")[-1]["detail"]["head"], new)
        self.assertEqual(self.poller.run_once()["head_changed"], 0, "told once")
        self.github.publish_check(1)
        self.assertEqual(self.poller.run_once()["approved"], 1)
        self.assertEqual([r["head_sha"] for r in self.controller.inputs("waiting")], [new])

    def test_closed_and_draft_prs_withdraw_their_inputs(self):
        self.approved(1)
        self.approved(2)
        self.github.prs[1]["state"] = "closed"
        self.github.prs[2]["draft"] = True
        summary = self.poller.run_once()
        self.assertEqual((summary["listed"], summary["closed"], summary["drafted"]), (1, 1, 1))
        reasons = {r["number"]: r["reason"] for r in self.controller.inputs("cancelled")}
        self.assertEqual(reasons, {1: "closed", 2: "draft"})
        self.assertEqual(self.poller.run_once()["closed"], 0)

    def test_member_revocation_and_head_change_are_journaled_and_kept(self):
        self.approved()
        self.controller.tick()
        (row,) = self.controller.inputs()
        self.assertEqual(row["state"], "inserted")
        self.github.publish_check(1, eligible=False, review_safe=False, merge_base=None)
        summary = self.poller.run_once()
        self.assertEqual((summary["member_events"], summary["revoked"]), (1, 0))
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")
        self.assertEqual(len(self.journal("withdrawal-refused")), 1)
        self.github.push_pr(1, {"P1.lean": "z\n"})
        summary = self.poller.run_once()
        self.assertEqual(summary["member_events"], 1)
        self.assertEqual(len(self.journal("member-event-ignored")), 1)
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")
        summary = self.poller.run_once()
        self.assertEqual((summary["member_events"], summary["reconciled"]), (0, 0), "each decision once")
        self.assertEqual(len(self.journal("withdrawal-refused")), 1)
        self.assertEqual(len(self.journal("member-event-ignored")), 1)


class Budget(PollCase):
    def test_skipped_below_the_reserve_and_resumed_after(self):
        self.github.create_pr(1, {"A.lean": "a\n"})
        self.github.publish_check(1)
        self.github.remaining = 150
        summary = self.poller.run_once()
        self.assertTrue(summary["skipped"])
        self.assertEqual((self.calls("list_open_pull_requests"), self.poller.skipped, self.poller.passes), (0, 1, 0))
        self.assertTrue(self.approvals()["last_poll"]["skipped"])
        self.assertEqual(self.controller.inputs(), [])
        self.github.remaining = 4800
        self.assertEqual(self.poller.run_once()["approved"], 1)
        self.assertEqual(self.poller.passes, 1)

    def test_stops_early_when_the_quota_runs_out_mid_pass(self):
        for number in (1, 2, 3):
            self.github.create_pr(number, {f"P{number}.lean": "x\n"})
            self.github.publish_check(number)
        listing = self.github.list_open_pull_requests

        def exhausting(repo, *, review_app_id):
            result = listing(repo, review_app_id=review_app_id)
            self.github.remaining = 10
            return result

        self.github.list_open_pull_requests = exhausting
        summary = self.poller.run_once()
        self.assertEqual((summary["listed"], summary["reconciled"], summary["stopped_early"]), (3, 0, True))
        self.assertEqual(self.controller.inputs(), [])
        self.github.list_open_pull_requests = listing
        self.github.remaining = 4800
        self.assertEqual(self.poller.run_once()["approved"], 3)

    def test_due_follows_the_interval_setting(self):
        self.assertTrue(self.poller.due())
        self.poller.run_once()
        self.assertFalse(self.poller.due())
        self.clock.advance(899)
        self.assertFalse(self.poller.due())
        self.clock.advance(1)
        self.assertTrue(self.poller.due())
        self.controller.set_setting("approval_poll_interval_seconds", 60, "kim")
        self.poller.run_once()
        self.clock.advance(60)
        self.assertTrue(self.poller.due())
        self.assertEqual(self.poller.interval(), 60.0)

    def test_loop_runs_at_start_on_the_interval_and_on_wake(self):
        loop = ApprovalPollLoop(self.poller, max_interval=0.05)
        loop.start()
        deadline = time.monotonic() + 5
        while self.poller.passes < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(self.poller.passes, 3)
        self.assertEqual(self.poller.last_summary["reason"], "interval")
        loop.max_interval = 30
        passes = self.poller.passes
        loop.wake()
        deadline = time.monotonic() + 5
        while self.poller.passes <= passes and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.poller.last_summary["reason"], "wake")
        loop.stop()
        self.assertEqual(self.approvals()["last_poll"]["reason"], "wake")


class Command(unittest.TestCase):
    def test_reconcile_approvals_posts_and_prints_the_summary(self):
        requests = []

        def fetch(url, method="GET", body=None, token=None, timeout=10):
            requests.append((url, method, body, token))
            return {"listed": 1200, "approved": 2, "reason": "operator kim-em"}

        args = cli.build_parser().parse_args(["reconcile-approvals", "--url", "http://q", "--token", "ghp_x"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_reconcile_approvals(args, fetch=fetch), 0)
        self.assertEqual(requests, [("http://q/admin/reconcile-approvals", "POST", {}, "ghp_x")])
        self.assertEqual(json.loads(out.getvalue())["approved"], 2)


if __name__ == "__main__":
    unittest.main()

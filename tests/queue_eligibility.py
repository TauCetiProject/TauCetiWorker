#!/usr/bin/env python3
"""The review App's `merge eligibility` checks (tauceti_queue.eligibility), the way TauCetiReview
approves today, consumed as Bors's `merge_eligibility.ex` consumes them.

Covered: trusted checks by name and App id, the decision decoded from `external_id` with Bors's
consistency rules (schema, repo, PR, head, completion, flags, merge base, conclusion), the highest
id winning regardless of delivery order, and `reconcile` against a fake controller: approval with
the check as evidence, revocation on an unsafe review, neutral and ineligible decisions waiting,
keep labels blocking, a moved merge base waiting, and the webhook receiver routing `check_run`
deliveries to it.
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import eligibility as e  # noqa: E402
from tauceti_queue import webhook  # noqa: E402
from tauceti_queue.github_adapter import PullRequest  # noqa: E402

TAUCETI = "TauCetiProject/TauCeti"
HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "d" * 40


def check(
    check_id=10,
    *,
    pr=42,
    head=HEAD,
    eligible=True,
    review_safe=True,
    single=False,
    merge_base=MERGE_BASE,
    app_id=e.REVIEW_APP_ID,
    name=e.NAME,
    status="completed",
    conclusion=None,
    repo=TAUCETI,
    schema=e.SCHEMA,
):
    data = {
        "schema": schema,
        "repo": repo,
        "pr": pr,
        "merge_base_sha": merge_base,
        "eligible": eligible,
        "review_safe": review_safe,
        "single": single,
    }
    if conclusion is None:
        conclusion = "success" if eligible else ("neutral" if review_safe else "failure")
    return {
        "id": check_id,
        "name": name,
        "app": {"id": app_id},
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "external_id": json.dumps(data, sort_keys=True, separators=(",", ":")),
        "completed_at": "2026-11-04T07:00:00Z",
        "html_url": "https://github.com/TauCetiProject/TauCeti/runs/10",
    }


class Decoding(unittest.TestCase):
    def test_trusted_and_notification_pr(self):
        self.assertTrue(e.trusted(check()))
        self.assertFalse(e.trusted(check(app_id=1)))
        self.assertFalse(e.trusted(check(name="other")))
        self.assertEqual(e.notification_pr(check(), TAUCETI), 42)
        self.assertIsNone(e.notification_pr(check(repo="other/repo"), TAUCETI))
        self.assertIsNone(
            e.notification_pr({"name": e.NAME, "app": {"id": e.REVIEW_APP_ID}, "external_id": "not json"}, TAUCETI)
        )

    def test_decode_rules(self):
        decision = e.decode(check(), TAUCETI, 42, HEAD)
        self.assertEqual(
            (decision.check_id, decision.eligible, decision.review_safe, decision.single, decision.merge_base_sha),
            (10, True, True, False, MERGE_BASE),
        )
        self.assertEqual(decision.conclusion, "success")
        for broken in [
            check(pr=43),
            check(head="c" * 40),
            check(status="in_progress"),
            check(conclusion="neutral"),
            check(schema="tauceti-merge.eligibility/v2"),
            check(eligible=True, merge_base="short"),
            check(eligible=True, review_safe=False),
            check(check_id=0),
        ]:
            self.assertIsNone(e.decode(broken, TAUCETI, 42, HEAD), broken["external_id"])
        self.assertEqual(
            e.decode(check(eligible=False, review_safe=False, merge_base=None), TAUCETI, 42, HEAD).conclusion, "failure"
        )
        self.assertEqual(e.decode(check(eligible=False, merge_base=None), TAUCETI, 42, HEAD).conclusion, "neutral")

    def test_latest_wins_by_id_and_ignores_other_apps(self):
        checks = [check(5, eligible=False), check(9, app_id=1), check(7, eligible=True), check(6, head="c" * 40)]
        decision = e.latest(checks, TAUCETI, 42, HEAD)
        self.assertEqual((decision.check_id, decision.eligible), (7, True))
        self.assertIsNone(e.latest([check(9, app_id=1)], TAUCETI, 42, HEAD))
        self.assertIsNone(
            e.latest([check(8, status="in_progress"), check(5)], TAUCETI, 42, HEAD),
            "an invalid latest check decides nothing",
        )


class ProductionRecords(unittest.TestCase):
    def test_real_rest_check_runs_decode_with_the_highest_id_winning(self):
        fixture = json.loads((REPO / "tests/fixtures/merge-eligibility-check-runs.json").read_text())
        head = fixture["rest"][0]["head_sha"]
        decision = e.latest(fixture["rest"], fixture["repo"], 24557, head, fixture["review_app_id"])
        self.assertEqual((decision.check_id, decision.eligible, decision.single), (125149712005, True, False))
        self.assertEqual(decision.merge_base_sha, "93af5b549b7d9ed44291d6ceb093a8013dda00f8")
        self.assertEqual(e.notification_pr(fixture["rest"][1], fixture["repo"]), 24557)
        self.assertEqual(e.decode(fixture["rest"][1], fixture["repo"], 24557, head).conclusion, "failure")


class FakeController:
    canonical_branch = "main"

    def __init__(self):
        self.calls = []

    def approve(self, repo, pr, head_sha, reviewer, priority, evidence):
        self.calls.append(("approve", repo, pr, head_sha, reviewer, priority, evidence))

    def unapprove(self, repo, pr, actor=""):
        self.calls.append(("unapprove", repo, pr, actor))

    def handle_event(self, kind, payload):
        self.calls.append(("event", kind))


class FakeAdapter:
    def __init__(self, pr, checks, merge_base=MERGE_BASE):
        self.pr = pr
        self.checks = checks
        self.merge_base = merge_base

    def fetch_pr(self, repo, number):
        if self.pr is None:
            raise LookupError(number)
        return self.pr

    def eligibility_checks(self, repo, head):
        return self.checks

    def pr_merge_base(self, repo, number, head):
        return self.merge_base


def pr(**overrides):
    fields = dict(
        number=42,
        head_sha=HEAD,
        base_ref="main",
        title="t",
        body="",
        author_login="a",
        state="open",
        draft=False,
        labels=(),
        base_sha=BASE,
    )
    fields.update(overrides)
    return PullRequest(**fields)


class Reconcile(unittest.TestCase):
    def test_eligible_approves_with_the_check_as_evidence(self):
        controller = FakeController()
        self.assertEqual(e.reconcile(controller, FakeAdapter(pr(), [check(single=True)]), TAUCETI, 42), "approved")
        (call,) = controller.calls
        self.assertEqual(call[:6], ("approve", TAUCETI, 42, HEAD, "tauceti-review-bot[bot]", 0))
        evidence = call[6]
        self.assertEqual(
            (evidence["kind"], evidence["actor_login"], evidence["actor_id"], evidence["comment_id"]),
            ("eligibility_check", "tauceti-review-bot[bot]", e.REVIEW_APP_ID, 10),
        )
        self.assertEqual(
            (evidence["head_sha"], evidence["merge_base_sha"], evidence["single"], evidence["admin"]),
            (HEAD, MERGE_BASE, True, False),
        )

    def test_unsafe_review_revokes(self):
        controller = FakeController()
        self.assertEqual(
            e.reconcile(
                controller, FakeAdapter(pr(), [check(eligible=False, review_safe=False, merge_base=None)]), TAUCETI, 42
            ),
            "revoked",
        )
        self.assertEqual(controller.calls, [("unapprove", TAUCETI, 42, "tauceti-review-bot[bot]")])

    def test_waiting_cases(self):
        for adapter, expected in [
            (FakeAdapter(pr(), []), "waiting"),
            (FakeAdapter(pr(), [check(eligible=False, merge_base=None)]), "waiting"),
            (FakeAdapter(pr(draft=True), [check()]), "waiting"),
            (FakeAdapter(pr(state="closed"), [check()]), "waiting"),
            (FakeAdapter(pr(base_ref="release"), [check()]), "waiting"),
            (FakeAdapter(pr(labels=("Keep",)), [check()]), "blocked"),
            (FakeAdapter(pr(), [check()], merge_base="e" * 40), "moved"),
            (FakeAdapter(None, [check()]), "absent"),
        ]:
            controller = FakeController()
            self.assertEqual(e.reconcile(controller, adapter, TAUCETI, 42), expected)
            self.assertEqual(controller.calls, [], expected)

    def test_webhook_routes_check_runs(self):
        controller = FakeController()
        adapter = FakeAdapter(pr(), [check()])
        receiver = webhook.WebhookReceiver(controller, adapter, repositories=[TAUCETI])
        payload = {"action": "completed", "repository": {"full_name": TAUCETI}, "check_run": check()}
        self.assertEqual(receiver.handle(webhook.Delivery("check_run", "d1", payload)), "ok")
        self.assertEqual([c[0] for c in controller.calls], ["approve", "event"])
        other = {
            "action": "completed",
            "repository": {"full_name": TAUCETI},
            "check_run": {"name": "build", "app": {"id": 1}},
        }
        receiver.handle(webhook.Delivery("check_run", "d2", other))
        self.assertEqual(
            [c[0] for c in controller.calls], ["approve", "event", "event"], "other checks only reach handle_event"
        )


if __name__ == "__main__":
    unittest.main()

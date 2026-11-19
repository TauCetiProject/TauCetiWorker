"""Admission evidence cache and its shadow comparison: inert when off, ordered, never deciding."""

import dataclasses
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import webhook
from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.controller import ControllerError
from tauceti_queue.fakes import make_controller
from tauceti_queue.github_adapter import PullRequest
from tauceti_queue.ports import Status

REPO = "TauCetiProject/TauCeti"


def iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller, self.github, self.repo, self.clock = make_controller(self.tmp.name)
        self.addCleanup(lambda: self.controller.store.close())
        self.config = AdmissionPolicy(rubrics=["correctness", "reuse"], prefix="TauCeti/", allow_files=[])
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)
        self.updated = {}
        plain = self.github.admission_snapshot

        def snapshot(repo, number, *, generation=0):
            # The production adapter's fresh pull carries base sha, updated_at and the comment count.
            result = plain(repo, number, generation=generation)
            self.clock.advance(2)  # reads take time; deliveries meanwhile are newer than the read
            pull = result.pull
            full = PullRequest(
                **{f.name: getattr(pull, f.name) for f in dataclasses.fields(pull)},
                base_sha=self.repo.main_sha(),
                updated_at=self.updated.get(number, iso(self.clock())),
                head_ref=f"branch-{number}",
                head_repo=REPO,
                comments_count=len(self.github.review_comments.get(number, [])),
            )
            return dataclasses.replace(result, pull=full)

        self.github.admission_snapshot = snapshot

    # -- helpers

    def board(self, number, *, state="green", comment=10, at=None):
        head = self.github.prs[number]["head_sha"]
        base = self.github.pr_merge_base(REPO, number, head)
        ts = iso(self.clock() if at is None else at)
        meta = {
            "head_sha": head,
            "merge_base_sha": base,
            "round": 1,
            "ts": ts,
            "mode": "commit",
            "states": {r: state for r in self.config.rubrics},
        }
        return {
            "id": comment,
            "created_at": ts,
            "updated_at": ts,
            "html_url": "",
            "user": {"login": "reviewer", "id": 5},
            "body": "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->",
        }

    def ready(self, number=1):
        head = self.github.create_pr(number, {f"TauCeti/P{number}.lean": f"-- {number}\n"})
        self.github.review_comments[number] = [self.board(number)]
        for context in ("build", "scope"):
            self.github.statuses[(head, context)] = Status("success", "", "", self.clock(), None)
        return head

    def deliver(self, kind, payload):
        receiver = webhook.WebhookReceiver(self.controller, self.github, repositories=[REPO])
        repository = {**payload.get("repository", {}), "full_name": REPO}
        return receiver.handle(webhook.Delivery(kind, "", {**payload, "repository": repository}))

    def comment_event(self, number, comment, action="created"):
        self.deliver(
            "issue_comment",
            {
                "action": action,
                "issue": {
                    "number": number,
                    "pull_request": {},
                    "comments": len(self.github.review_comments.get(number, [])),
                    "updated_at": iso(self.clock()),
                },
                "comment": comment,
            },
        )

    def evaluate(self, number=1):
        self.intake.invalidate(number)
        self.intake.run_once()

    def shadow(self):
        return self.intake.evidence_status()["shadow"]

    def rows(self, table):
        return self.controller.store.rows(table, {"repository": REPO})

    # -- tests

    def test_off_by_default_writes_nothing(self):
        self.assertEqual(self.controller.setting("admission_evidence_mode"), "off")
        head = self.ready()
        self.comment_event(1, self.board(1))
        self.deliver("status", {"id": 1, "sha": head, "context": "build", "state": "success"})
        self.evaluate()
        self.assertEqual(self.rows("admission_evidence"), [])
        self.assertEqual(self.rows("admission_evidence_statuses"), [])
        self.assertEqual(self.intake.evidence_status(), {"mode": "off"})
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

    def test_mode_is_validated(self):
        with self.assertRaises(ControllerError):
            self.controller.set_setting("admission_evidence_mode", "live", "test")
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.assertEqual(self.controller.status()["admission"]["evidence"]["mode"], "shadow")

    def test_first_read_seeds_and_events_keep_it_in_step(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        head = self.ready()
        self.evaluate()
        self.assertEqual(self.shadow()["counts"], {"unseeded": 1})
        self.assertEqual(self.shadow()["seeded"], 1)
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

        # The scoreboard turns red; GitHub's delivery reaches the cache before the next read.
        self.clock.advance(60)
        red = self.board(1, state="red")
        self.github.review_comments[1] = [red]
        self.comment_event(1, red, "edited")
        self.clock.advance(1)
        self.evaluate()
        self.assertEqual(self.shadow()["counts"].get("match"), 1, self.shadow())
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "cancelled")

        # A status the cache never heard about is a disagreement, reported with its direction.
        self.clock.advance(60)
        green = self.board(1)
        self.github.review_comments[1] = [green]
        self.comment_event(1, green, "edited")
        self.github.statuses[(head, "build")] = Status("failure", "", "", self.clock(), None)
        self.clock.advance(1)
        self.evaluate()
        recent = self.shadow()["recent"][-1]
        self.assertEqual(recent["outcome"], "mismatch")
        self.assertIn("statuses", recent["fields"])
        self.assertEqual(recent["direction"], "cache-would-admit")
        self.assertEqual(self.shadow()["directions"], {"cache-would-admit": 1})

    def test_delivery_during_read_is_a_race_and_survives_seeding(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.ready()
        self.evaluate()
        plain = self.github.admission_snapshot
        late = self.board(1, comment=11)

        def racing(repo, number, *, generation=0):
            result = plain(repo, number, generation=generation)
            self.github.admission_snapshot = plain  # one race; the delivery re-dirties the PR
            self.clock.advance(1)
            self.github.review_comments[1] = [self.github.review_comments[1][0], late]
            self.comment_event(1, late)
            return result

        self.github.admission_snapshot = racing
        self.clock.advance(60)
        self.evaluate()
        self.assertEqual(self.shadow()["counts"].get("race"), 1, self.shadow())
        self.assertEqual(self.shadow()["counts"].get("match"), 1, self.shadow())  # the re-read agrees
        ids = {r["comment_id"] for r in self.rows("admission_evidence_comments") if not r["deleted"]}
        self.assertEqual(ids, {10, 11})  # the read did not see 11, but did not erase it either

    def test_ordering_rules(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        head = self.ready()
        cache = self.intake.evidence
        # Statuses: the higher status id wins whatever the arrival order.
        self.deliver("status", {"id": 7, "sha": head, "context": "build", "state": "failure"})
        self.deliver("status", {"id": 6, "sha": head, "context": "build", "state": "success"})
        self.deliver("status", {"id": 8, "sha": head, "context": "deploy", "state": "success"})
        self.deliver("status", {"id": 9, "sha": head, "context": "self-tested/1", "state": "success"})
        self.assertEqual(len(self.rows("admission_evidence_statuses")), 2)
        cache.store.execute("DELETE FROM admission_evidence_statuses WHERE context='self-tested/1'")
        statuses = self.rows("admission_evidence_statuses")
        self.assertEqual([(s["context"], s["state"]) for s in statuses], [("build", "failure")])
        # Comments: an older edit never replaces a newer one, and a deletion is final.
        new, old = self.board(1, at=200), self.board(1, at=100)
        self.comment_event(1, new, "edited")
        self.comment_event(1, old, "edited")
        (row,) = self.rows("admission_evidence_comments")
        self.assertEqual(row["updated_text"], new["updated_at"])
        self.comment_event(1, new, "deleted")
        self.comment_event(1, self.board(1, at=300), "edited")
        (row,) = self.rows("admission_evidence_comments")
        self.assertEqual(row["deleted"], 1)
        # An irrelevant comment is counted but its body is not kept.
        self.comment_event(1, {"id": 12, "body": "thanks!", "user": {"login": "a", "id": 1}, "updated_at": iso(5)})
        kept = {r["comment_id"]: r for r in self.rows("admission_evidence_comments")}
        self.assertEqual((kept[12]["relevant"], kept[12]["body"]), (0, ""))
        # Pull requests: an older payload is ignored; two versions with one timestamp unseed the row.
        pull = {"number": 1, "head": {"sha": head}, "base": {"ref": "main"}, "state": "open", "labels": []}
        self.deliver("pull_request", {"action": "edited", "pull_request": {**pull, "body": "b", "updated_at": iso(50)}})
        self.deliver("pull_request", {"action": "edited", "pull_request": {**pull, "body": "a", "updated_at": iso(40)}})
        self.assertEqual(self.rows("admission_evidence")[0]["body"], "b")
        cache.store.update("admission_evidence", {"repository": REPO, "number": 1}, seeded_at=1)
        self.deliver("pull_request", {"action": "edited", "pull_request": {**pull, "body": "c", "updated_at": iso(50)}})
        self.assertEqual(self.rows("admission_evidence")[0]["seeded_at"], 0)
        self.assertEqual(cache.view(1)["status"], "unseeded")

    def test_comment_count_gap_is_reported_not_evaluated(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.ready()
        self.evaluate()
        # A comment arrives on GitHub but its delivery is lost; the PR's count says so.
        self.github.review_comments[1].append({"id": 13, "body": "hello", "user": {"login": "a", "id": 1}})
        self.deliver(
            "pull_request",
            {
                "action": "labeled",
                "label": {"name": "ready"},
                "pull_request": {
                    "number": 1,
                    "head": {"sha": self.github.prs[1]["head_sha"]},
                    "base": {"ref": "main", "sha": self.repo.main_sha()},
                    "state": "open",
                    "body": self.github.prs[1]["body"],
                    "labels": [],
                    "comments": 2,
                    "updated_at": iso(self.clock() + 1),
                },
            },
        )
        self.assertEqual(self.intake.evidence.view(1)["status"], "gap")
        self.clock.advance(60)
        self.evaluate()
        self.assertEqual(self.shadow()["counts"].get("gap"), 1)
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

    def test_shadow_failure_never_blocks_admission(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.ready()

        def broken(*args, **kwargs):
            raise RuntimeError("cache unavailable")

        self.intake.shadow.compare = broken
        self.intake.evidence.apply_event = broken
        self.comment_event(1, self.board(1))
        self.evaluate()
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")

    def pull_payload(self, number=1, **fields):
        pr = self.github.prs[number]
        return {
            "number": number,
            "head": {"sha": pr["head_sha"], "ref": f"branch-{number}", "repo": {"full_name": REPO}},
            "base": {"ref": "main", "sha": self.repo.main_sha()},
            "state": "open",
            "body": pr["body"],
            "user": {"login": pr["author_login"]},
            "labels": [],
            **fields,
        }

    def test_differences_the_policy_ignores_are_still_reported(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.ready()
        self.evaluate()
        # A delivery says another author and branch; the next full read disagrees.
        self.clock.advance(60)
        payload = self.pull_payload(user={"login": "mallory"}, comments=1, updated_at=iso(self.clock()))
        self.deliver("pull_request", {"action": "edited", "pull_request": payload})
        self.clock.advance(60)
        self.evaluate()
        recent = self.shadow()["recent"][-1]
        self.assertEqual(
            (recent["outcome"], recent["fields"], recent["self_tested"]), ("mismatch", ["author"], "unavailable")
        )
        # A comment GitHub counts but the cache never saw is a gap, not a match.
        self.clock.advance(60)
        self.github.review_comments[1].append({"id": 20, "body": "nice", "user": {"login": "a", "id": 1}})
        self.evaluate()
        self.assertEqual(self.shadow()["recent"][-1]["outcome"], "mismatch")
        self.assertIn("comment_ids", self.shadow()["recent"][-1]["fields"])
        self.assertGreaterEqual(self.shadow()["with_differences"], 2)

    def test_old_deliveries_do_not_rewind_seeded_evidence(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        head = self.ready()
        self.evaluate()  # seeds build=success with no status id
        cache = self.intake.evidence
        seeded = self.rows("admission_evidence")[0]["count_updated_at"]
        # A status created well before the read arrives late: ignored.
        self.deliver(
            "status",
            {"id": 3, "sha": head, "context": "build", "state": "pending", "updated_at": iso(self.clock() - 3600)},
        )
        self.assertEqual(cache.view(1)["statuses"]["build"], "success")
        # One created around the read with another state: order unknown, never "seeded".
        self.deliver(
            "status", {"id": 4, "sha": head, "context": "build", "state": "failure", "updated_at": iso(self.clock())}
        )
        self.assertEqual(cache.view(1)["status"], "status-order-unknown")
        # One created well after the read: newer, accepted.
        self.deliver(
            "status",
            {"id": 5, "sha": head, "context": "build", "state": "failure", "updated_at": iso(self.clock() + 3600)},
        )
        self.assertEqual(cache.view(1)["statuses"]["build"], "failure")
        # An old comment delivery cannot rewind GitHub's comment count.
        self.deliver(
            "issue_comment",
            {
                "action": "edited",
                "issue": {"number": 1, "pull_request": {}, "comments": 0, "updated_at": iso(seeded - 600)},
                "comment": self.board(1, at=seeded - 600),
            },
        )
        self.assertEqual(self.rows("admission_evidence")[0]["comments_count"], 1)
        # Pushes to main are ordered by the repository's pushed_at.
        ref = f"refs/heads/{self.controller.canonical_branch}"
        self.deliver("push", {"ref": ref, "after": "b" * 40, "repository": {"full_name": REPO, "pushed_at": 200}})
        self.deliver("push", {"ref": ref, "after": "a" * 40, "repository": {"full_name": REPO, "pushed_at": 100}})
        self.assertEqual(cache.store.get_setting(cache.tip_key)["sha"], "b" * 40)
        self.deliver("push", {"ref": ref, "after": "c" * 40, "repository": {"full_name": REPO, "pushed_at": 200}})
        self.assertEqual(cache.store.get_setting(cache.tip_key)["sha"], "")  # a tie: no push tip
        self.assertIsNone(cache.view(1)["tip"])  # and a tie after the last read leaves the base unknown
        # Two comment counts with one timestamp: order unknown, so the PR needs a full read.
        at = self.rows("admission_evidence")[0]["count_updated_at"]
        self.deliver(
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": 1, "pull_request": {}, "comments": 5, "updated_at": iso(at)},
                "comment": {"id": 40, "body": "x", "user": {"login": "a", "id": 1}, "updated_at": iso(at)},
            },
        )
        self.assertEqual(cache.view(1)["status"], "unseeded")

    def test_same_second_relevance_change_unseeds(self):
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.ready()
        self.evaluate()
        board = self.github.review_comments[1][0]
        self.comment_event(1, {**board, "body": "plain words now"}, "edited")
        self.assertEqual(self.intake.evidence.view(1)["status"], "unseeded")

    def test_push_tip_against_the_last_read(self):
        from tauceti_queue.admission_evidence import EvidenceCache

        row = {"base_sha": "r" * 40, "seeded_at": 100, "seeded_until": 110}
        self.assertEqual(EvidenceCache._tip({}, row), "r" * 40)
        self.assertEqual(EvidenceCache._tip({"sha": "p" * 40, "at": 90}, row), "r" * 40)  # before the read
        self.assertIsNone(EvidenceCache._tip({"sha": "p" * 40, "at": 105}, row))  # during the read
        self.assertEqual(EvidenceCache._tip({"sha": "p" * 40, "at": 120}, row), "p" * 40)  # after it
        self.assertIsNone(EvidenceCache._tip({"sha": "", "ambiguous": True, "at": 120}, row))

    def test_events_mode_evaluates_from_the_single_read(self):
        self.controller.set_setting("admission_evidence_mode", "events", "test")
        self.ready()
        used = []
        full = self.github.admission_snapshot

        def single(repo, number, *, generation=0):
            used.append(number)
            return full(repo, number, generation=generation)

        self.github.admission_snapshot_single = single
        self.evaluate()
        self.assertEqual(used, [1])
        self.assertEqual(self.controller.input_for(REPO, 1)["state"], "waiting")
        self.controller.set_setting("admission_evidence_mode", "shadow", "test")
        self.clock.advance(60)
        self.evaluate()
        self.assertEqual(used, [1])  # shadow keeps today's read

    def test_events_mode_ignores_a_status_that_changes_nothing(self):
        head = self.ready()
        for mode, dirtied in (("shadow", True), ("events", False)):
            self.controller.set_setting("admission_evidence_mode", mode, "test")
            self.deliver("status", {"id": 1, "sha": head, "context": "build", "state": "success"})
            self.intake.run_once()
            self.assertTrue(self.intake.is_current(1, head))
            generation = self.intake.state.get(1)["generation"]
            self.deliver("status", {"id": 2, "sha": head, "context": "build", "state": "success"})
            self.assertEqual(self.intake.state.get(1)["generation"] != generation, dirtied, mode)
            self.intake.run_once()
        # A state a delivery (not the last read) installed is never treated as already evaluated.
        later = iso(self.clock() + 3600)
        self.deliver("status", {"id": 4, "sha": head, "context": "scope", "state": "failure", "updated_at": later})
        self.intake.run_once()
        generation = self.intake.state.get(1)["generation"]
        self.intake.evidence.store.update(
            "admission_evidence_statuses", {"repository": REPO, "sha": head, "context": "scope"}, status_id=4
        )
        self.deliver("status", {"id": 5, "sha": head, "context": "scope", "state": "failure", "updated_at": later})
        self.assertNotEqual(self.intake.state.get(1)["generation"], generation)
        self.intake.run_once()
        # A status that does change the state still dirties the PR in events mode.
        generation = self.intake.state.get(1)["generation"]
        self.deliver("status", {"id": 3, "sha": head, "context": "build", "state": "failure"})
        self.assertNotEqual(self.intake.state.get(1)["generation"], generation)


if __name__ == "__main__":
    unittest.main()

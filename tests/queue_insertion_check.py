"""The check before an approved head joins a batch: inert when off, holds on changed or unreadable inputs."""

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue.admission import AdmissionPolicy, AdmissionPoller
from tauceti_queue.fakes import make_controller
from tauceti_queue.ports import Status

REPO = "TauCetiProject/TauCeti"


class InsertionCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller, self.github, self.repo, self.clock = make_controller(self.tmp.name)
        self.addCleanup(lambda: self.controller.store.close())
        self.config = AdmissionPolicy(rubrics=["correctness", "reuse"], prefix="TauCeti/", allow_files=[])
        self.intake = AdmissionPoller(self.controller, self.github, REPO, self.config, clock=self.clock)

    def board(self, number=1, state="green"):
        head = self.github.prs[number]["head_sha"]
        ts = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        meta = {
            "head_sha": head,
            "merge_base_sha": self.github.pr_merge_base(REPO, number, head),
            "round": 1,
            "ts": ts,
            "mode": "commit",
            "states": {r: state for r in self.config.rubrics},
        }
        body = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->"
        self.github.review_comments[number] = [{"id": 10, "created_at": ts, "updated_at": ts, "body": body}]

    def approved(self, number=1):
        head = self.github.create_pr(number, {f"TauCeti/P{number}.lean": f"-- {number}\n"})
        self.board(number)
        for context in ("build", "scope"):
            self.github.statuses[(head, context)] = Status("success", "", "", self.clock(), None)
        self.assertEqual(self.intake.run_once()["approved"], 1)
        return head

    def reads(self):
        return [c for c in self.github.calls if c[0] == "admission_insertion_source"]

    def counter(self, name):
        return self.controller.status()["counters"].get(name, 0)

    def test_off_by_default_reads_nothing(self):
        self.assertFalse(self.controller.setting("admission_insertion_check"))
        self.approved()
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")
        self.assertEqual(self.reads(), [])

    def test_unchanged_inputs_are_inserted(self):
        self.controller.set_setting("admission_insertion_check", True, "test")
        self.approved()
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(self.counter("insertion_checks_passed"), 1)

    def test_a_revocation_whose_webhook_was_lost_never_reaches_a_batch(self):
        self.controller.set_setting("admission_insertion_check", True, "test")
        self.approved()
        self.clock.advance(60)
        self.board(state="red")  # no delivery: admission's records still say green
        self.assertTrue(self.intake.is_current(1, self.github.prs[1]["head_sha"]))
        self.controller.tick()
        row = self.controller.inputs()[0]
        self.assertEqual((row["state"], row["last_probe"]), ("waiting", "admission-recheck"))
        self.assertEqual(self.controller.store.count("members"), 0)
        self.assertEqual(self.counter("insertion_checks_held"), 1)
        self.assertEqual(len(self.controller.store.journal_entries("insertion-check-held")), 1)
        # The PR is evaluated again, and the red scoreboard withdraws it.
        self.assertFalse(self.intake.is_current(1, self.github.prs[1]["head_sha"]))
        self.intake.run_once()
        self.assertEqual(self.controller.inputs()[0]["state"], "cancelled")

    def test_an_unreadable_source_holds_the_insertion(self):
        self.controller.set_setting("admission_insertion_check", True, "test")
        self.approved()

        def unavailable(repo, number, head):
            raise RuntimeError("GitHub is down")

        self.github.admission_insertion_source = unavailable
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")
        self.assertEqual(self.counter("insertion_checks_held"), 1)
        # Once GitHub answers again and nothing changed, it is evaluated and inserted.
        del self.github.admission_insertion_source
        self.intake.run_once()
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "inserted")

    def test_a_digest_that_never_matches_stops_reading_until_the_backoff_ends(self):
        self.controller.set_setting("admission_insertion_check", True, "test")
        self.approved()
        plain, reads = self.github._admission_source, []

        def differs(repo, number, head):
            reads.append(number)
            return {**plain(number), "body": "what the check reads, never what evaluation read"}

        self.github.admission_insertion_source = differs
        self.controller.tick()  # read differs: held, and the PR evaluated again
        self.intake.run_once()  # the evaluation reads the same inputs as before
        self.controller.tick()  # same evaluation: held without another read
        self.controller.tick()
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")
        self.assertEqual(self.counter("insertion_checks_held"), 3)
        self.assertEqual(len(reads), 1)
        self.clock.advance(self.controller.setting("approval_poll_interval_seconds") + 1)
        self.controller.tick()  # the backoff ended: read again, still held
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.controller.inputs()[0]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()

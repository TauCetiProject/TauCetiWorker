#!/usr/bin/env python3
"""The cutover import of Bors's state (tauceti_queue.bors_import, Controller.import_bors_state).

Covered, over a reduced copy of the production observation feed and candidate journals
(`tests/fixtures/bors-import-sample.json`, hand-checked counts below): every member of a waiting
batch, sealed or not, becomes an approved input with its Bors admission age, reviewer and priority
when a journal knows them and the import time otherwise, with the import evidence; the members of
the running head are recorded as the external head, not as inputs; a dry run changes nothing; a
re-run reports everything present and imports nothing; closed, draft, moved-head and unknown PRs are
refused with their reason; feeds for another repository or schema are rejected; the import refuses
to start without the API budget to validate the heads. Then the first-head rule on a small
synthetic feed: the controller inserts but seals nothing while Bors's head is unlanded, and seals
once the landing is observed as Bors's squash commits on the canonical branch, as the head tip
becoming an ancestor of it, or as Bors closing the members; a re-import whose feed no longer runs
the batch (or `release_head`) releases the members to approved inputs. Finally the CLI command.
"""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import __main__ as cli  # noqa: E402
from tauceti_queue import bors_import, feeds  # noqa: E402
from tauceti_queue.controller import ControllerError  # noqa: E402
from tauceti_queue.fakes import make_controller, run_git  # noqa: E402

PROJECT = "TauCetiProject/TauCeti"
FIXTURE = json.loads((REPO / "tests/fixtures/bors-import-sample.json").read_text())

# Hand-checked against the fixture (the reduction keeps every batch of the 2026-11-04T07:45Z feed
# and at most six members of each, preferring members whose age a journal knows):
BATCHES = 19
RUNNING_BATCH = 2598
RUNNING_MEMBERS = [27473, 27022, 27000, 26270, 26491, 27049]
WAITING_SEALED, WAITING_UNSEALED = 12, 6
MEMBERS = 99
CANDIDATES = MEMBERS - len(RUNNING_MEMBERS)  # 93 members of waiting batches
WITH_AGE, WITHOUT_AGE = 79, 14
UNSEALED_BATCH = 2640  # its members: 26078 (age known), 27549, 25653, 27347, 27389, 27362 (unknown)
OLDEST = (24551, 1793605603.029097)  # original_admitted_at 1793605603029097 microseconds
SCOPE_APPROVED = 27473  # kim-em's manual approval with a merge base, a member of the running head
OBSERVED_AT = "2026-11-04T07:35:09.483467Z"


def materialize(github, fixture):
    """Create every member PR in the fake GitHub and rewrite the fixture's heads to the real ones."""
    document = copy.deepcopy(fixture)
    heads = {}
    for batch in document["observation"]["batches"]:
        for member in batch["members"]:
            real = github.create_pr(member["pr"], {f"PR{member['pr']}.lean": f"-- {member['pr']}\n"})
            heads[member["head_sha"]] = real
            member["head_sha"] = real

    def rewrite(record, key="head_sha"):
        if isinstance(record, dict) and record.get(key) in heads:
            record[key] = heads[record[key]]

    for outcome in document["observation"]["outcomes"]:
        rewrite(outcome)
    for approval in document["observation"]["scope_approvals"]:
        rewrite(approval)
    for eligible in document["observation"]["handoff"]["eligible"]:
        rewrite(eligible)
    for admission in document["candidates"]["admissions"]:
        rewrite(admission["snapshot"])
        rewrite(admission["snapshot"].get("manual_approval"))
    return document, heads


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.github, self.repo, self.clock = make_controller(self.root, publication=False)
        self.addCleanup(self.controller.store.close)

    def fetch_pr_calls(self):
        return sum(1 for name, _ in self.github.calls if name == "fetch_pr")

    def journal(self, kind):
        return self.controller.store.journal_entries(kind)


class ProductionFixture(ImportCase):
    def test_fixture_shape_matches_the_hand_checked_counts(self):
        observation, candidates = FIXTURE["observation"], FIXTURE["candidates"]
        self.assertEqual(observation["schema"], feeds.OBSERVATION_SCHEMA)
        self.assertEqual(observation["repo"], PROJECT)
        self.assertEqual(len(observation["batches"]), BATCHES)
        states = [(b["state"], b["sealed"]) for b in observation["batches"]]
        self.assertEqual(states.count(("running", True)), 1)
        self.assertEqual(states.count(("waiting", True)), WAITING_SEALED)
        self.assertEqual(states.count(("waiting", False)), WAITING_UNSEALED)
        self.assertEqual(sum(len(b["members"]) for b in observation["batches"]), MEMBERS)
        running = next(b for b in observation["batches"] if b["state"] == "running")
        self.assertEqual((running["id"], [m["pr"] for m in running["members"]]), (RUNNING_BATCH, RUNNING_MEMBERS))
        index = bors_import.admission_index(candidates)
        known = [
            m
            for b in observation["batches"]
            if b["state"] != "running"
            for m in b["members"]
            if (m["pr"], m["head_sha"]) in index
        ]
        self.assertEqual((len(known), CANDIDATES - len(known)), (WITH_AGE, WITHOUT_AGE))
        oldest = min(index.values(), key=lambda facts: facts.approved_at)
        self.assertEqual(oldest.approved_at, OLDEST[1])
        self.assertEqual(observation["handoff"]["observed_at"], OBSERVED_AT)
        self.assertLess((REPO / "tests/fixtures/bors-import-sample.json").stat().st_size, 100_000)

    def test_dry_run_then_import_then_idempotent_rerun(self):
        document, _heads = materialize(self.github, FIXTURE)
        observation, candidates = document["observation"], document["candidates"]
        before = self.fetch_pr_calls()
        now = self.clock()

        dry = self.controller.import_bors_state(observation, candidates, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["feed_observed_at"], OBSERVED_AT)
        self.assertEqual(
            dry["batches"], {"running": 1, "waiting_sealed": WAITING_SEALED, "waiting_unsealed": WAITING_UNSEALED}
        )
        self.assertEqual(
            dry["members"], {"total": MEMBERS, "external_head": len(RUNNING_MEMBERS), "candidates": CANDIDATES}
        )
        self.assertEqual((dry["imported"], dry["present"], dry["refused"]), (CANDIDATES, 0, 0))
        self.assertEqual(dry["ages"], {"from_candidates": WITH_AGE, "import_time": WITHOUT_AGE})
        self.assertEqual(
            dry["external_head"], {"batch_id": RUNNING_BATCH, "members": len(RUNNING_MEMBERS), "action": "recorded"}
        )
        self.assertEqual(self.controller.inputs(), [], "a dry run records nothing")
        self.assertIsNone(self.controller.external_head())
        self.assertEqual(self.journal("bors-import"), [])
        self.assertEqual(self.fetch_pr_calls() - before, CANDIDATES, "one PR read per candidate")

        report = self.controller.import_bors_state(observation, candidates)
        self.assertFalse(report["dry_run"])
        self.assertEqual((report["imported"], report["present"], report["refused"]), (CANDIDATES, 0, 0))
        self.assertEqual(report["ages"], dry["ages"])
        self.assertEqual(report["external_head"]["action"], "recorded")
        waiting = self.controller.inputs("waiting")
        self.assertEqual(len(waiting), CANDIDATES)
        self.assertEqual(len(self.controller.inputs()), CANDIDATES, "no member of the running head is an input")
        by_number = {row["number"]: row for row in waiting}
        self.assertNotIn(RUNNING_MEMBERS[0], by_number)
        oldest = by_number[OLDEST[0]]
        self.assertEqual(oldest["approved_at"], OLDEST[1])
        self.assertEqual(oldest["reviewer"], "tauceti-review-bot[bot]")
        evidence = json.loads(oldest["evidence"])
        self.assertEqual(
            (evidence["source"], evidence["feed_observed_at"], evidence["age_source"], evidence["reviewer_source"]),
            ("bors-import", OBSERVED_AT, "candidates", "candidates"),
        )
        self.assertIsInstance(evidence["batch_id"], int)
        unknown = by_number[27549]
        self.assertEqual(unknown["approved_at"], now, "an age no journal knows is the import time")
        unknown_evidence = json.loads(unknown["evidence"])
        self.assertEqual(
            (unknown_evidence["batch_id"], unknown_evidence["batch_state"], unknown_evidence["sealed"]),
            (UNSEALED_BATCH, "waiting", False),
        )
        self.assertEqual(
            (unknown_evidence["age_source"], unknown_evidence["reviewer_source"]), ("import-time", "assumed")
        )
        self.assertTrue(all(row["priority"] == 0 for row in waiting))
        ordered = self.controller.status()["waiting"]
        self.assertEqual(ordered["count"], CANDIDATES)
        self.assertEqual(ordered["oldest_approved_at"], OLDEST[1])

        external = self.controller.external_head()
        self.assertEqual(
            (external["batch_id"], external["state"], len(external["members"])), (RUNNING_BATCH, "running", 6)
        )
        self.assertEqual([m["pr"] for m in external["members"]], RUNNING_MEMBERS)
        self.assertEqual(external["feed_observed_at"], OBSERVED_AT)
        status = self.controller.status()["external_head"]
        self.assertEqual(
            (status["state"], status["members"], status["pending_members"]), ("running", 6, sorted(RUNNING_MEMBERS))
        )
        self.assertEqual(len(self.journal("bors-import")), 1)
        self.assertEqual(len(self.journal("approved")), CANDIDATES)

        before = self.fetch_pr_calls()
        again = self.controller.import_bors_state(observation, candidates)
        self.assertEqual((again["imported"], again["present"], again["refused"]), (0, CANDIDATES, 0))
        self.assertEqual(again["external_head"]["action"], "unchanged")
        self.assertEqual(len(self.controller.inputs()), CANDIDATES)
        self.assertEqual(len(self.journal("approved")), CANDIDATES, "nothing already present is touched")
        self.assertEqual(self.fetch_pr_calls(), before, "nothing present costs a PR read")
        self.assertIn("imported 0 approved inputs", bors_import.format_report(again))

    def test_scope_approval_travels_with_a_released_member(self):
        document, _heads = materialize(self.github, FIXTURE)
        observation, candidates = document["observation"], document["candidates"]
        report = self.controller.import_bors_state(observation, candidates, release_head=True)
        self.assertEqual(report["external_head"]["action"], "released")
        self.assertEqual(report["released_from_head"], len(RUNNING_MEMBERS))
        self.assertEqual(report["imported"], MEMBERS)
        row = self.controller.input_for(PROJECT, SCOPE_APPROVED)
        self.assertEqual(row["reviewer"], "kim-em")
        evidence = json.loads(row["evidence"])
        self.assertEqual((evidence["admin"], evidence["actor_id"]), (True, 477956))
        self.assertEqual(evidence["merge_base_sha"], "178f807996a4d882a79e4cef922aa9270c2ff11e")
        approvals = feeds.feed_source(self.controller).scope_approvals(PROJECT)
        self.assertEqual([a["pr"] for a in approvals], [SCOPE_APPROVED])
        self.assertIsNone(self.controller.external_head(), "a head released at import is never recorded")

    def test_refusals_name_the_reason_and_the_rest_is_imported(self):
        document, _heads = materialize(self.github, FIXTURE)
        observation, candidates = document["observation"], document["candidates"]
        self.github.prs[27549]["state"] = "closed"
        self.github.prs[25653]["draft"] = True
        moved = self.github.push_pr(27347, {"PR27347.lean": "-- moved\n"})
        del self.github.prs[27389]
        report = self.controller.import_bors_state(observation, candidates)
        self.assertEqual((report["imported"], report["present"], report["refused"]), (CANDIDATES - 4, 0, 4))
        refusals = {r["pr"]: r for r in report["refusals"]}
        self.assertEqual(refusals[27549]["reason"], "closed")
        self.assertEqual(refusals[25653]["reason"], "draft")
        self.assertEqual(refusals[27347]["reason"], "head-mismatch")
        self.assertTrue(refusals[27389]["reason"].startswith("fetch-failed:"))
        self.assertEqual({r["batch_id"] for r in refusals.values()}, {UNSEALED_BATCH})
        self.assertNotEqual(refusals[27347]["head_sha"], moved, "the refused record names the feed's head")
        self.assertEqual(len(self.controller.inputs()), CANDIDATES - 4)
        text = bors_import.format_report(report)
        self.assertIn("refused #27549", text)
        self.assertIn("4 refused", text)
        self.assertEqual(self.journal("approval-refused")[0]["detail"]["number"], 27347)

    def test_feed_validation_and_budget(self):
        document, _heads = materialize(self.github, FIXTURE)
        observation, candidates = document["observation"], document["candidates"]
        with self.assertRaises(ControllerError) as caught:
            self.controller.import_bors_state(dict(observation, repo="other/repo"), candidates)
        self.assertEqual(caught.exception.code, "feed-repository")
        with self.assertRaises(ControllerError) as caught:
            self.controller.import_bors_state(dict(observation, schema="tauceti-bors.observation/v2"), candidates)
        self.assertEqual(caught.exception.code, "feed-schema")
        with self.assertRaises(ControllerError) as caught:
            self.controller.import_bors_state(dict(observation, base="release"), candidates)
        self.assertEqual(caught.exception.code, "feed-base")
        self.github.remaining = 50
        with self.assertRaises(ControllerError) as caught:
            self.controller.import_bors_state(observation, candidates)
        self.assertEqual(caught.exception.code, "rate-limit")
        self.assertEqual(self.controller.inputs(), [])
        self.assertEqual(sum(1 for name, _ in self.github.calls if name == "fetch_pr"), 0)
        self.github.remaining = 4800
        self.assertEqual(self.controller.import_bors_state(observation, candidates)["imported"], CANDIDATES)

    def test_admission_index_merges_journals_and_reduces(self):
        index = bors_import.admission_index([FIXTURE["candidates"], FIXTURE["candidates"]])
        self.assertEqual(len(index), WITH_AGE + len(RUNNING_MEMBERS), "the running head's members have ages too")
        facts = index[
            (
                SCOPE_APPROVED,
                next(
                    m["head_sha"]
                    for b in FIXTURE["observation"]["batches"]
                    for m in b["members"]
                    if m["pr"] == SCOPE_APPROVED
                ),
            )
        ]
        self.assertEqual((facts.reviewer, facts.priority), ("kim-em", 0))
        self.assertEqual(facts.manual_approval["actor_id"], 477956)
        reduced = bors_import.reduce_candidates(FIXTURE["candidates"])
        self.assertEqual(reduced["candidates"], [])
        self.assertEqual(len(reduced["admissions"]), len(index))
        self.assertEqual(bors_import.admission_index(reduced), index)
        self.assertEqual(bors_import.admission_index(None), {})


def synthetic_feed(batches, *, observed_at="2026-11-04T08:00:00Z"):
    return {
        "schema": feeds.OBSERVATION_SCHEMA,
        "repo": PROJECT,
        "base": "main",
        "batch_ids": [b["id"] for b in batches],
        "batches": batches,
        "outcomes": [],
        "handoff": {"observed_at": observed_at},
        "held": [],
        "bump_repairs": [],
        "requested_batch": None,
        "scope_approvals": [],
    }


class FirstHeadRule(ImportCase):
    """Bors's running head is left to land under Bors; the service seals its first head only after that."""

    def setUp(self):
        super().setUp()
        self.heads = {n: self.github.create_pr(n, {f"P{n}.lean": f"-- {n}\n"}) for n in (1, 2, 3, 4)}
        self.staging_tip = self.repo.commit_branch(
            {"P1.lean": "-- 1\n", "P2.lean": "-- 2\n"}, "Bors staging tip", push_to="refs/heads/staging"
        )
        self.feed = synthetic_feed(
            [
                {
                    "id": 10,
                    "state": "running",
                    "sealed": True,
                    "members": [{"pr": 1, "head_sha": self.heads[1]}, {"pr": 2, "head_sha": self.heads[2]}],
                    "base": "main",
                    "base_sha": self.repo.main_sha(),
                    "head_sha": self.staging_tip,
                },
                {
                    "id": 11,
                    "state": "waiting",
                    "sealed": False,
                    "members": [{"pr": 3, "head_sha": self.heads[3]}, {"pr": 4, "head_sha": self.heads[4]}],
                    "base": "main",
                    "base_sha": self.repo.main_sha(),
                    "head_sha": None,
                },
            ]
        )

    def import_and_tick(self):
        report = self.controller.import_bors_state(self.feed, None)
        self.assertEqual((report["imported"], report["external_head"]["action"]), (2, "recorded"))
        reasons = self.controller.tick()
        members = self.controller.members(self.controller.batches("open")[0]["id"])
        self.assertEqual(sorted(m["number"] for m in members), [3, 4])
        self.assertEqual(self.controller.batches("head"), [], "nothing seals while Bors's head is unlanded")
        self.assertTrue(any("Bors's head (batch 10, 2 members, 2 not yet landed)" in r for r in reasons), reasons)
        return report

    def assert_sealed(self):
        heads = self.controller.batches("head")
        self.assertEqual(len(heads), 1)
        self.assertEqual(sorted(m["number"] for m in self.controller.members(heads[0]["id"])), [3, 4])
        self.assertEqual(self.controller.external_head()["state"], "landed")
        self.assertEqual(self.controller.status()["external_head"]["pending_members"], [])

    def test_landing_as_bors_squash_commits_on_main(self):
        self.import_and_tick()
        self.repo.advance_main({"P1.lean": "-- 1\n"}, "feat: first change (#1)")
        self.controller.tick()
        external = self.controller.external_head()
        self.assertEqual((external["state"], external["landed_members"]), ("running", [1]))
        self.assertEqual(self.controller.batches("head"), [])
        self.assertEqual(self.controller.status()["external_head"]["pending_members"], [2])
        self.repo.advance_main({"P2.lean": "-- 2\n"}, "feat: second change (#2)\n\nBody of the squash.")
        self.controller.tick()
        self.assert_sealed()
        self.assertEqual(self.controller.external_head()["landed_how"], "canonical-advance")
        progress = self.journal("external-head-progress")
        self.assertEqual([p["detail"]["landed"] for p in progress], [[1], [1, 2]])

    def test_landing_as_the_head_tip_becoming_an_ancestor_of_main(self):
        self.import_and_tick()
        run_git(self.repo.origin, "update-ref", "refs/heads/main", self.staging_tip)
        self.controller.tick()
        self.assert_sealed()
        self.assertEqual(self.controller.external_head()["landed_how"], "head-ancestor")

    def test_landing_as_bors_closing_the_members(self):
        self.import_and_tick()
        for number in (1, 2):
            self.controller.handle_event(
                "pull_request",
                {
                    "action": "closed",
                    "number": number,
                    "pull_request": {"number": number, "merged": False, "title": f"[Merged by Bors] - Change {number}"},
                },
            )
        self.assertEqual(self.controller.external_head()["landed_how"], "closed-by-bors")
        self.controller.tick()
        self.assert_sealed()

    def test_reimport_without_the_running_batch_releases_its_members(self):
        self.import_and_tick()
        later = synthetic_feed([dict(self.feed["batches"][1])], observed_at="2026-11-04T09:00:00Z")
        dry = self.controller.import_bors_state(later, None, dry_run=True)
        self.assertEqual(
            (dry["external_head"]["action"], dry["released_from_head"], dry["imported"]), ("released", 2, 2)
        )
        self.assertEqual(self.controller.external_head()["state"], "running", "a dry run releases nothing")
        report = self.controller.import_bors_state(later, None)
        self.assertEqual(
            (report["external_head"]["reason"], report["imported"], report["present"]),
            ("no longer running in the feed", 2, 2),
        )
        external = self.controller.external_head()
        self.assertEqual((external["state"], external["release_reason"]), ("released", "no longer running in the feed"))
        released = {row["number"]: row for row in self.controller.inputs("waiting")}
        self.assertEqual(sorted(released), [1, 2])
        self.assertEqual(json.loads(released[1]["evidence"])["batch_state"], "released")
        self.controller.tick()
        heads = self.controller.batches("head")
        self.assertEqual(len(heads), 1)
        self.assertEqual(sorted(m["number"] for m in self.controller.members(heads[0]["id"])), [1, 2, 3, 4])

    def test_operator_release_skips_members_that_already_landed(self):
        self.import_and_tick()
        self.repo.advance_main({"P1.lean": "-- 1\n"}, "feat: first change (#1)")
        self.github.prs[1]["state"] = "closed"
        self.controller.tick()
        report = self.controller.import_bors_state(self.feed, None, release_head=True)
        self.assertEqual((report["external_head"]["action"], report["released_from_head"]), ("released", 1))
        self.assertEqual((report["imported"], report["present"], report["refused"]), (1, 2, 0))
        self.assertEqual(sorted(row["number"] for row in self.controller.inputs("waiting")), [2])
        self.assertEqual(self.controller.external_head()["release_reason"], "operator release")
        self.controller.tick()
        self.assertEqual(len(self.controller.batches("head")), 1)


class Command(unittest.TestCase):
    def test_import_bors_posts_the_documents_and_prints_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.json"
            journal = Path(tmp) / "candidates.json"
            feed.write_text(json.dumps(FIXTURE["observation"]))
            journal.write_text(json.dumps(FIXTURE["candidates"]))
            requests = []

            def fetch(url, method="GET", body=None, token=None, timeout=10):
                requests.append((url, method, body, token, timeout))
                return {
                    "dry_run": True,
                    "repository": PROJECT,
                    "feed_observed_at": OBSERVED_AT,
                    "batches": {"running": 1, "waiting_sealed": 12, "waiting_unsealed": 6},
                    "members": {"total": 99, "external_head": 6, "candidates": 93},
                    "imported": 92,
                    "present": 0,
                    "refused": 1,
                    "refusals": [{"pr": 27549, "head_sha": "f" * 40, "batch_id": 2640, "reason": "closed"}],
                    "ages": {"from_candidates": 79, "import_time": 13},
                    "external_head": {"batch_id": 2598, "members": 6, "action": "recorded"},
                    "released_from_head": 0,
                    "api_budget": {"needed": 93, "remaining": 4700, "reset_at": 0},
                }

            args = cli.build_parser().parse_args(
                [
                    "import-bors",
                    "--feed",
                    str(feed),
                    "--candidates",
                    str(journal),
                    "--candidates",
                    str(journal),
                    "--dry-run",
                    "--url",
                    "http://q",
                    "--token",
                    "ghp_x",
                ]
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_import_bors(args, fetch=fetch), 0)
            (url, method, body, token, timeout) = requests[0]
            self.assertEqual((url, method, token), ("http://q/admin/import-bors", "POST", "ghp_x"))
            self.assertGreaterEqual(timeout, 600, "a production import takes longer than a status read")
            self.assertEqual(body["observation"]["schema"], feeds.OBSERVATION_SCHEMA)
            self.assertEqual((body["dry_run"], body["release_head"]), (True, False))
            self.assertEqual(len(body["candidates"]), 2)
            self.assertEqual(body["candidates"][0]["candidates"], [], "journals are reduced to their admissions")
            self.assertEqual(len(body["candidates"][0]["admissions"]), WITH_AGE + len(RUNNING_MEMBERS))
            text = out.getvalue()
            self.assertIn("dry run: would import 92 approved inputs (79 with their Bors age, 13 aged from now)", text)
            self.assertIn("Bors's running head: batch 2598 with 6 members, recorded", text)
            self.assertIn("refused #27549 at ffffffffffff: closed", text)

            args = cli.build_parser().parse_args(
                ["import-bors", "--feed", str(tmp) + "/missing.json", "--url", "http://q"]
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.cmd_import_bors(args, fetch=fetch), 1)
            self.assertIn("cannot read the feed", err.getvalue())


if __name__ == "__main__":
    unittest.main()

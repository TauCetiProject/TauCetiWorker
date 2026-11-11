#!/usr/bin/env python3
"""progress_shepherd: unset, a progress round does nothing to an open report; set, it dispatches the
roadmap repository's gate workflow for a report left open, a bounded number of times with the ask
recorded before it is sent, and then records the report for a person."""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_tools.forge import GITHUB, RepositoryRef

from tauceti_worker import cli, config, incidents
from tauceti_worker import progress_landing as shepherd
from tauceti_worker import progress_publication as publication
from tauceti_worker import work_units as wu
from tauceti_worker.constants import (
    PROGRESS_MERGE_WORKFLOW,
    PROGRESS_SHEPHERD_ASKS,
    PROGRESS_SHEPHERD_GRACE,
    PROGRESS_SHEPHERD_INTERVAL,
    PROGRESS_SHEPHERD_RECHECK,
    ROADMAP,
)
from tauceti_worker.github import GitHubError

T0 = 1_800_000_000
HEAD = "a" * 40
REFUSED = (
    "The automated progress gate did not merge this pull request.\n\n```\n"
    "Area was reported 1.5h ago; reports for one roadmap are at least 6h apart\n```\n\n"
    "It is left for a human to review."
)


def report(number=31, *, area="Area", opened=T0, draft=False):
    return {
        "number": number,
        "headRefName": f"progress/aaaaaaa-bbbbbbb/{area}",
        "headRefOid": HEAD,
        "createdAt": shepherd.time.strftime("%Y-%m-%dT%H:%M:%SZ", shepherd.time.gmtime(opened)),
        "isDraft": draft,
        "url": f"https://example.invalid/pull/{number}",
    }


def answer(code=0, stderr="", *, statuses=(), before_request=False):
    result = subprocess.CompletedProcess([], code, "", stderr)
    result.github_records = [{"status": status} for status in statuses]
    result.github_before_request = before_request
    # As the transport marks one `gh api` request that GitHub answered 403 or 429.
    result.github_retry_safe = bool(statuses) and all(status in (403, 429) for status in statuses)
    return result


class ProgressShepherdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(state=self.root / "state")
        self.ledger = self.cfg.state / shepherd.LEDGER
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("TAUCETI_PROGRESS_SHEPHERD", "TAUCETI_PROGRESS_STRATEGY", incidents.ENV):
            os.environ.pop(name, None)
        self.now = T0
        self.open_reports = [report()]
        self.listing = None
        self.runs = '["completed"]'
        self.comments = []
        self.result = answer()
        self.run_reads, self.dispatches, self.logged, self.warned = [], [], [], []
        self.listings = 0
        self.others = 1

        def pr_list(_self, fields, *, author=None, state="open", limit=200):
            self.listings += 1
            self.assertEqual((_self.repo, fields, author, state), (ROADMAP, shepherd.FIELDS, "fixture", "open"))
            if self.listing is not None:
                raise self.listing
            others = [{**report(1000 + n), "headRefName": f"roadmap/unrelated-{n}"} for n in range(self.others)]
            return [*others, *self.open_reports][:limit]

        def api_jq(_self, path, jq):
            self.run_reads.append(path)
            return self.runs

        def run(argv, *, env=None, input_text=None, **_kwargs):
            body = json.loads(input_text)
            recorded = json.loads(self.ledger.read_text())["reports"][body["inputs"]["pr"]]
            self.dispatches.append((argv, env.get("TAUCETI_OUTBOX_REPLAY"), body, recorded["asks"]))
            result = self.result.pop(0) if isinstance(self.result, list) else self.result
            if isinstance(result, Exception):
                raise result
            return result

        for target, attribute, value in (
            (shepherd.GitHub, "pr_list", pr_list),
            (shepherd.GitHub, "api_jq", api_jq),
            (shepherd.GitHub, "issue_comments", lambda _self, number: self.comments),
            (shepherd, "run", run),
            (shepherd, "me", lambda: "fixture"),
            (shepherd, "log", self.logged.append),
            (shepherd, "warn_red", self.warned.append),
            (shepherd.time, "time", lambda: self.now),
        ):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def look(self, at):
        self.now = T0 + at
        return shepherd.look(self.cfg)

    def entry(self, number=31):
        return json.loads(self.ledger.read_text())["reports"].get(str(number))

    def incident(self, number=31):
        name = f"progress-stuck-{incidents.subject('progress', number, ROADMAP)}.json"
        return json.loads((Path(os.environ[incidents.ENV]) / name).read_text())

    def use_incidents(self):
        os.environ[incidents.ENV] = str(self.root / "incidents")

    # ------------------------------------------------------------------ before the first ask

    def test_a_young_report_and_a_draft_cost_one_listing(self):
        self.open_reports.append(report(40, opened=T0 - 10 * PROGRESS_SHEPHERD_GRACE, draft=True))
        rows = self.look(PROGRESS_SHEPHERD_GRACE - 60)
        self.assertEqual([row["number"] for row in rows], [31, 40])
        self.assertEqual((self.run_reads, self.dispatches, self.logged), ([], [], []))
        self.assertFalse(self.ledger.exists())

    def test_an_unfinished_gate_run_is_waited_for(self):
        for self.runs, why in (('["completed", "queued"]', "unfinished"), (None, "could not be read")):
            self.ledger.unlink(missing_ok=True)
            self.run_reads.clear()
            self.look(PROGRESS_SHEPHERD_GRACE + 5)
            self.assertIn(f"repos/{ROADMAP}/actions/workflows/{PROGRESS_MERGE_WORKFLOW}/runs?", self.run_reads[0])
            self.assertIn("actor=fixture&created=%3E%3D2027-01-15T07%3A59%3A00Z", self.run_reads[0])
            self.assertIn(why, self.logged[-1])
            self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_RECHECK - 5)
            self.assertEqual(len(self.run_reads), 1, "one look per recheck interval")
            self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_RECHECK + 10)
            self.assertEqual((len(self.run_reads), self.dispatches), (2, []))
            self.assertNotIn("asks", self.entry())

    # ------------------------------------------------------------------ asking

    def test_the_ask_is_recorded_before_it_is_sent_and_spaced(self):
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        argv, replay, body, recorded = self.dispatches[0]
        self.assertEqual(
            argv[:5],
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{ROADMAP}/actions/workflows/{PROGRESS_MERGE_WORKFLOW}/dispatches",
            ],
        )
        self.assertEqual(
            (replay, body, recorded), ("1", {"ref": "main", "inputs": {"pr": "31", "wait_for_queue": True}}, 1)
        )
        self.assertIn("asked the merge gate about report #31 (Area) again (1/2)", self.logged[-1])
        self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_INTERVAL - 5)
        self.assertEqual(len(self.dispatches), 1)
        self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_INTERVAL + 10)
        self.assertEqual([d[3] for d in self.dispatches], [1, 2])
        self.assertIn("created=%3E%3D2027-01-15T09%3A59%3A05Z", self.run_reads[-1], "runs since the previous ask")

    def test_a_gate_without_the_queue_input_is_asked_without_it(self):
        self.result = [
            answer(1, 'gh: Unexpected inputs provided: ["wait_for_queue"] (HTTP 422)', statuses=[422]),
            answer(),
        ]
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertEqual(
            [d[2]["inputs"] for d in self.dispatches], [{"pr": "31", "wait_for_queue": True}, {"pr": "31"}]
        )
        self.assertEqual((self.entry()["asks"], self.warned), (1, []))
        self.assertIn("asked the merge gate about report #31 (Area) again (1/2)", self.logged[-1])

    def test_a_round_that_died_while_asking_does_not_ask_again(self):
        self.result = OSError("gh went away")
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertIn("stopped early (OSError: gh went away)", self.logged[-1])
        self.result = answer()
        self.look(PROGRESS_SHEPHERD_GRACE + 600)
        self.assertEqual((len(self.dispatches), self.entry()["asks"]), (1, 1))

    def test_a_lost_answer_counts_and_a_rate_limit_does_not(self):
        self.result = answer(1, "gh: Bad Gateway (HTTP 502)", statuses=[502])
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertEqual(self.entry()["asks"], 1)
        self.assertIn("gave no clear answer (gh: Bad Gateway (HTTP 502)); counted as asked", self.logged[-1])

        self.ledger.unlink()
        for self.result in (
            answer(1, "gh: secondary rate limit; Retry-After: 61", before_request=True),
            answer(1, "gh: API rate limit exceeded for user ID 1 (HTTP 403)", statuses=[403]),
            answer(1, "gh: Bad credentials (HTTP 401)", statuses=[401]),
        ):
            self.look(PROGRESS_SHEPHERD_GRACE + 5)
            self.assertEqual(self.entry(), {"checked_at": self.now})
            self.assertIn("could not ask the merge gate about report #31 (Area) yet", self.logged[-1])
            self.ledger.unlink()

    # ------------------------------------------------------------------ handing a report to a person

    def test_bounded_asks_then_one_incident(self):
        self.use_incidents()
        self.comments = [{"body": "unrelated"}, {"body": REFUSED}, {"body": "thanks"}]
        step = PROGRESS_SHEPHERD_INTERVAL + 60
        for n in range(PROGRESS_SHEPHERD_ASKS + 4):
            self.look(PROGRESS_SHEPHERD_GRACE + 5 + n * step)
        self.assertEqual(len(self.dispatches), PROGRESS_SHEPHERD_ASKS)
        self.assertEqual(len(self.run_reads), PROGRESS_SHEPHERD_ASKS + 1, "nothing is read once a person has it")
        value = self.incident()
        refusal = "Area was reported 1.5h ago; reports for one roadmap are at least 6h apart"
        self.assertEqual(
            {key: value[key] for key in ("kind", "key", "repository", "forge", "stage", "pr", "head", "count")},
            {
                "kind": "progress-stuck",
                "key": f"progress-31.{hashlib.sha256(RepositoryRef(GITHUB, ROADMAP).key.encode()).hexdigest()[:12]}",
                "repository": ROADMAP,
                "forge": {"kind": "github", "url": "https://github.com"},
                "stage": "progress",
                "pr": 31,
                "head": HEAD,
                "count": 1,
            },
        )
        self.assertEqual(
            (value["url"], value["area"], value["opened_at"], value["asks"], value["refusal"]),
            ("https://example.invalid/pull/31", "Area", "2027-01-15T08:00:00Z", PROGRESS_SHEPHERD_ASKS, refusal),
        )
        self.assertEqual(
            value["detail"],
            "report #31 (Area) has been open 10.0 h and the merge gate has not landed it after 2 more ask(s); "
            f"the gate's last refusal: {refusal}",
        )
        self.assertEqual(len(self.warned), 1)

    def test_without_the_directory_it_is_logged_once(self):
        step = PROGRESS_SHEPHERD_INTERVAL + 60
        for n in range(PROGRESS_SHEPHERD_ASKS + 4):
            self.look(PROGRESS_SHEPHERD_GRACE + 5 + n * step)
        self.assertEqual((len(self.dispatches), len(self.warned)), (PROGRESS_SHEPHERD_ASKS, 1))
        self.assertIn("the gate left no refusal on the pull request. Not asking again", self.warned[0])
        self.assertTrue(self.entry()["stuck"])

    def test_an_unfinished_run_holds_the_incident_back(self):
        self.use_incidents()
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_INTERVAL + 10)
        self.runs = '["in_progress"]'
        self.look(PROGRESS_SHEPHERD_GRACE + 2 * PROGRESS_SHEPHERD_INTERVAL + 20)
        self.assertEqual((incidents.records(), self.warned), ([], []))

    def test_a_rejected_dispatch_is_handed_over_at_once(self):
        self.use_incidents()
        self.result = answer(1, "gh: Resource not accessible by personal access token (HTTP 403)", statuses=[403])
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        value = self.incident()
        self.assertEqual(value["asks"], 0)
        self.assertIn(
            f"this account could not dispatch {PROGRESS_MERGE_WORKFLOW} "
            "(gh: Resource not accessible by personal access token (HTTP 403))",
            value["detail"],
        )
        self.look(PROGRESS_SHEPHERD_GRACE + 3 * PROGRESS_SHEPHERD_INTERVAL)
        self.assertEqual((len(self.dispatches), len(self.warned)), (1, 1))

    def test_a_record_another_worker_wrote_ends_the_asks(self):
        self.use_incidents()
        name = incidents.note_progress_stuck(ROADMAP, 31, HEAD) and f"progress-stuck-{self.incident()['key']}.json"
        acked = Path(os.environ[incidents.ENV]) / incidents.ACKED
        acked.mkdir()
        os.replace(acked.parent / name, acked / name)
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertEqual((self.dispatches, self.run_reads, self.warned), ([], [], []))
        self.assertEqual(self.entry(), {"stuck": True})

    def test_a_record_for_another_repository_does_not(self):
        self.use_incidents()
        for repository in (incidents.TAUCETI, "elsewhere/TauCetiRoadmap"):
            self.assertTrue(incidents.note_progress_stuck(repository, 31, HEAD))
        self.assertFalse(incidents.progress_stuck(ROADMAP, 31))
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.entry()["asks"], 1)

    def test_a_failed_record_is_written_again_later(self):
        self.use_incidents()
        (self.root / "incidents").write_text("a file where the directory should be")
        self.ledger.parent.mkdir(parents=True)
        asked = {"asks": PROGRESS_SHEPHERD_ASKS, "asked_at": T0}
        self.ledger.write_text(json.dumps({"reports": {"31": asked}}))
        with patch.object(incidents, "log"):
            self.look(PROGRESS_SHEPHERD_INTERVAL + 5)
        self.assertEqual((self.entry(), self.warned), ({**asked, "checked_at": self.now}, []))

    # ------------------------------------------------------------------ the ledger and the listing

    def test_a_closed_report_leaves_the_ledger(self):
        self.open_reports.append(report(7, area="Other", opened=T0 - 60))
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.assertEqual([d[2]["inputs"]["pr"] for d in self.dispatches], ["7", "31"])
        self.open_reports.pop()
        self.look(PROGRESS_SHEPHERD_GRACE + 60)
        self.assertEqual(list(json.loads(self.ledger.read_text())["reports"]), ["31"])

    def test_a_report_behind_other_pull_requests_is_still_asked_about(self):
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.others = 200
        rows = self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_INTERVAL + 10)
        self.assertEqual([row["number"] for row in rows], [31])
        self.assertEqual([d[3] for d in self.dispatches], [1, 2])

    def test_a_listing_cut_off_asks_nothing_and_keeps_the_ledger(self):
        self.look(PROGRESS_SHEPHERD_GRACE + 5)
        self.others = wu.PROGRESS_REPORT_SCAN
        self.assertIsNone(self.look(PROGRESS_SHEPHERD_GRACE + PROGRESS_SHEPHERD_INTERVAL + 10))
        self.assertEqual((len(self.dispatches), self.entry()["asks"]), (1, 1))
        self.assertIn(f"more than {wu.PROGRESS_REPORT_SCAN} open pull requests", self.logged[-1])

    def test_an_unreadable_ledger_asks_about_nothing(self):
        self.ledger.parent.mkdir(parents=True)
        for content in ("{", '{"reports": {"31": {"asks": "many"}}}'):
            self.ledger.write_text(content)
            self.assertEqual(len(self.look(PROGRESS_SHEPHERD_GRACE + 5)), 1)
            self.assertEqual((self.dispatches, self.ledger.read_text()), ([], content))
            self.assertIn("delete it to start the count again", self.logged[-1])

    def test_an_unreadable_listing_asks_about_nothing(self):
        self.listing = GitHubError("gh pr list failed: HTTP 502")
        self.assertIsNone(self.look(PROGRESS_SHEPHERD_GRACE + 5))
        self.assertEqual(self.dispatches, [])
        self.assertFalse(self.ledger.exists())

    # ------------------------------------------------------------------ the progress round and the setting

    def round(self, *, retained=None):
        counters = SimpleNamespace(read=lambda key: 0, write=lambda key, value: None)
        worker = SimpleNamespace(cfg=self.cfg, counters=counters)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(publication, "retained", return_value=retained))
            stack.enter_context(patch.object(wu, "me", return_value="fixture"))
            stack.enter_context(patch.object(wu, "log"))
            return wu._do_progress_inner(worker, SimpleNamespace())

    def test_unset_a_round_never_looks(self):
        self.assertFalse(config.progress_shepherd())
        with patch.object(shepherd, "look", side_effect=AssertionError("looked")):
            self.assertIsNone(self.round(retained={"actor": "another"}))

    def test_set_a_round_looks_first_and_the_limit_counts_that_listing(self):
        os.environ.update(TAUCETI_PROGRESS_SHEPHERD="1", TAUCETI_PROGRESS_STRATEGY="threshold")
        self.open_reports.append(report(7, area="Other", opened=T0 - 60))
        self.now = T0 + PROGRESS_SHEPHERD_GRACE + 5
        self.assertIsNone(self.round(), "held at the default limit of two")
        self.assertEqual((len(self.dispatches), self.listings), (2, 1))
        self.assertIsNone(self.round(retained={"actor": "another"}))
        self.assertEqual(self.listings, 2, "a round with a prepared report looks as well")

    def test_setting_and_flag(self):
        for raw, expected in (("true", True), (" ON ", True), ("0", False), ("", False)):
            os.environ["TAUCETI_PROGRESS_SHEPHERD"] = raw
            self.assertEqual(config.progress_shepherd(), expected, raw)
        parser = cli.build_parser()
        self.assertIs(parser.parse_args(["work", "--progress-shepherd"]).progress_shepherd, True)
        self.assertIsNone(parser.parse_args(["work"]).progress_shepherd)


if __name__ == "__main__":
    unittest.main()

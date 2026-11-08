#!/usr/bin/env python3
"""Quota pauses retain stage work and reach the driver without failed-round backoff."""

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
from tauceti_worker import cli, loop, work_units
from tauceti_worker import github_transport as transport
from tauceti_worker.config import Die, GitHubUnavailable, NoProgress
from tauceti_worker.constants import EX_GITHUB_WAIT, EX_NOPROGRESS


class GitHubPauseTests(unittest.TestCase):
    def test_stage_preserves_accrued_attempts_and_propagates_reset(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "fixture", "TAUCETI_WORKER_ID": "fixture"}
            ),
        ):
            values = {"fix-7-abc": 3}
            counters = SimpleNamespace(
                read=lambda key: values[key], write=lambda key, value: values.__setitem__(key, value)
            )
            worker = SimpleNamespace(counters=counters)
            candidate = SimpleNamespace(pr=7, head="abc")
            reset = 2000000000

            def interrupted(*_args):
                values["fix-7-abc"] += 1
                transport.record_block("core", reset)
                return 1

            with self.assertRaises(GitHubUnavailable) as raised:
                work_units._run_github_stage("fix", interrupted, worker, None, candidate, None, False)
            self.assertEqual(values["fix-7-abc"], 3)
            self.assertEqual(raised.exception.retry_at, reset)

    def test_unrelated_search_and_mirrored_read_quotas_do_not_gate_stages(self):
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": ""}),
            patch.object(
                transport,
                "observed_budget",
                return_value={"search": (0, 2000), "core": (10, 2000), "graphql": (10, 2000)},
            ),
        ):
            work_units._check_stage_read_quota("fix")
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid"}),
            patch.object(transport, "observed_budget", return_value={"core": (1, 2000), "graphql": (0, 2000)}),
        ):
            work_units._check_stage_read_quota("review")
        with (
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid"}),
            patch.object(transport, "observed_budget", return_value={"core": (0, 2000), "graphql": (5000, 2000)}),
            self.assertRaises(GitHubUnavailable) as waiting,
        ):
            work_units._check_stage_read_quota("review")
        self.assertEqual(waiting.exception.retry_at, 2000)

    def test_post_write_progress_bypasses_webhook_lag(self):
        from tauceti_worker.github import GitHub

        with patch("tauceti_worker.github.gh_run", return_value=SimpleNamespace(returncode=1)) as read:
            with transport.read_context(fresh=True, direct=True, retry=False):
                GitHub("org/repo")._gh(["pr", "view", "7"])
        self.assertEqual(read.call_args.kwargs["max_wait"], 0)
        with patch("tauceti_worker.github.gh_run", return_value=SimpleNamespace(returncode=1)) as read:
            with transport.read_context(fresh=True, direct=True):
                GitHub("org/repo")._gh(["pr", "view", "7"])
        self.assertNotIn("max_wait", read.call_args.kwargs)
        gh = SimpleNamespace(
            pr_progress_state=lambda _: {"head": "new" if transport.direct_reads() else "old", "ncomments": 0}
        )
        self.assertIsNone(
            work_units._progressed(SimpleNamespace(gh=gh), SimpleNamespace(pr=7), {"head": "old", "ncomments": 0})
        )
        self.assertTrue(
            work_units._progressed(
                SimpleNamespace(gh=gh), SimpleNamespace(pr=7), {"head": "old", "ncomments": 0, "published": "new"}
            )
        )
        self.assertFalse(transport.direct_reads())

    def test_claim_release_uses_accepted_reaction_id_despite_empty_mirror(self):
        from tauceti_worker.github import GitHub

        gh = GitHub("org/repo")
        with (
            patch.object(
                gh, "_gh", return_value=subprocess.CompletedProcess([], 0, json.dumps({"id": 99}), "")
            ) as send,
            patch.object(gh, "reactions", return_value=[]) as read,
        ):
            self.assertTrue(gh.add_reaction(7))
            self.assertTrue(gh.remove_reaction(7))
            self.assertIn("DELETE", send.call_args.args[0])
            self.assertTrue(send.call_args.args[0][-1].endswith("/reactions/99"))
            read.assert_not_called()

    def test_cli_quota_before_dispatch_is_a_wait_without_failure_record(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ, {"TAUCETI_GITHUB_SHARED_DIR": tmp, "GH_TOKEN": "fixture", "TAUCETI_WORKER_ID": "fixture"}
            ),
        ):

            def survey_exhausted():
                transport.record_block("graphql", 2000000000)
                raise Die("GitHub quota exhausted while surveying")

            with (
                patch.object(cli, "main", side_effect=survey_exhausted),
                patch.object(cli, "_ensure_scripts_executable"),
                patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
                patch.object(cli, "report_runtime") as status,
                patch.object(cli, "report_failure") as failure,
            ):
                self.assertEqual(cli.cli_main(), EX_GITHUB_WAIT)
                failure.assert_not_called()
                self.assertEqual(status.call_args.args, ("waiting-github",))
                self.assertEqual(status.call_args.kwargs["next_action_at"], 2000000000)
                self.assertIsNone(status.call_args.kwargs["failure_reason"])

    def test_cli_ordinary_no_progress_keeps_its_existing_classification(self):
        with (
            patch.object(cli, "main", side_effect=NoProgress("no eligible work")),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(transport, "blocking_event_since", return_value=None),
            patch.object(cli, "report_failure") as failure,
        ):
            self.assertEqual(cli.cli_main(), EX_NOPROGRESS)
            failure.assert_called_once()

    def test_retained_publication_does_not_park_unrelated_work(self):
        from tauceti_worker import github_outbox

        with (
            patch.object(github_outbox, "drain", return_value=["retained-intent"]),
            patch.object(transport, "blocking_event_since", return_value={"reset": 2000000000}),
            patch.object(cli, "configure_api", side_effect=NoProgress("reached admission")) as admit,
            patch.object(cli, "report_runtime") as status,
        ):
            with self.assertRaises(NoProgress) as raised:
                cli.cmd_work(SimpleNamespace(dry_run=False), only=["fix"], agent="claude", one_round=True)
            self.assertEqual(str(raised.exception), "reached admission")
            admit.assert_called_once()
            status.assert_called_once_with(publication_pending_count=1)

    def drive(self, codes, retry_at):
        args = cli.build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--loop"])
        waits, statuses = [], []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == len(codes):
                raise KeyboardInterrupt

        with (
            patch.dict(os.environ, {"TAUCETI_USE_BUDGET": "0", "TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid"}),
            patch.object(loop, "api_mode", return_value=True),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", side_effect=codes),
            patch.object(loop, "runtime_snapshot", return_value={"next_action_at": retry_at, "detail": "quota reset"}),
            patch.object(loop, "report_runtime", side_effect=lambda state=None, **kw: statuses.append((state, kw))),
            patch.object(loop.time, "time", return_value=1000),
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            self.assertEqual(loop.cmd_loop(args, SimpleNamespace(wid="fixture"), only=["fix"], agent="claude"), 130)
        return waits, statuses

    def test_driver_honors_reset_without_escalation(self):
        waits, statuses = self.drive([EX_GITHUB_WAIT, EX_GITHUB_WAIT], 1010)
        self.assertEqual(waits, [11, 11])
        self.assertEqual(len([state for state, _ in statuses if state == "waiting-github"]), 2)
        self.assertFalse(any(state == "backoff" for state, _ in statuses))

    def test_expired_reset_and_unknown_acknowledgement_recheck_promptly(self):
        self.assertEqual(self.drive([EX_GITHUB_WAIT], 999)[0], [1])
        self.assertEqual(self.drive([EX_GITHUB_WAIT, EX_GITHUB_WAIT], None)[0], [5, 5])

    def test_infrastructure_wait_resets_unrelated_no_progress_streak(self):
        self.assertEqual(self.drive([EX_NOPROGRESS, EX_GITHUB_WAIT, EX_NOPROGRESS], 1002)[0], [60, 3, 60])


if __name__ == "__main__":
    unittest.main()

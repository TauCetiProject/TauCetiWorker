#!/usr/bin/env python3
"""A crashed round always leaves a one-line reason, and unavailable reads do not crash rounds.

- cli_main turns an unexpected exception into a logged `round crashed: <Type>: <message>` line and a
  published failure, and still lets KeyboardInterrupt and SystemExit through.
- The loop's `round rc=N` line carries the round's published reason and log path.
- An unavailable Forgejo PR listing (tauceti_tools.forge.Incomplete) makes progress unknown rather
  than crashing the round after the agent finished.
- An unreadable roadmap board still refuses to author, but through a Die that names the cause.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_tools.forge import Incomplete  # noqa: E402

from tauceti_worker import cli, loop, work_units  # noqa: E402
from tauceti_worker import github_transport as transport  # noqa: E402
from tauceti_worker import intentions as it  # noqa: E402
from tauceti_worker.config import Die  # noqa: E402
from tauceti_worker.github import GitHubError  # noqa: E402


class CliCrashTests(unittest.TestCase):
    def run_cli(self, error, argv=("tauceti", "_round")):
        logged = []
        with (
            patch.object(cli, "main", side_effect=error),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(transport, "blocking_event_since", return_value=None),
            patch.object(cli, "log", side_effect=logged.append),
            patch.object(cli, "report_failure") as failure,
            patch.object(sys, "argv", list(argv)),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            rc = cli.cli_main()
        return rc, logged, failure, stderr.getvalue()

    def test_unexpected_exception_logs_and_publishes_one_line_reason(self):
        rc, logged, failure, stderr = self.run_cli(ValueError("bad\nshape   of data"))
        self.assertEqual(rc, 1)
        reason = "round crashed: ValueError: bad shape of data"
        self.assertIn(reason, logged)
        failure.assert_called_once_with(reason, code=1)
        self.assertIn("Traceback", stderr, "the traceback stays available for debugging")

    def test_a_forge_error_names_its_type(self):
        rc, logged, _failure, _ = self.run_cli(Incomplete("Forgejo PR listing is unavailable"))
        self.assertEqual(rc, 1)
        self.assertIn("round crashed: Incomplete: Forgejo PR listing is unavailable", logged)

    def test_other_commands_are_not_labelled_rounds(self):
        _rc, logged, _failure, _ = self.run_cli(RuntimeError("boom"), argv=("tauceti", "status"))
        self.assertIn("tauceti crashed: RuntimeError: boom", logged)

    def test_interrupts_and_exits_are_not_swallowed(self):
        self.assertEqual(self.run_cli(KeyboardInterrupt())[0], 130)
        with self.assertRaises(SystemExit):
            self.run_cli(SystemExit(3))


class LoopReasonTests(unittest.TestCase):
    def drive(self, snapshot):
        args = cli.build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--loop"])
        logged = []

        def sleep(_seconds):
            raise KeyboardInterrupt

        with (
            patch.dict(os.environ, {"TAUCETI_USE_BUDGET": "0", "TAUCETI_GITHUB_STATE_URL": "https://fixture.invalid"}),
            patch.object(loop, "api_mode", return_value=True),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", return_value=1),
            patch.object(loop, "runtime_snapshot", return_value=snapshot),
            patch.object(loop, "report_runtime"),
            patch.object(loop, "log", side_effect=logged.append),
            patch.object(loop.time, "time", return_value=1000),
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            self.assertEqual(loop.cmd_loop(args, SimpleNamespace(wid="fixture"), only=["fix"], agent="claude"), 130)
        return [line for line in logged if line.startswith("round rc=1")]

    def test_backoff_line_carries_the_published_reason_and_log(self):
        lines = self.drive(
            {"failure_reason": "round crashed: ValueError: bad\nshape", "failure_log": "/logs/agent.log"}
        )
        self.assertEqual(len(lines), 1)
        self.assertTrue(
            lines[0].startswith("round rc=1: round crashed: ValueError: bad shape (log: /logs/agent.log); "),
            lines[0],
        )

    def test_backoff_line_without_a_reason_is_unchanged(self):
        lines = self.drive({})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("round rc=1; no-progress streak=1"), lines[0])


class ForgejoProgressTests(unittest.TestCase):
    def unavailable(self, *_args, **_kwargs):
        raise Incomplete("Forgejo PR listing is unavailable")

    def test_unavailable_listing_makes_progress_unknown(self):
        w = SimpleNamespace(gh=SimpleNamespace(pr_list=self.unavailable))
        roadmap = SimpleNamespace(pr=None)
        self.assertIsNone(work_units._open_pr_numbers(w))
        self.assertIsNone(work_units._progress_snapshot(w, roadmap))
        self.assertIsNone(work_units._progressed(w, roadmap, {"prs": {1, 2}}))

    def test_unavailable_pr_state_makes_progress_unknown(self):
        w = SimpleNamespace(gh=SimpleNamespace(pr_progress_state=self.unavailable))
        self.assertIsNone(work_units._progress_snapshot(w, SimpleNamespace(pr=7)))


class RoadmapHoldReadTests(unittest.TestCase):
    def test_unreadable_board_refuses_to_author_with_a_reason(self):
        class FailingGH:
            def issue_list(self, *_args, **_kwargs):
                raise GitHubError("GraphQL: API rate limit exceeded\nfor user")

        with self.assertRaises(Die) as refused:
            it.avoid_list(FailingGH(), None, True)
        self.assertEqual(
            str(refused.exception),
            f"roadmap: cannot read administrative holds on {it.ROADMAP} "
            "(GraphQL: API rate limit exceeded for user); not authoring",
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""progress_strategy, progress_max_open and progress_skip: unset is today's plan and due check;
`threshold` polls `plan` on its own timer under the account's open-report limit."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import cli, config, work_recovery
from tauceti_worker import progress_publication as publication
from tauceti_worker import work_units as wu
from tauceti_worker.config import Die
from tauceti_worker.constants import (
    EX_NOPROGRESS,
    PROGRESS_ATTEMPT_GAP,
    PROGRESS_ERROR_KEY,
    PROGRESS_FLAGS_KEY,
    PROGRESS_POLL,
    PROGRESS_TTL,
)
from tauceti_worker.github import GitHubError
from tauceti_worker.survey import progress_due

SHA = "b" * 40
PLAN = {
    "roadmap": "Area",
    "from_sha": "c" * 40,
    "to_sha": SHA,
    "prs": [1],
    "status_path": "Area/STATUS.md",
    "progress_path": "Area/PROGRESS.md",
}
FACTS = {"docs_sha": SHA, "to_sha": SHA, "declarations": [{"name": "A.t"}]}
SETTINGS = ("TAUCETI_PROGRESS_STRATEGY", "TAUCETI_PROGRESS_MAX_OPEN", "TAUCETI_PROGRESS_SKIP")
REJECTED = (
    "usage: tauceti-progress plan [-h]\ntauceti-progress plan: error: unrecognized arguments: --strategy threshold"
)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class ProgressStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "progress/work"
        self.work.mkdir(parents=True)
        self.roadmap = self.root / "progress/roadmap"
        (self.roadmap / ".git").mkdir(parents=True)
        self.table = self.root / "progress/table.json"
        self.cfg = SimpleNamespace(state=self.root, logdir=self.root / "logs", checkout=self.root / "code")
        self.values = {}
        self.counters = SimpleNamespace(
            read=lambda key: self.values.get(key, 0),
            write=lambda key, value: self.values.__setitem__(key, value),
            incr=lambda key: self.values.__setitem__(key, self.values.get(key, 0) + 1),
        )
        self.worker = SimpleNamespace(cfg=self.cfg, counters=self.counters)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in SETTINGS:
            os.environ.pop(name, None)
        self.plans, self.open_reports, self.listings = [], [], 0
        self.plan_result = lambda args: self.wrote(args)

    def wrote(self, args):
        Path(args[args.index("--out") + 1]).write_text(json.dumps(PLAN))
        return subprocess.CompletedProcess([], 0, "wrote plan", "")

    def not_due(self, next_qualifies_at, message="not due: no roadmap qualifies yet: 2 have new pull requests"):
        def result(args):
            self.assertFalse(self.table.exists(), "the previous plan's table is removed before each plan")
            if next_qualifies_at != "absent":
                self.table.write_text(
                    json.dumps({"version": 1, "chosen": None, "next_qualifies_at": next_qualifies_at})
                )
            return subprocess.CompletedProcess([], EX_NOPROGRESS, "", f"Updating registry\n{message}\n")

        return result

    def run_tool(self, stage, *args, capture=False, docs_cache=None):
        if stage == "plan":
            self.plans.append(list(args))
            return self.plan_result(args)
        Path(args[args.index("--out") + 1]).write_text(json.dumps(FACTS))
        return subprocess.CompletedProcess([], 0, "", "")

    def prepare(self):
        with patch.object(wu.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            return wu._prepare_progress(self.worker, self.run_tool, self.roadmap, self.work)

    def due(self, *, at=None):
        """The survey's verdict, with the `due` subcommand counted and answering "due"."""
        answer = subprocess.CompletedProcess([], 0, "due: cadence elapsed", "")
        with contextlib.ExitStack() as stack:
            tool = stack.enter_context(patch("tauceti_worker.survey.subprocess.run", return_value=answer))
            if at is not None:
                stack.enter_context(patch("tauceti_worker.survey.time.time", return_value=at))
            return (*progress_due(self.cfg, self.counters), tool.call_count)

    def round(self, *, retained=None, listing=None, apply=0):
        """One `_do_progress_inner`, with GitHub, git, the tool and the writer stubbed."""

        def pr_list(_self, fields, *, author=None, state="open"):
            self.listings += 1
            self.assertEqual((fields, author, state), (["number", "headRefName"], "fixture", "open"))
            if listing is not None:
                raise listing
            return self.open_reports

        def execute(args, **kwargs):
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1] in ("plan", "facts"):
                return self.run_tool(args[1], *args[2:], capture=True, docs_cache=kwargs["env"]["TAUCETI_DOCS_CACHE"])
            return subprocess.CompletedProcess(args, 0, "writer prompt", "")

        def writer(directory, *_args, **_kwargs):
            (Path(directory) / "status-body.md").write_text("status")
            (Path(directory) / "section-body.md").write_text("section")
            return 0

        with contextlib.ExitStack() as stack:
            for obj, attr, value in (
                (publication, "retained", retained),
                (publication, "parked_window", None),
                (publication, "save", {}),
                (publication, "defer", retained or {}),
                (work_recovery, "guard", SimpleNamespace(ok=True)),
                (work_recovery, "note_owner", None),
                (wu, "prepare_checkout", True),
                (wu, "fill_prompt", "prompt"),
                (wu, "me", "fixture"),
            ):
                stack.enter_context(patch.object(obj, attr, return_value=value))
            self.applied = stack.enter_context(patch.object(wu, "_apply_prepared_progress", return_value=apply))
            stack.enter_context(patch.object(wu.GitHub, "pr_list", pr_list))
            stack.enter_context(patch.object(wu, "progress_argv", side_effect=lambda _state, *args: ["tool", *args]))
            stack.enter_context(patch.object(wu.subprocess, "run", side_effect=execute))
            stack.enter_context(patch.object(wu, "run_agent_host", side_effect=writer))
            return wu._do_progress_inner(self.worker, SimpleNamespace(agent_name="fixture", work_model="fixture"))

    # ------------------------------------------------------------------ unset

    def test_unset_plans_and_asks_due_as_before(self):
        self.assertIsNotNone(self.prepare())
        self.assertEqual(
            self.plans[0][:8],
            [
                "--roadmap-dir",
                str(self.roadmap),
                "--roadmap-ref",
                "origin/main",
                "--code-dir",
                str(self.cfg.checkout),
                "--min-prs",
                "25",
            ],
        )
        self.assertEqual(self.plans[0][8], "--out")
        self.assertEqual(len(self.plans[0]), 10)
        self.assertEqual(self.due(), (True, "due: cadence elapsed", 1))
        self.assertEqual(self.round(), 0)
        self.assertEqual(self.listings, 0, "the open-report limit belongs to the threshold strategy")
        self.assertGreater(self.values["progress-attempt-ts"], 0)

    def test_unset_not_due_keeps_the_due_cache_delay(self):
        self.plan_result = self.not_due("absent")
        before = time.time()
        self.assertIsNone(self.prepare())
        self.assertAlmostEqual(self.values["progress-mechanical-retry-at"], before + PROGRESS_TTL, delta=5)
        self.assertNotIn("progress-poll-at", self.values)

    def test_unset_exit_2_is_an_ordinary_mechanical_failure(self):
        self.plan_result = lambda args: subprocess.CompletedProcess([], 2, "", REJECTED)
        with self.assertRaisesRegex(Die, "tauceti-progress plan failed"):
            self.prepare()
        self.assertNotIn(PROGRESS_FLAGS_KEY, self.values)
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)

    # ------------------------------------------------------------------ threshold: the plan and its timer

    def test_threshold_plan_flags_and_skip_list(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        os.environ["TAUCETI_PROGRESS_SKIP"] = " Beta, -Alpha ,,Beta"
        self.assertIsNotNone(self.prepare())
        args = self.plans[0]
        self.assertNotIn("--min-prs", args)
        self.assertEqual(args[args.index("--strategy") + 1], "threshold")
        self.assertEqual(args[args.index("--table") + 1], str(self.table))
        self.assertEqual(args[args.index("--label-cache") + 1], str(self.root / "progress/labels.json"))
        self.assertEqual([a for a in args if a.startswith("--skip-area")], ["--skip-area=-Alpha", "--skip-area=Beta"])
        self.assertEqual(args[-2], "--out")

    def test_skip_list_reaches_the_default_plan(self):
        os.environ["TAUCETI_PROGRESS_SKIP"] = "Beta"
        self.assertIsNotNone(self.prepare())
        self.assertEqual(self.plans[0][6:9], ["--min-prs", "25", "--skip-area=Beta"])
        self.assertNotIn("--strategy", self.plans[0])

    def test_threshold_survey_never_runs_due(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        due, reason, calls = self.due()
        self.assertTrue(due)
        self.assertIn("threshold", reason)
        self.assertEqual(calls, 0)
        # A fresh cached verdict from the default strategy is not this strategy's answer.
        cache = self.root / "cache/progress-due.json"
        cache.parent.mkdir()
        cache.write_text(json.dumps({"due": False, "reason": "not due: cadence"}))
        self.assertTrue(self.due()[0])

    def test_threshold_waits_for_the_time_the_table_names(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        now = time.time()
        for named, expected in (
            (iso(now + 1500), 1500),
            (iso(now + 10 * PROGRESS_POLL), PROGRESS_POLL),
            (iso(now - 60), PROGRESS_TTL),
            (None, PROGRESS_POLL),
            ("absent", PROGRESS_POLL),
            ("soon", PROGRESS_POLL),
        ):
            self.values.clear()
            self.values["progress-mechanical-retry-at"] = int(now) + PROGRESS_ATTEMPT_GAP
            self.table.write_text("{}")
            self.plan_result = self.not_due(named)
            self.assertIsNone(self.prepare())
            self.assertAlmostEqual(self.values["progress-poll-at"], now + expected, delta=5, msg=named)
            self.assertEqual(self.values["progress-mechanical-retry-at"], 0, "the reservation gives way to the poll")
        due, reason, calls = self.due()
        self.assertFalse(due)
        self.assertIn("no roadmap qualifies yet: 2 have new pull requests; next plan after", reason)
        self.assertNotIn("not due:", reason)
        self.assertEqual(calls, 0)
        self.assertEqual(self.due(at=self.values["progress-poll-at"] + 1)[::2], (True, 0))

    def test_threshold_keeps_the_gap_and_the_error_limit_for_failures(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.values["progress-attempt-ts"] = int(time.time()) - 60
        self.assertIn("waiting out the attempt gap", self.due()[1])
        self.values["progress-attempt-ts"] = 0
        self.values[PROGRESS_ERROR_KEY] = 3
        self.assertIn("backing off", self.due()[1])

    def test_threshold_publication_clears_the_attempt_gap(self):
        def tool(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, "https://example.invalid/pull/1", "")

        for strategy, expected in ((None, 123), ("threshold", 0)):
            os.environ.pop("TAUCETI_PROGRESS_STRATEGY", None)
            if strategy:
                os.environ["TAUCETI_PROGRESS_STRATEGY"] = strategy
            self.values["progress-attempt-ts"] = 123
            with patch.object(publication, "complete"):
                self.assertEqual(wu._apply_prepared_progress(self.worker, tool, self.roadmap, self.work, {}), 0)
            self.assertEqual(self.values["progress-attempt-ts"], expected, strategy)

    def test_threshold_refused_window_keeps_the_attempt_gap(self):
        def tool(*_args, **_kwargs):
            return subprocess.CompletedProcess([], EX_NOPROGRESS, "", "this window was already proposed and closed")

        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.values["progress-attempt-ts"] = 123
        with patch.object(publication, "complete"), self.assertRaises(wu.NoProgress):
            wu._apply_prepared_progress(self.worker, tool, self.roadmap, self.work, {})
        self.assertEqual(self.values["progress-attempt-ts"], 123)

    # ------------------------------------------------------------------ threshold: the open-report limit

    def test_threshold_holds_at_the_open_report_limit(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.open_reports = [
            {"number": 31, "headRefName": "progress/aaaaaaa-bbbbbbb/Area"},
            {"number": 12, "headRefName": "roadmap/unrelated-worker1"},
            {"number": 7, "headRefName": "progress/ccccccc-ddddddd/Other"},
        ]
        before = time.time()
        self.assertIsNone(self.round())
        self.assertEqual((self.plans, self.listings), ([], 1))
        self.assertNotIn("progress-mechanical-retry-at", self.values, "a held round reserves nothing")
        self.assertAlmostEqual(self.values["progress-poll-at"], before + PROGRESS_TTL, delta=5)
        reason = self.due()[1]
        self.assertIn("2 report(s) of this account open (#7, #31), the limit is 2", reason)

        os.environ["TAUCETI_PROGRESS_MAX_OPEN"] = "3"
        self.assertEqual(self.round(), 0)
        self.assertEqual(len(self.plans), 1)
        self.applied.assert_called_once()

    def test_threshold_does_not_plan_when_the_count_is_unknown(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.assertIsNone(self.round(listing=GitHubError("gh pr list failed: HTTP 502")))
        self.assertEqual(self.plans, [])
        self.assertIn("could not count this account's open reports (gh pr list failed: HTTP 502)", self.due()[1])

    def test_threshold_publishes_a_prepared_report_at_the_limit(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.open_reports = [{"number": n, "headRefName": f"progress/{n}/Area"} for n in (1, 2)]
        self.assertEqual(self.round(retained={"actor": "fixture", "attempts": 1}), 0)
        self.assertEqual((self.plans, self.listings), ([], 0))
        self.applied.assert_called_once()

    # ------------------------------------------------------------------ a revision without the flags

    def test_rejected_flags_fail_once_and_then_hold_the_phase(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.plan_result = lambda args: subprocess.CompletedProcess([], 2, "", REJECTED)
        with self.assertRaisesRegex(Die, "rejected the `plan` flags .* set TAUCETI_PROGRESS_REF"):
            self.round()
        self.assertEqual(len(self.plans), 1, "no replanning against a parser that cannot change")
        self.assertEqual(self.values[PROGRESS_FLAGS_KEY], 1)
        self.assertEqual(self.values.get(PROGRESS_ERROR_KEY, 0), 0)
        self.assertEqual(self.values["progress-mechanical-retry-at"], 0)
        self.assertIn("unrecognized arguments", next(self.cfg.logdir.glob("progress-plan-*.log")).read_text())
        due, reason, calls = self.due()
        self.assertEqual((due, calls), (False, 0))
        self.assertIn(f"clear state/{PROGRESS_FLAGS_KEY} to retry", reason)

        # Removing the settings returns the worker to the default strategy at once.
        del os.environ["TAUCETI_PROGRESS_STRATEGY"]
        self.assertEqual(self.due(), (True, "due: cadence elapsed", 1))
        os.environ["TAUCETI_PROGRESS_SKIP"] = "Beta"
        self.assertFalse(self.due()[0])

    def test_rejected_skip_flag_under_the_default_strategy(self):
        os.environ["TAUCETI_PROGRESS_SKIP"] = "Beta"
        answer = "tauceti-progress plan: error: unrecognized arguments: --skip-area=Beta"
        self.plan_result = lambda args: subprocess.CompletedProcess([], 2, "", answer)
        with self.assertRaisesRegex(Die, "rejected the `plan` flags"):
            self.prepare()
        self.assertEqual(self.values[PROGRESS_FLAGS_KEY], 1)

    def test_other_plan_failures_under_threshold_stay_ordinary(self):
        os.environ["TAUCETI_PROGRESS_STRATEGY"] = "threshold"
        self.plan_result = lambda args: subprocess.CompletedProcess([], 1, "", "plan: a shallow clone is refused")
        with self.assertRaisesRegex(Die, "tauceti-progress plan failed"):
            self.prepare()
        self.assertNotIn(PROGRESS_FLAGS_KEY, self.values)
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)

    # ------------------------------------------------------------------ settings

    def test_settings_readers(self):
        self.assertEqual(
            (config.progress_strategy(), config.progress_max_open(), config.progress_skip()), (None, 2, [])
        )
        os.environ.update(TAUCETI_PROGRESS_STRATEGY=" threshold ", TAUCETI_PROGRESS_MAX_OPEN="1")
        self.assertEqual((config.progress_strategy(), config.progress_max_open()), ("threshold", 1))
        os.environ["TAUCETI_PROGRESS_MAX_OPEN"] = "0"
        with self.assertRaises(ValueError):
            config.progress_max_open()

    def test_work_flags(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            ["work", "--progress-strategy", "threshold", "--progress-max-open", "1", "--progress-skip=-Alpha,Beta"]
        )
        self.assertEqual(
            (args.progress_strategy, args.progress_max_open, args.progress_skip), ("threshold", 1, "-Alpha,Beta")
        )
        plain = parser.parse_args(["work"])
        self.assertEqual((plain.progress_strategy, plain.progress_max_open, plain.progress_skip), (None, None, None))
        for bad in (["--progress-strategy", "busiest"], ["--progress-max-open", "0"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["work", *bad])


if __name__ == "__main__":
    unittest.main()

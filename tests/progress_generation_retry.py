#!/usr/bin/env python3
"""Deployment churn retries mechanical stages, preserving the window and paid-stage state."""

import contextlib
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
from tauceti_worker import cli, loop, work_recovery
from tauceti_worker import progress_publication as publication
from tauceti_worker import work_units as wu
from tauceti_worker.config import Die, DocumentationUnavailable, ProgressPreparationUnavailable
from tauceti_worker.constants import EX_DOCUMENTATION_WAIT, EX_NOPROGRESS, PROGRESS_ERROR_KEY
from tauceti_worker.survey import progress_due

OLD, NEW = "a" * 40, "b" * 40
PLAN = {
    "roadmap": "Area",
    "from_sha": "c" * 40,
    "to_sha": NEW,
    "prs": [1],
    "status_path": "Area/STATUS.md",
    "progress_path": "Area/PROGRESS.md",
}
FACTS = {"docs_sha": NEW, "to_sha": NEW, "declarations": [{"name": "A.t"}]}
LEGACY = f"facts: the documentation changed from planned commit {OLD[:7]} to {NEW[:7]} before facts were collected; replan the window"


class GenerationRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "progress/work"
        self.work.mkdir(parents=True)
        self.roadmap = self.root / "progress/roadmap"
        (self.roadmap / ".git").mkdir(parents=True)
        (self.roadmap / "PROGRESS.md").write_text("unchanged published cursor")
        self.cfg = SimpleNamespace(state=self.root, logdir=self.root / "logs", checkout=self.root / "code")
        self.values = {"progress-attempt-ts": 123, "fix-42-head": 2, PROGRESS_ERROR_KEY: 1}
        self.worker = SimpleNamespace(
            cfg=self.cfg,
            counters=SimpleNamespace(
                read=lambda key: self.values.get(key, 0),
                write=lambda key, value: self.values.__setitem__(key, value),
                incr=lambda key: self.values.__setitem__(key, self.values.get(key, 0) + 1),
            ),
        )
        self.calls, self.caches, self.timeouts = [], [], []

    def run_tool(self, stage, *args, capture=False, docs_cache=None):
        self.calls.append(stage)
        assert capture
        assert Path(docs_cache).is_dir()
        self.caches.append((stage, docs_cache))
        if stage == "plan":
            Path(args[args.index("--out") + 1]).write_text(json.dumps(PLAN))
        else:
            Path(args[args.index("--out") + 1]).write_text(json.dumps(FACTS))
        return subprocess.CompletedProcess([], 0, f"{stage} stdout", f"{stage} stderr")

    def assert_uncharged(self):
        self.assertEqual(self.values["progress-attempt-ts"], 123)
        self.assertEqual(self.values["fix-42-head"], 2)
        self.assertEqual((self.roadmap / "PROGRESS.md").read_text(), "unchanged published cursor")

    def test_churn_is_bounded_and_next_ordinary_attempt_recovers(self):
        for name in ("plan.json", "facts.json", "status-body.md", "section-body.md"):
            (self.work / name).write_text("prepared " + name)
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}

        def churning(stage, *args, **kwargs):
            proc = self.run_tool(stage, *args, **kwargs)
            return subprocess.CompletedProcess([], 76, "facts partial stdout", LEGACY) if stage == "facts" else proc

        with self.assertRaises(DocumentationUnavailable) as raised:
            wu._prepare_progress(self.worker, churning, self.roadmap, self.work)
        self.assertIn("3 attempts", str(raised.exception))
        self.assertEqual(self.calls, ["plan", "facts"] * 3)
        self.assertEqual(len({cache for _, cache in self.caches}), 3)
        for a, b in zip(self.caches[::2], self.caches[1::2], strict=True):
            self.assertEqual(a[1], b[1])
        for name, body in before.items():
            self.assertEqual((self.work / name).read_bytes(), body)
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)
        self.assert_uncharged()
        logs = list(self.cfg.logdir.glob("progress-facts-*.log"))
        self.assertEqual(len(logs), 3)
        for log in logs:
            self.assertIn(LEGACY, log.read_text())
            self.assertIn("facts partial stdout", log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn("waiting-documentation", progress_due(self.cfg, self.worker.counters)[1])
        # Dependency restoration uses the normal path; neither artifacts nor cursors are reset.
        self.calls.clear()
        with patch("tauceti_worker.survey.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "due", "")):
            with patch("tauceti_worker.survey.time.time", return_value=self.values["progress-docs-retry-at"] + 1):
                self.assertTrue(progress_due(self.cfg, self.worker.counters)[0])
        prepared = wu._prepare_progress(self.worker, self.run_tool, self.roadmap, self.work)
        self.assertEqual(json.loads(prepared[1].read_text()), FACTS)
        self.assertEqual(self.calls, ["plan", "facts"])
        self.assert_uncharged()

    def test_invalid_plan_missing_extant_module_and_fetch_failure_do_not_retry(self):
        for failed_stage, detail in (
            ("plan", "cursor is not an ancestor"),
            ("facts", "no page for extant module; site may be redeploying"),
            ("facts", "could not fetch documentation: HTTP 503"),
        ):
            self.calls.clear()

            def fail(stage, *args, failed_stage=failed_stage, detail=detail, **kwargs):
                if stage == failed_stage:
                    self.calls.append(stage)
                    return subprocess.CompletedProcess([], 1, "tool stdout", detail)
                return self.run_tool(stage, *args, **kwargs)

            with self.assertRaises(Die) as raised:
                wu._prepare_progress(self.worker, fail, self.roadmap, self.work)
            self.assertIn(detail, str(raised.exception))
            self.assertEqual(self.calls, ["plan"] if failed_stage == "plan" else ["plan", "facts"])
            self.assert_uncharged()

    def test_legacy_mismatch_and_plan_mismatch_retry_only_mechanical_work(self):
        for failed_stage, code, detail in (("facts", 1, LEGACY), ("plan", 76, "mixed probe builds")):
            self.calls.clear()
            failed = False

            def once(stage, *args, failed_stage=failed_stage, code=code, detail=detail, **kwargs):
                nonlocal failed
                if stage == failed_stage and not failed:
                    failed = True
                    self.calls.append(stage)
                    return subprocess.CompletedProcess([], code, "", detail)
                return self.run_tool(stage, *args, **kwargs)

            self.assertIsNotNone(wu._prepare_progress(self.worker, once, self.roadmap, self.work))
            self.assertEqual(
                self.calls, (["plan", "facts"] if failed_stage == "facts" else ["plan"]) + ["plan", "facts"]
            )
            self.assert_uncharged()

    def drive_inner(
        self, failures=0, fail_stage="facts", code=76, timeout_stage=None, write_bodies=True, checkout_ok=True
    ):
        remaining = failures
        options = SimpleNamespace(agent_name="fixture", work_model="fixture")

        def execute(args, **kwargs):
            nonlocal remaining
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            stage = args[1]
            self.assertLessEqual(kwargs["timeout"], 1800)
            self.timeouts.append(kwargs["timeout"])
            if stage == timeout_stage:
                raise subprocess.TimeoutExpired(args, 1800, output=b"partial stdout", stderr=b"partial stderr")
            if stage in ("plan", "facts"):
                if stage == fail_stage and remaining:
                    remaining -= 1
                    self.calls.append(stage)
                    return subprocess.CompletedProcess(
                        args, code, "mechanical stdout", LEGACY if code == 76 else "invalid mechanical input"
                    )
                return self.run_tool(
                    stage, *args[2:], capture=kwargs["capture_output"], docs_cache=kwargs["env"]["TAUCETI_DOCS_CACHE"]
                )
            self.calls.append(stage)
            return subprocess.CompletedProcess(args, 0, "writer prompt", "prompt stderr")

        def writer(*_args):
            self.calls.append("writer")
            self.assertEqual(json.loads((self.work / "plan.json").read_text())["to_sha"], NEW)
            self.assertEqual(json.loads((self.work / "facts.json").read_text())["docs_sha"], NEW)
            writer_dir = Path(_args[0])
            self.assertFalse((writer_dir / "status-body.md").exists())
            self.assertFalse((writer_dir / "section-body.md").exists())
            if write_bodies:
                (writer_dir / "status-body.md").write_text("new status")
                (writer_dir / "section-body.md").write_text("new section")
            return 0

        with contextlib.ExitStack() as stack:
            for obj, attr, value in (
                (publication, "retained", None),
                (publication, "parked_window", None),
                (publication, "save", {}),
                (publication, "defer", {}),
                (work_recovery, "guard", SimpleNamespace(ok=True)),
                (work_recovery, "note_owner", None),
                (wu, "prepare_checkout", checkout_ok),
                (wu, "_apply_prepared_progress", 0),
            ):
                stack.enter_context(patch.object(obj, attr, return_value=value))
            stack.enter_context(patch.object(wu, "progress_argv", side_effect=lambda _state, *args: ["tool", *args]))
            stack.enter_context(patch.object(wu, "fill_prompt", return_value="prompt"))
            stack.enter_context(patch.object(wu, "me", return_value="fixture"))
            stack.enter_context(patch.object(wu.subprocess, "run", side_effect=execute))
            stack.enter_context(patch.object(wu, "run_agent_host", side_effect=writer))
            return wu._do_progress_inner(self.worker, options)

    def test_one_paid_launch_after_coherent_facts(self):
        self.assertEqual(self.drive_inner(failures=2), 0)
        self.assertEqual(self.calls, ["plan", "facts"] * 3 + ["prompt", "writer"])
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)
        for stage in ("plan", "facts", "prompt"):
            logs = list(self.cfg.logdir.glob(f"progress-{stage}-*.log"))
            self.assertTrue(logs, stage)
            self.assertTrue(all("=== stdout ===" in p.read_text() and "=== stderr ===" in p.read_text() for p in logs))
        self.assertGreater(self.values["progress-attempt-ts"], 123)

    def test_writer_never_launched_on_mechanical_failure(self):
        for failures, stage, code, error in (
            (3, "facts", 76, DocumentationUnavailable),
            (1, "plan", 1, Die),
            (1, "facts", 1, Die),
        ):
            self.calls.clear()
            with self.assertRaises(error):
                self.drive_inner(failures, stage, code)
            self.assertNotIn("writer", self.calls)
            self.assertNotIn("prompt", self.calls)
            self.assert_uncharged()

    def test_same_identity_preserves_prose_and_changed_identity_archives_it(self):
        for same in (True, False):
            (self.work / "plan.json").write_text(json.dumps(PLAN if same else {**PLAN, "to_sha": OLD}))
            (self.work / "facts.json").write_text(json.dumps(FACTS))
            (self.work / "status-body.md").write_text("prior prose")
            self.assertEqual(self.drive_inner(), 0)
            archives = list((self.root / "progress/mechanical").glob("attempt-*/previous/status-body.md"))
            self.assertEqual(bool(archives), not same)
            if archives:
                self.assertEqual(archives[-1].read_text(), "prior prose")

    def test_tool_timeout_keeps_stage_and_both_partial_streams(self):
        with self.assertRaises(Die) as raised:
            self.drive_inner(timeout_stage="facts")
        self.assertIn("facts", str(raised.exception))
        self.assertIn("TimeoutExpired", str(raised.exception))
        self.assertNotIn("writer", self.calls)
        saved = list(self.cfg.logdir.glob("progress-facts-*.log"))
        self.assertEqual(len(saved), 1)
        self.assertIn("partial stdout", saved[0].read_text())
        self.assertIn("partial stderr", saved[0].read_text())
        self.assert_uncharged()

    def test_genuine_errors_have_separate_retry_spacing(self):
        for stage in ("plan", "facts", "prompt"):
            with self.assertRaises(Die):
                self.drive_inner(
                    failures=1, fail_stage=stage, code=1, timeout_stage="prompt" if stage == "prompt" else None
                )
            self.assert_uncharged()
            self.assertGreater(self.values["progress-mechanical-retry-at"], time.time() + 28000)
            with patch("tauceti_worker.survey.subprocess.run") as tool:
                self.assertFalse(progress_due(self.cfg, self.worker.counters)[0])
                tool.assert_not_called()

    def test_not_due_and_parked_windows_suppress_replanning_and_facts(self):
        for parked in (False, True):
            self.calls.clear()

            def run(stage, *args, parked=parked, **kw):
                if not parked:
                    self.calls.append(stage)
                    return subprocess.CompletedProcess([], 75, "", "not enough PRs")
                return self.run_tool(stage, *args, **kw)

            with patch.object(publication, "parked_window", return_value=self.root if parked else None):
                self.assertIsNone(wu._prepare_progress(self.worker, run, self.roadmap, self.work))
            self.assertEqual(self.calls, ["plan"])
            self.assert_uncharged()
            with patch("tauceti_worker.survey.subprocess.run") as tool:
                self.assertFalse(progress_due(self.cfg, self.worker.counters)[0])
                tool.assert_not_called()

    def test_churn_cooldown_grows_and_leaves_other_work_available(self):
        for expected in (60, 300, 900, 1800, 1800):
            with patch.object(wu.time, "time", return_value=1000):
                with self.assertRaises(DocumentationUnavailable) as raised:
                    wu._progress_documentation_wait(self.worker, "deployment changing")
                self.assertEqual(raised.exception.retry_at, 1000 + expected)
                with patch("tauceti_worker.survey.subprocess.run") as tool:
                    self.assertFalse(progress_due(self.cfg, self.worker.counters)[0])
                    tool.assert_not_called()
            self.assert_uncharged()
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)

    def test_successful_but_incoherent_facts_retry_and_missing_identity_is_fatal(self):
        remaining = 1

        def incoherent(stage, *args, **kw):
            nonlocal remaining
            proc = self.run_tool(stage, *args, **kw)
            if stage == "facts" and remaining:
                remaining -= 1
                Path(args[args.index("--out") + 1]).write_text(json.dumps({**FACTS, "docs_sha": OLD}))
            return proc

        self.assertIsNotNone(wu._prepare_progress(self.worker, incoherent, self.roadmap, self.work))
        self.assertEqual(self.calls, ["plan", "facts"] * 2)

        def missing(stage, *args, **kw):
            proc = self.run_tool(stage, *args, **kw)
            if stage == "facts":
                Path(args[args.index("--out") + 1]).write_text("{}")
            return proc

        with self.assertRaisesRegex(Die, "facts must contain"):
            wu._prepare_progress(self.worker, missing, self.roadmap, self.work)
        self.assertGreater(self.values["progress-mechanical-retry-at"], 0)
        self.assert_uncharged()

    def test_noop_writer_cannot_publish_retained_prose(self):
        (self.work / "plan.json").write_text(json.dumps(PLAN))
        (self.work / "facts.json").write_text(json.dumps(FACTS))
        for name in ("status-body.md", "section-body.md"):
            (self.work / name).write_text("retained body")
        with self.assertRaisesRegex(Die, "agent did not write"):
            self.drive_inner(write_bodies=False)
        self.assertEqual((self.work / "status-body.md").read_text(), "retained body")
        self.assertTrue((self.work / "writing.json").exists())
        with patch("tauceti_worker.progress_publication.outbox.intent_paths") as legacy:
            self.assertIsNone(publication.retained(self.cfg))
            legacy.assert_not_called()

    def test_legacy_retry_requires_distinct_facts_builds(self):
        proc = subprocess.CompletedProcess([], 1, "", LEGACY)
        self.assertFalse(wu._progress_generation_changed("plan", proc))
        self.assertTrue(wu._progress_generation_changed("facts", proc))
        proc.stderr = LEGACY.replace(NEW[:7], OLD[:7])
        self.assertFalse(wu._progress_generation_changed("facts", proc))
        proc.stderr = LEGACY.replace(NEW[:7], OLD)
        self.assertFalse(wu._progress_generation_changed("facts", proc))

    def test_documentation_wait_is_not_a_failed_activity_round(self):
        from tauceti_worker import activity

        with patch.object(activity, "emit") as emit:
            record = activity.Round("fixture")
            record.finish(DocumentationUnavailable("deployment changing"))
            self.assertIn("round_waited", [c.args[0] for c in emit.call_args_list])
            self.assertNotIn("round_finished", [c.args[0] for c in emit.call_args_list])

    def test_stage_timeout_fits_the_remaining_round_budget(self):
        with patch.dict(os.environ, {"TAUCETI_ROUND_DEADLINE": str(time.time() - 1)}):
            with self.assertRaises(ProgressPreparationUnavailable):
                self.drive_inner()
        self.assertNotIn("plan", self.calls)
        self.assert_uncharged()
        with patch.dict(os.environ, {"TAUCETI_ROUND_DEADLINE": str(time.time() + 600)}):
            self.assertEqual(self.drive_inner(), 0)
        self.assertTrue(self.timeouts)
        self.assertTrue(all(timeout <= 585 for timeout in self.timeouts))

    def test_checkout_failure_preserves_cadence_and_defers_mechanical_work(self):
        with self.assertRaisesRegex(Die, "checkout failed"):
            self.drive_inner(checkout_ok=False)
        self.assert_uncharged()
        self.assertNotIn("writer", self.calls)
        self.assertGreater(self.values["progress-mechanical-retry-at"], time.time() + 28000)
        with patch("tauceti_worker.survey.subprocess.run") as tool:
            self.assertFalse(progress_due(self.cfg, self.worker.counters)[0])
            tool.assert_not_called()

    def test_deadline_cut_timeout_is_an_uncharged_non_churn_wait(self):
        with patch.dict(os.environ, {"TAUCETI_ROUND_DEADLINE": str(time.time() + 600)}):
            with self.assertRaises(ProgressPreparationUnavailable) as raised:
                self.drive_inner(timeout_stage="facts")
        self.assertEqual(raised.exception.state, "waiting-progress-preparation")
        self.assertEqual(self.values[PROGRESS_ERROR_KEY], 1)
        self.assertEqual(self.values.get("progress-docs-waits", 0), 0)
        self.assertNotIn("writer", self.calls)
        self.assert_uncharged()
        self.assertIn("partial stdout", next(self.cfg.logdir.glob("progress-facts-*.log")).read_text())

    def test_insufficient_writer_budget_waits_before_spending_cadence(self):
        with patch.dict(os.environ, {"TAUCETI_ROUND_DEADLINE": str(time.time() + 120)}):
            with self.assertRaises(ProgressPreparationUnavailable):
                self.drive_inner()
        self.assertIn("prompt", self.calls)
        self.assertNotIn("writer", self.calls)
        self.assert_uncharged()

    def test_mechanical_traces_are_bounded_and_prepared_archives_survive(self):
        archive = self.root / "progress/mechanical/attempt-prepared/previous"
        archive.mkdir(parents=True)
        (archive / "status-body.md").write_text("prepared prose")

        def not_due(*_args, **_kw):
            return subprocess.CompletedProcess([], 75, "", "not due")

        for _ in range(25):
            self.assertIsNone(wu._prepare_progress(self.worker, not_due, self.roadmap, self.work))
        self.assertEqual(len(list(archive.parent.parent.glob("attempt-*"))), 21)
        self.assertEqual((archive / "status-body.md").read_text(), "prepared prose")
        self.assert_uncharged()

    def test_cli_and_loop_keep_documentation_wait_out_of_failure_backoff(self):
        with (
            patch.object(cli, "main", side_effect=DocumentationUnavailable("waiting-documentation")),
            patch.object(cli, "_ensure_scripts_executable"),
            patch.object(cli, "ensure_ssl_cert_file", return_value="fixture"),
            patch.object(cli, "report_runtime") as status,
            patch.object(cli, "report_failure") as failure,
        ):
            self.assertEqual(cli.cli_main(), EX_DOCUMENTATION_WAIT)
            failure.assert_not_called()
            self.assertEqual(status.call_args.args, ("waiting-documentation",))
        args = cli.build_parser().parse_args(["work", "--agent", "claude", "--claude-billing", "api", "--loop"])
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise KeyboardInterrupt

        with (
            patch.dict(os.environ, {"TAUCETI_USE_BUDGET": "0"}),
            patch.object(loop, "api_mode", return_value=True),
            patch.object(
                loop, "run_round_subprocess", side_effect=[EX_NOPROGRESS, EX_DOCUMENTATION_WAIT, EX_NOPROGRESS]
            ),
            patch.object(loop, "report_runtime"),
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            self.assertEqual(
                loop.cmd_loop(args, SimpleNamespace(wid="fixture"), only=["progress"], agent="claude"), 130
            )
        self.assertEqual(sleeps, [60, 5, 60])


if __name__ == "__main__":
    unittest.main()

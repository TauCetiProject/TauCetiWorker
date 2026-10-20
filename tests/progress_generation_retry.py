#!/usr/bin/env python3
"""Deployment churn retries mechanical stages, preserving the window and paid-stage state."""

import contextlib
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
from tauceti_worker import cli, loop, work_recovery
from tauceti_worker import progress_publication as publication
from tauceti_worker import work_units as wu
from tauceti_worker.config import Die, DocumentationUnavailable
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
        self.calls, self.caches = [], []

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

    def drive_inner(self, failures=0, fail_stage="facts", code=76, timeout_stage=None):
        remaining = failures
        options = SimpleNamespace(agent_name="fixture", work_model="fixture")

        def execute(args, **kwargs):
            nonlocal remaining
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            stage = args[1]
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
            (self.work / "status-body.md").write_text("new status")
            (self.work / "section-body.md").write_text("new section")
            return 0

        with contextlib.ExitStack() as stack:
            for obj, attr, value in (
                (publication, "retained", None),
                (publication, "parked_window", None),
                (publication, "save", {}),
                (publication, "defer", {}),
                (work_recovery, "guard", SimpleNamespace(ok=True)),
                (work_recovery, "note_owner", None),
                (wu, "prepare_checkout", True),
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
            archives = list(self.work.glob("mechanical-*/previous/status-body.md"))
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
        self.assertEqual(sleeps, [60, 60, 60])


if __name__ == "__main__":
    unittest.main()

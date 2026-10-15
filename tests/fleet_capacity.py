#!/usr/bin/env python3
"""Fleet admission fails closed and provenance never affects a mutation."""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import github_outbox, loop
from tauceti_worker.budget import BudgetError
from tauceti_worker.claude_api import ClaudeAPIContext
from tauceti_worker.fleet_capacity import admission_reason
from tauceti_worker.fleet_events import emit, mutation


class FleetTests(unittest.TestCase):
    def test_unmanaged_and_expired(self):
        self.assertIsNone(admission_reason("a", env={}))
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "policy.json"
            env = {"TAUCETI_FLEET_ADMISSION_FILE": str(p)}
            self.assertIn("unavailable", admission_reason("a", env=env))
            p.write_text(json.dumps(dict(version=1, expires_at=20, workers=["a"])))
            self.assertIsNone(admission_reason("a", env=env, now=10))
            self.assertIn("reserved", admission_reason("b", env=env, now=10))
            self.assertIn("expired", admission_reason("a", env=env, now=21))
            self.assertIn("clock", admission_reason("a", env=env, now=-20))
            for value in [
                dict(version=2, expires_at=20, workers=["a"]),
                dict(version=1, expires_at=float("nan"), workers=["a"]),
                dict(version=1, expires_at=True, workers=["a"]),
                dict(version=1, expires_at=100, workers="a"),
            ]:
                p.write_text(json.dumps(value))
                self.assertIn("unavailable", admission_reason("a", env=env, now=10))

    def test_bridge_checks_capacity_even_without_budget_and_never_launches(self):
        context = ClaudeAPIContext("worker", "roadmap")
        with (
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI has capacity priority"),
            patch.object(context.budget, "admit") as admit,
        ):
            answer = context.handle(dict(action="admit", id="a" * 32, model="test-model"))
            self.assertFalse(answer["allowed"])
            admit.assert_not_called()
            self.assertFalse(context.invocations)

    def test_admitted_context_finishes_continuations_and_budget_still_checks_each_call(self):
        context = ClaudeAPIContext("worker", "review")
        context.enabled = True
        with (
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value=None) as capacity,
            patch.object(context.budget, "admit", return_value=dict(allowed=True)) as admit,
            patch.object(context.budget, "snapshot", return_value={}),
            patch("tauceti_worker.claude_api.report_runtime"),
        ):
            self.assertTrue(context.handle(dict(action="admit", id="a" * 32, model="test"))["allowed"])
            capacity.return_value = "CI has capacity priority"
            self.assertTrue(context.handle(dict(action="admit", id="b" * 32, model="test"))["allowed"])
            self.assertEqual(capacity.call_count, 1)
            self.assertEqual(admit.call_count, 2)
            admit.return_value = dict(allowed=False, reason="waiting for funding")
            self.assertFalse(context.handle(dict(action="admit", id="c" * 32, model="test"))["allowed"])

    def test_context_start_refuses_capacity_before_setup(self):
        context = ClaudeAPIContext("worker", "review")
        with (
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"),
            patch.object(context.budget, "snapshot") as budget,
        ):
            with self.assertRaisesRegex(BudgetError, "CI priority"):
                context.start()
            budget.assert_not_called()

    def test_loop_waits_before_survey_or_round_launch(self):
        with (
            patch.object(loop, "fleet_admission_reason", return_value="CI priority"),
            patch.object(loop.time, "sleep", side_effect=RuntimeError("end-test")),
            patch.object(loop, "github_budget") as survey,
            patch.object(loop, "run_round_subprocess") as run,
            patch.object(loop, "report_runtime") as status,
        ):
            with self.assertRaisesRegex(RuntimeError, "end-test"):
                loop.cmd_loop(SimpleNamespace(), SimpleNamespace(wid="w"), only=[], agent="claude")
            survey.assert_not_called()
            run.assert_not_called()
            self.assertIn("waiting-capacity", [call.args[0] for call in status.call_args_list if call.args])

    def test_assistance_uses_mutated_target_and_reconciled_create_has_origin(self):
        with patch("tauceti_worker.fleet_events.emit") as record:
            mutation(
                dict(argv=["api", "repos/o/r/issues/8/comments"], context=dict(repository="wrong/repo", pr=7)),
                SimpleNamespace(stdout=""),
            )
            self.assertEqual(record.call_args.kwargs["repository"], "o/r")
            self.assertEqual(record.call_args.kwargs["pr"], 8)
            value = dict(argv=["pr", "create"], context=dict(repository="o/r", head="me:branch"), created=0)
            reply = dict(
                data=dict(
                    repository=dict(
                        pullRequests=dict(
                            nodes=[dict(url="https://github.com/o/r/pull/9", headRepositoryOwner=dict(login="me"))],
                            pageInfo=dict(hasNextPage=False),
                        )
                    )
                )
            )
            with patch.object(
                github_outbox.tr, "execute", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")
            ):
                self.assertTrue(github_outbox.reconcile(value, {}))
            self.assertEqual(record.call_args.args[0], "pr_created")
            self.assertEqual(record.call_args.kwargs["pr"], 9)

    def test_reporting_never_waits_for_journal_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with (
                path.open("a") as held,
                patch.dict(os.environ, TAUCETI_FLEET_EVENTS=str(path), TAUCETI_FLEET_MACHINE_ID="test"),
            ):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                emit("pr_activity", repository="o/r", pr=1)
                self.assertEqual(path.read_text(), "")

    def test_safe_push_reports_using_isolated_worker_python(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            git = directory / "git"
            git.write_text("#!/bin/sh\nexit 0\n")
            git.chmod(0o700)
            (directory / "json.py").write_text("raise RuntimeError('untrusted import')\n")
            journal = directory / "events.jsonl"
            result = subprocess.run(
                [str(Path(__file__).resolve().parents[1] / "scripts/git-safe-push"), "refs/heads/test"],
                cwd=directory,
                env={
                    **os.environ,
                    "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                    "TAUCETI_FLEET_PYTHON": sys.executable,
                    "TAUCETI_FLEET_EMITTER": str(
                        Path(__file__).resolve().parents[1] / "tauceti_worker/fleet_events.py"
                    ),
                    "TAUCETI_FLEET_EVENTS": str(journal),
                    "TAUCETI_FLEET_MACHINE_ID": "test",
                    "TAUCETI_GITHUB_PR": "42",
                    "TAUCETI_CLAIM_KEY": "",
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(journal.read_text())["pr"], 42)

    def test_events_are_structured_private_and_deduplicable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with patch.dict(os.environ, {"TAUCETI_FLEET_EVENTS": str(path), "TAUCETI_FLEET_MACHINE_ID": "host-a"}):
                request = dict(argv=["pr", "create"], context={}, created=1)
                result = SimpleNamespace(stdout="https://github.com/TauCetiProject/TauCeti/pull/123\n")
                mutation(request, result)
                mutation(request, result)
                emit("pr_activity", secret="do-not-record", repository="a/b", pr=1)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["id"], records[1]["id"])
            self.assertEqual(records[0]["pr"], 123)
            self.assertNotIn("do-not-record", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()

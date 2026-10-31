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
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import activity, agents, cli, github_outbox, loop, work_units
from tauceti_worker.claude_api import ClaudeAPIContext
from tauceti_worker.config import AdmissionUnavailable
from tauceti_worker.constants import EX_ADMISSION_WAIT
from tauceti_worker.fleet_capacity import admission_reason, admission_unit, check_launch_admission
from tauceti_worker.fleet_events import emit, mutation
from tauceti_worker.round import Claims, RoundContext
from tauceti_worker.survey import Counters
from tauceti_worker.usage_lifecycle import identity


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
                [],
                None,
                "invalid",
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
            patch("tauceti_worker.usage_supervisor.register"),
            patch("tauceti_worker.usage_supervisor.ensure_supervisor"),
            patch("tauceti_worker.claude_api.safe_message", return_value={"owner": {}}),
        ):
            self.assertTrue(
                context.handle(dict(action="admit", id="a" * 32, model="test", process=identity()))["allowed"]
            )
            capacity.return_value = "CI has capacity priority"
            self.assertTrue(
                context.handle(dict(action="admit", id="b" * 32, model="test", process=identity()))["allowed"]
            )
            self.assertEqual(capacity.call_count, 1)
            self.assertEqual(admit.call_count, 2)
            admit.return_value = dict(allowed=False, reason="waiting for funding")
            self.assertFalse(
                context.handle(dict(action="admit", id="c" * 32, model="test", process=identity()))["allowed"]
            )

    def test_context_start_refuses_capacity_before_setup(self):
        context = ClaudeAPIContext("worker", "review")
        with (
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"),
            patch.object(context.budget, "snapshot") as budget,
        ):
            with self.assertRaisesRegex(AdmissionUnavailable, "CI priority"):
                context.start()
            budget.assert_not_called()
            self.assertFalse(context.directory.exists())

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

    def test_cli_and_activity_record_wait_instead_of_failure(self):
        refusal = AdmissionUnavailable("CI has capacity priority", state="waiting-capacity")
        with (
            patch.object(cli, "main", side_effect=refusal),
            patch.object(cli, "report_runtime") as status,
            patch.object(cli, "report_failure") as failure,
        ):
            self.assertEqual(cli.cli_main(), EX_ADMISSION_WAIT)
            self.assertEqual(status.call_args.args, ("waiting-capacity",))
            self.assertEqual(status.call_args.kwargs["detail"], str(refusal))
            failure.assert_not_called()
        with patch.object(activity, "emit") as emit:
            record = activity.Round("test")
            record.select("fix")
            record.finish(refusal)
        self.assertEqual(
            [call.args[0] for call in emit.call_args_list], ["round_started", "round_selected", "round_waited"]
        )

    def test_claim_released_and_local_work_preserved_then_head_and_lease_rechecked(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, TAUCETI_CLAUDE_BILLING="subscription"):
            root = Path(tmp)
            cfg = SimpleNamespace(wid="worker", state=root / "state", checkout=root / "checkout", logdir=root / "logs")
            cfg.checkout.mkdir()
            saved = cfg.checkout / "unfinished.lean"
            saved.write_text("preserved local work")
            candidate = SimpleNamespace(pr=7, head="a" * 40, reason="", rebase_budget_key="rebase-pr-7")
            counters = Counters(cfg)
            with (
                patch("tauceti_worker.resources.RoundResources"),
                patch("tauceti_worker.round.claims_repo", return_value="o/claims"),
                patch("tauceti_worker.round.subprocess.run", return_value=SimpleNamespace(returncode=0)) as claim_cmd,
                patch.object(Claims, "start_heartbeat"),
            ):
                with self.assertRaises(AdmissionUnavailable):
                    with RoundContext(cfg, record_activity=False) as context:
                        claims = Claims(cfg, context)
                        self.assertTrue(claims.begin_branch_work(7, candidate.head, "branch", "o", "r"))
                        # The wrapper restores the attempt, and round teardown releases its lease.
                        worker = SimpleNamespace(cfg=cfg, counters=counters, claims=claims)

                        def refused(*_):
                            counters.incr("rebase-pr-7")
                            raise AdmissionUnavailable("CI priority", state="waiting-capacity")

                        work_units._run_github_stage("rebase", refused, worker, None, candidate, None, False)
                self.assertIsNone(claims.held)
                self.assertEqual(claim_cmd.call_args.args[0][1:3], ["release", "branch/7"])
            self.assertEqual(saved.read_text(), "preserved local work")
            self.assertEqual(counters.read("rebase-pr-7"), 0)
            # A new round uses fresh PR state, so a head changed during the wait cannot launch.
            worker.gh = SimpleNamespace(
                pr_view=Mock(
                    return_value=dict(
                        headRefOid="b" * 40, state="OPEN", isDraft=False, baseRefName="main", mergeable="CONFLICTING"
                    )
                )
            )
            opts = work_units.RoundOpts(
                only=["rebase"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False
            )
            with (
                patch.object(work_units.shutil, "which", return_value="claude"),
                patch.object(work_units, "_check_stage_read_quota"),
                patch.object(work_units, "do_rebase") as launch,
            ):
                self.assertIsNone(work_units.dispatch("rebase", worker, None, candidate, opts))
                launch.assert_not_called()
            # If the selected head still stands, the next round must take a new lease before launch.
            worker.gh.pr_view.return_value["headRefOid"] = candidate.head
            with (
                patch("tauceti_worker.resources.RoundResources"),
                patch("tauceti_worker.round.claims_repo", return_value="o/claims"),
                patch("tauceti_worker.round.subprocess.run", return_value=SimpleNamespace(returncode=0)) as claim_cmd,
                patch.object(Claims, "start_heartbeat"),
            ):
                with RoundContext(cfg, record_activity=False) as context:
                    worker.claims = Claims(cfg, context)
                    self.assertTrue(worker.claims.begin_branch_work(7, candidate.head, "branch", "o", "r"))
                    self.assertEqual(claim_cmd.call_args.args[0][1:3], ["acquire", "branch/7"])
            self.assertEqual(saved.read_text(), "preserved local work")

    def test_loop_race_waits_keep_specific_reason_and_never_escalate(self):
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise KeyboardInterrupt

        with (
            patch.object(loop, "api_mode", return_value=True),
            patch.dict(os.environ, TAUCETI_USE_BUDGET="0"),
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", return_value=EX_ADMISSION_WAIT),
            patch.object(loop.Budget, "refresh_waiter"),
            patch.object(loop, "runtime_snapshot", return_value=dict(state="waiting-capacity", detail="CI priority")),
            patch.object(loop, "report_runtime") as status,
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            self.assertEqual(loop.cmd_loop(SimpleNamespace(), SimpleNamespace(wid="w"), only=[], agent="claude"), 130)
        self.assertEqual(waits, [5, 5, 5])
        states = [call for call in status.call_args_list if call.args and call.args[0] == "waiting-capacity"]
        self.assertEqual(len(states), 3)
        self.assertTrue(all(call.kwargs["detail"] == "CI priority" for call in states))
        self.assertFalse(any(call.args and call.args[0] == "backoff" for call in status.call_args_list))

    def test_non_api_launch_race_and_admitted_continuation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, TAUCETI_ACTIVITY_DIR=tmp):
            with (
                admission_unit("w"),
                patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"),
            ):
                with patch.object(agents.subprocess, "Popen") as spawn:
                    with self.assertRaises(AdmissionUnavailable):
                        agents.run_agent_proc(
                            [sys.executable, "-c", "pass"],
                            env=dict(os.environ),
                            logdir=Path(tmp),
                            label="fixture",
                            provider="codex",
                        )
                    spawn.assert_not_called()
                with patch.object(agents, "run_process") as spawn:
                    with self.assertRaises(AdmissionUnavailable):
                        agents.run_to_logfile(["engine"], Path(tmp) / "review.log", "review #7")
                    spawn.assert_not_called()
            with (
                admission_unit("w"),
                patch("tauceti_worker.fleet_capacity.admission_reason", return_value=None) as policy,
            ):
                check_launch_admission()
                policy.return_value = "CI priority"
                check_launch_admission()
                self.assertEqual(policy.call_count, 1)
            # The following selected unit must recheck; admission cannot leak across rounds.
            with (
                admission_unit("w"),
                patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"),
            ):
                with self.assertRaises(AdmissionUnavailable):
                    check_launch_admission()

    def test_dispatch_refuses_capacity_before_stage_or_claim(self):
        cfg = SimpleNamespace(wid="w")
        worker = SimpleNamespace(cfg=cfg)
        candidate = SimpleNamespace(pr=0, head="", reason="")
        opts = work_units.RoundOpts(
            only=["roadmap"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False
        )
        with (
            patch.dict(os.environ, TAUCETI_FLEET_ADMISSION_FILE="fixture"),
            patch.object(work_units.shutil, "which", return_value="claude"),
            patch.object(work_units, "_check_stage_read_quota"),
            patch.object(work_units, "_still_actionable", return_value=True),
            patch("tauceti_worker.fleet_capacity.admission_reason", return_value="CI priority"),
            patch.object(work_units, "do_roadmap") as stage,
        ):
            with self.assertRaisesRegex(AdmissionUnavailable, "CI priority"):
                work_units.dispatch("roadmap", worker, None, candidate, opts)
            stage.assert_not_called()

    def test_admission_wait_preserves_failure_streak_and_ignores_stale_running_detail(self):
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise KeyboardInterrupt

        with (
            patch.object(loop, "api_mode", return_value=True),
            patch.dict(os.environ, TAUCETI_USE_BUDGET="0"),
            patch.object(loop, "fleet_admission_reason", return_value=None),
            patch.object(loop, "github_budget", return_value={}),
            patch.object(loop, "run_round_subprocess", side_effect=[1, EX_ADMISSION_WAIT, 1]),
            patch.object(loop.Budget, "refresh_waiter"),
            patch.object(loop, "runtime_snapshot", return_value=dict(state="running", detail="provider=claude")),
            patch.object(loop, "report_runtime") as status,
            patch.object(loop.time, "sleep", side_effect=sleep),
        ):
            loop.cmd_loop(SimpleNamespace(), SimpleNamespace(wid="w"), only=[], agent="claude")
        self.assertEqual(waits, [loop.BACKOFF_BASE * 2, 5, loop.BACKOFF_BASE * 4])
        wait = next(call for call in status.call_args_list if call.args and call.args[0] == "waiting-budget")
        self.assertEqual(wait.kwargs["detail"], "rechecking local API admission")

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
            git.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                "  'rev-parse --verify'*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;\n"
                "  mktag) cat >/dev/null; echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;\n"
                "  '-c push.followTags=false push'*) printf '*\\taaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/test\\t[new branch]\\n' ;;\n"
                "esac\nexit 0\n"
            )
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

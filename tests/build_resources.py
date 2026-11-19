"""Resource failures defer retained work; queue ownership cancels existing agents."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import build_resources as b
from tauceti_worker import verification_resources as v


class Resources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = SimpleNamespace(cfg=SimpleNamespace(state=self.root), gh=SimpleNamespace(repo="o/r"), claims=None)
        self.candidate = SimpleNamespace(pr=10)

    def test_resource_wait_refunds_content_attempts_after_the_agent_has_run(self):
        from tauceti_worker.config import NoProgress
        from tauceti_worker.work_units import _run_github_stage

        values = {"ci-10-abcdef": 1, "ci-pr-10": 2}
        self.worker.counters = SimpleNamespace(
            read=lambda k: values.get(k, 0), write=lambda k, v: values.update({k: v})
        )
        candidate = SimpleNamespace(pr=10, head="abcdef")

        def fail(*args):
            values.update({"ci-10-abcdef": 2, "ci-pr-10": 3})
            raise b.ResourceUnavailable("thread exhaustion")

        with patch("tauceti_worker.fleet_capacity.unit_admitted", return_value=True):
            with self.assertRaises(b.ResourceUnavailable):
                _run_github_stage("fix-ci", fail, self.worker, None, candidate, None, False)
        self.assertEqual(values, {"ci-10-abcdef": 1, "ci-pr-10": 2})

        def source_fail(*args):
            values.update({"ci-10-abcdef": 2, "ci-pr-10": 3})
            raise NoProgress("unresolved source error")

        with patch("tauceti_worker.github_transport.blocked_since", return_value=False), self.assertRaises(NoProgress):
            _run_github_stage("fix-ci", source_fail, self.worker, None, candidate, None, False)
        self.assertEqual(values, {"ci-10-abcdef": 2, "ci-pr-10": 3})

    def test_queue_selection_declines_before_checkout_or_claim(self):
        from tauceti_worker.work_units import _do_fixlike

        counters = {}
        self.worker.counters = SimpleNamespace(
            read=lambda k: counters.get(k, 0), write=lambda k, v: counters.update({k: v})
        )
        sv = SimpleNamespace(
            open_prs=[SimpleNamespace(number=10, head_owner="o", head_repo="r", head_ref="b", labels=["bors-staging"])]
        )
        result = _do_fixlike(
            self.worker,
            sv,
            SimpleNamespace(pr=10, head="abc"),
            SimpleNamespace(prs=()),
            False,
            prompt_file="unused",
            label="fix",
        )
        self.assertIsNone(result)

    def test_native_verifier_applies_defaults_to_actual_child_without_mutating_parent(self):
        from tauceti_worker.verification import Verification

        verifier = Verification(self.root, self.root / "state")
        verifier.directory.mkdir(parents=True)
        env = {k: value for k, value in os.environ.items() if k not in v.DEFAULTS}
        command = [
            sys.executable,
            "-c",
            "import os; print(os.environ['LEAN_NUM_THREADS'], os.environ['RAYON_NUM_THREADS'])",
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.command("test", command, env=env), "4 1")
            self.assertEqual(
                verifier.command("test", command, env={**env, "LEAN_NUM_THREADS": "2", "RAYON_NUM_THREADS": "3"}), "2 3"
            )
        self.assertNotIn("LEAN_NUM_THREADS", env)
        self.assertNotIn("RAYON_NUM_THREADS", env)

    def test_defaults_preserve_explicit_values(self):
        self.assertEqual(v.defaults({}), {"LEAN_NUM_THREADS": "4", "RAYON_NUM_THREADS": "1"})
        self.assertEqual(v.defaults({"RAYON_NUM_THREADS": "2"})["RAYON_NUM_THREADS"], "2")

    def test_thread_failure_signals_cancellation_and_preserves_candidate(self):
        source = self.root / "candidate.lean"
        source.write_text("repaired source")
        real = subprocess.Popen

        def start(*args, **kw):
            return real(
                [
                    sys.executable,
                    "-c",
                    "print('ThreadPoolBuildError: Resource temporarily unavailable'); raise SystemExit(1)",
                ],
                **kw,
            )

        with self.assertRaises(b.ResourceUnavailable), b.MaintenanceStop(self.worker, self.candidate) as stop:
            with patch.object(v.subprocess, "Popen", start), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(v.verify_shared(["verify"]), 75)
            self.assertTrue(stop.is_set())
            with patch.object(v.subprocess, "Popen", side_effect=AssertionError("must not launch another build")):
                self.assertEqual(v.verify_shared(["verify"]), 75)
        self.assertEqual(source.read_text(), "repaired source")

    def test_source_failure_is_not_a_resource_wait(self):
        real = subprocess.Popen

        def start(*args, **kw):
            return real([sys.executable, "-c", "print('error: unsolved goals'); raise SystemExit(1)"], **kw)

        with b.MaintenanceStop(self.worker, self.candidate):
            with patch.object(v.subprocess, "Popen", start), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(v.verify_shared(["verify"]), 1)
            self.assertIsNone(v.wait_reason())

    def test_queue_ownership_and_closed_pr_stop_active_work_from_shared_state_only(self):
        for live in [{"state": "OPEN", "labels": [{"name": "bors-staging"}]}, {"state": "CLOSED"}]:
            with self.assertRaises(b.QueueOwned), b.MaintenanceStop(self.worker, self.candidate) as stop:
                with patch("tauceti_worker.github_state.read", return_value=(True, live)) as read:
                    self.assertTrue(stop.is_set())
                    self.assertTrue(stop.is_set())
                    self.assertEqual(read.call_count, 1)
                    self.assertEqual(read.call_args.args, ("o/r", "pr"))

    def journal(self, *events):
        path = self.root / "worker-events.jsonl"
        path.write_text("")
        stat = path.stat()
        with path.open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")
        return dict(round_id="round", mutation_cursor=(str(path), stat.st_ino, 0))

    def closure(self, **fields):
        event = dict(
            kind="pr_activity",
            round_id="round",
            repository="TauCetiProject/TauCeti",
            pr=10,
            activity="github_mutation",
            publication="closure",
        )
        return {**event, **fields}

    def test_own_closure_stops_the_agent_as_a_completed_round(self):
        candidate = SimpleNamespace(pr=10, progress=self.journal(self.closure()))
        with b.MaintenanceStop(self.worker, candidate) as stop, contextlib.redirect_stdout(io.StringIO()):
            with patch("tauceti_worker.github_state.read", return_value=(True, {"state": "CLOSED"})):
                self.assertTrue(stop.is_set())
        self.assertEqual(stop.outcome, b.CLOSED_BY_AGENT)

    def test_a_closure_by_anyone_else_still_stops_retained_work_as_before(self):
        for live, events in [
            ({"state": "CLOSED"}, ()),
            ({"state": "CLOSED"}, (self.closure(round_id="other round"),)),
            ({"state": "CLOSED"}, (self.closure(pr=11),)),
            ({"state": "CLOSED"}, (self.closure(publication="reply"),)),
            ({"state": "MERGED"}, (self.closure(),)),
        ]:
            candidate = SimpleNamespace(pr=10, progress=self.journal(*events))
            with self.assertRaises(b.QueueOwned), b.MaintenanceStop(self.worker, candidate) as stop:
                with patch("tauceti_worker.github_state.read", return_value=(True, live)):
                    self.assertTrue(stop.is_set())
            self.assertIsNone(stop.outcome)
        # No progress snapshot (no journal) keeps today's behaviour.
        with self.assertRaises(b.QueueOwned), b.MaintenanceStop(self.worker, self.candidate) as stop:
            with patch("tauceti_worker.github_state.read", return_value=(True, {"state": "CLOSED"})):
                self.assertTrue(stop.is_set())

    def test_round_whose_agent_closed_its_pr_completes_with_its_own_outcome(self):
        from tauceti_worker import agents
        from tauceti_worker import work_units as wu

        co = self.root / "checkout"
        co.mkdir()
        self.worker.cfg = SimpleNamespace(checkout=co, logdir=self.root / "logs", state=self.root / "state")
        self.worker.claims = SimpleNamespace(begin_branch_work=lambda *a: True)
        self.worker.rs = SimpleNamespace(bust=lambda *a: None)
        self.worker.counters = wu.Counters(self.worker.cfg)
        survey = SimpleNamespace(
            open_prs=[SimpleNamespace(number=10, head_owner="o", head_repo="r", head_ref="b", labels=[])],
            me="o",
        )
        real_run = subprocess.run

        def checkout(command, **kwargs):
            if command[:3] == ["gh", "pr", "checkout"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
            return real_run(command, **kwargs)

        def stopped(code):
            def agent(*args, cancel_event, **kwargs):
                self.assertTrue(cancel_event.is_set())
                return code

            return agent

        for events, live, code, expected in [
            ((self.closure(),), {"state": "CLOSED"}, -15, 0),
            ((self.closure(),), {"state": "CLOSED"}, -9, 0),
            # Only a stop-induced exit becomes a completion; a crash after closing stays a failure.
            ((self.closure(),), {"state": "CLOSED"}, 70, 70),
            ((), {"state": "CLOSED"}, -15, b.QueueOwned),
            # An agent that closed its PR and exited by itself gets the same outcome.
            ((self.closure(),), {"state": "OPEN", "labels": []}, None, 0),
        ]:
            agent = stopped(code) if code is not None else lambda *a, **k: 0
            candidate = SimpleNamespace(pr=10, head="a" * 40, progress=self.journal(*events))
            with (
                patch.object(wu, "prepare_checkout", return_value=True),
                patch.object(wu, "run_agent_host", agent),
                patch.object(wu, "_refund_infra_failure"),
                patch.object(agents.subprocess, "run", checkout),
                patch("tauceti_worker.github_state.read", return_value=(True, live)),
                patch("tauceti_worker.activity.observation") as observation,
                patch.dict(os.environ, TAUCETI_ROUND_WORKTREES="0"),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                run = partial(
                    wu._do_fixlike,
                    self.worker,
                    survey,
                    candidate,
                    SimpleNamespace(agent_name="Claude", work_model="claude", prs=()),
                    False,
                    prompt_file="fix-ci.md",
                    label="fix-ci",
                )
                if expected is b.QueueOwned:
                    with self.assertRaises(b.QueueOwned):
                        run()
                    observation.assert_not_called()
                    continue
                self.assertEqual(run(), expected)
            if expected:
                observation.assert_not_called()
            else:
                observation.assert_called_once_with(b.CLOSED_BY_AGENT)

    def test_unavailable_shared_state_does_not_cancel_and_explicit_queue_work_is_allowed(self):
        for available, live, explicit in [
            (False, None, False),
            (True, {"state": "OPEN", "labels": ["bors-staging"]}, True),
        ]:
            with b.MaintenanceStop(self.worker, self.candidate, explicit=explicit) as stop:
                with patch("tauceti_worker.github_state.read", return_value=(available, live)):
                    self.assertFalse(stop.is_set())


if __name__ == "__main__":
    unittest.main()

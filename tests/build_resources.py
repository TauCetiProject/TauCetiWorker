"""Resource failures defer retained work; queue ownership cancels existing agents."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
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

    def test_recovery_preserves_queue_owned_source_without_starting_verification(self):
        from tauceti_worker.repair_recovery import Block, WorkerBackend

        backend = WorkerBackend(self.worker, SimpleNamespace())
        with self.assertRaises(Block) as caught:
            backend.policy({}, {"state": "OPEN", "labels": ["bors-staging"]})
        self.assertEqual(caught.exception.code, "queue-owned")

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

"""Contradictions repair shared state without blocking discovery or charging reviews."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import github_state as state
from tauceti_worker import github_transport as transport
from tauceti_worker import review_diagnostics as diagnostics
from tauceti_worker import state_repair
from tauceti_worker import work_units as work
from tauceti_worker.config import ClaimCollision
from tauceti_worker.constants import REVIEW_HEAD_CHANGED_EXIT, TAUCETI
from tauceti_worker.survey import Candidate, Counters, Survey

HEAD, NEW = "a" * 40, "b" * 40


class StateRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(state=self.root, logdir=self.root, store_dir=self.root / "store", wid="test")
        self.counters = Counters(self.cfg)
        self.w = SimpleNamespace(
            cfg=self.cfg,
            counters=self.counters,
            gh=SimpleNamespace(),
            claims=None,
            rs=SimpleNamespace(bust=Mock(), review_rounds=lambda *_: 1),
        )

    def test_refresh_does_not_touch_shared_circuit_or_retry_after(self):
        token = self.root / "token"
        token.write_text("private-test-token")
        result = dict(
            version=1, repository=TAUCETI, operation="pr", status="unavailable", retry_at=(time.time() + 3600) * 1000
        )
        response = io.BytesIO(json.dumps(result).encode())
        previous = state.last_failure()
        with (
            patch.dict(
                os.environ,
                {"TAUCETI_GITHUB_STATE_URL": "https://state.example", "TAUCETI_GITHUB_STATE_TOKEN_FILE": str(token)},
            ),
            patch.object(state.urllib.request, "urlopen", return_value=response) as read,
            patch.object(transport, "atomic", side_effect=AssertionError("shared cooldown written")),
        ):
            failure = state.refresh_pr(TAUCETI, 7, HEAD, NEW)
        self.assertEqual(failure["status"], "unavailable")
        self.assertGreater(failure["retry_at"], time.time() + 250)
        self.assertLessEqual(failure["retry_at"], time.time() + state.MAX_RETRY_AFTER)
        self.assertIs(state.last_failure(), previous)
        self.assertIsNone(read.call_args.kwargs["timeout"])
        body = json.loads(read.call_args.args[0].data)
        self.assertEqual(body["expected_head"], HEAD)

    def test_old_server_and_timeout_are_optional_capability_failures(self):
        token = self.root / "token"
        token.write_text("private-test-token")
        for error in [
            TimeoutError(),
            *[
                urllib.error.HTTPError("https://state.example", n, "unsupported", {"Retry-After": "3600"}, None)
                for n in (400, 401, 404, 405)
            ],
        ]:
            with (
                patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_TOKEN_FILE": str(token)}),
                patch.object(state.urllib.request, "urlopen", side_effect=error),
            ):
                result = state.refresh_pr(TAUCETI, 7, HEAD)
                self.assertEqual(result["status"], "unavailable")
                self.assertLess(result["retry_at"], time.time() + 40)
            if isinstance(error, urllib.error.HTTPError):
                error.close()

    def test_only_the_affected_head_waits_and_retries_after_restart(self):
        c = Candidate(7, HEAD)
        deadline = time.time() + 3600
        with patch.object(state, "refresh_pr", return_value=dict(status="unavailable", retry_at=deadline)) as refresh:
            state_repair.repair_selected(self.w, c, NEW)
            self.assertTrue(self.w.selection_changed)
            # A new wrapper with the same normal state directory honors the deadline.
            self.w.counters = Counters(self.cfg)
            state_repair.repair_selected(self.w, c, NEW)
            self.assertEqual(refresh.call_count, 1)
            state_repair.repair_selected(self.w, Candidate(8, HEAD), NEW)
            state_repair.repair_selected(self.w, Candidate(7, NEW), HEAD)
            self.assertEqual(refresh.call_count, 3)
            with patch.object(state_repair.time, "time", return_value=deadline + 5):
                state_repair.repair_selected(self.w, c, NEW)
                self.assertEqual(refresh.call_count, 4)

    def test_review_preflight_is_direct_and_declines_changed_head(self):
        def read(*_):
            self.assertTrue(transport.direct_reads())
            return dict(headRefOid=NEW, state="OPEN", isDraft=False)

        self.w.gh = SimpleNamespace(pr_view=read)
        with patch.object(state, "refresh_pr", return_value=dict(status="ok")) as refresh:
            self.assertFalse(work._still_actionable("review", self.w, None, Candidate(7, HEAD)))
        refresh.assert_called_once_with(TAUCETI, 7, HEAD, NEW)
        self.assertEqual(self.counters.read("review-err-7"), 0)

    def test_review_command_race_never_counts_as_a_review_or_error(self):
        self.counters.write("review-err-7", 2)
        self.w.rs.review_rounds = lambda *_: 1
        c = Candidate(7, HEAD)
        with (
            patch.object(work, "run_to_logfile", return_value=REVIEW_HEAD_CHANGED_EXIT),
            patch.object(work, "me", return_value="tester"),
            patch.object(state, "refresh_pr", return_value=dict(status="ok")),
            patch.object(work, "_sync_review_outbox", side_effect=AssertionError("false review completion")),
        ):
            self.assertIsNone(work.do_review(self.w, None, c, SimpleNamespace(work_model="claude"), False))
        self.assertEqual(self.counters.read("review-err-7"), 2)
        self.assertFalse((self.root / "review-failure-7.json").exists())

    def test_refund_keeps_genuine_errors_and_resumes_after_counter_write_crash(self):
        target = self.root / "review-failure-7.json"
        target.write_text(
            json.dumps(
                dict(
                    attempts=[
                        dict(
                            category="stale-head",
                            code=1,
                            summary=f"tauceti-review: PR #7 head is {NEW[:12]}, expected {HEAD[:12]}",
                        ),
                        dict(category="review-engine"),
                        dict(
                            category="stale-head",
                            code=1,
                            summary=f"tauceti-review: PR #7 head is {NEW[:12]}, expected {HEAD[:12]}",
                        ),
                    ]
                )
            )
        )
        self.counters.write("review-err-7", 3)
        original = self.counters.write

        def interrupted(key, value):
            original(key, value)
            raise OSError("crash after counter publication")

        with patch.object(self.counters, "write", side_effect=interrupted):
            diagnostics.refund_stale_head_errors(self.root, self.counters)
        self.assertEqual(self.counters.read("review-err-7"), 1)
        self.assertIn("stale_head_refund", json.loads(target.read_text()))
        diagnostics.refund_stale_head_errors(self.root, self.counters)
        diagnostics.refund_stale_head_errors(self.root, self.counters)
        self.assertEqual(self.counters.read("review-err-7"), 1)
        self.assertEqual(json.loads(target.read_text())["attempts"], [dict(category="review-engine")])

    def test_refund_accepts_the_retained_worker_wrapper(self):
        diagnostics.record_review_failure(
            self.root,
            worker="test",
            pr=7,
            head=HEAD,
            provider="claude",
            code=1,
            reason=f"review #7 exited with status 1: tauceti-review: PR #7 head is {NEW[:12]}, expected {HEAD[:12]}.",
        )
        self.counters.write("review-err-7", 1)
        diagnostics.refund_stale_head_errors(self.root, self.counters)
        diagnostics.refund_stale_head_errors(self.root, self.counters)
        self.assertEqual(self.counters.read("review-err-7"), 0)
        self.assertEqual(diagnostics.read_review_failure(self.root, 7)["attempts"], [])

    def test_retry_deadline_uses_temporary_wait_instead_of_failure_backoff(self):
        self.counters.write(state_repair.retry_key(7, HEAD), int(time.time()) + 60)
        sv = Survey(worker_id="test")
        sv.reviewable.actionable = [Candidate(7, HEAD)]
        opts = work.RoundOpts(only=["review"], agent="claude", dry_run=False, work_model="claude", sandbox_host=True)
        with (
            patch.object(work, "mirror_creds"),
            patch.object(work, "roadmap_only", return_value="any"),
            patch.object(work, "survey", return_value=sv),
            patch.object(work, "throttle_review"),
            patch.object(work, "dispatch", side_effect=AssertionError("retried before deadline")),
            self.assertRaises(ClaimCollision),
        ):
            work.run_round(self.w, opts)

    def test_genuine_errors_with_generic_head_words_are_not_refunded(self):
        attempts = [
            dict(category="stale-head", code=1, summary="expected head field in provider response"),
            dict(category="stale-head", code=2, summary=f"tauceti-review: PR #7 head is {NEW}, expected {HEAD}"),
            dict(
                category="stale-head",
                code=1,
                summary=f"review #8 exited with status 1: tauceti-review: PR #7 head is {NEW}, expected {HEAD}",
            ),
            dict(
                category="stale-head",
                code=1,
                summary=f"review #7 exited with status 1: tauceti-review: PR #8 head is {NEW}, expected {HEAD}",
            ),
        ]
        path = self.root / "review-failure-7.json"
        path.write_text(json.dumps(dict(attempts=attempts)))
        self.counters.write("review-err-7", 2)
        diagnostics.refund_stale_head_errors(self.root, self.counters)
        self.assertEqual(self.counters.read("review-err-7"), 2)
        self.assertEqual(json.loads(path.read_text())["attempts"], attempts)

    def test_repeated_upstream_disagreement_has_a_bounded_slow_retry(self):
        with (
            patch.object(
                state,
                "refresh_pr",
                return_value=dict(status="ok", outcome="recently-verified", data=dict(headRefOid=HEAD)),
            ),
            patch.object(state_repair, "log") as log,
        ):
            state_repair.repair_selected(self.w, Candidate(7, HEAD), NEW)
        self.assertGreater(self.counters.read(state_repair.retry_key(7, HEAD)), time.time() + 250)
        self.assertIn("repeat head mismatch", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

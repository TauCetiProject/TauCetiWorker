#!/usr/bin/env python3
"""Queue source/CI separation and durable, single-send dispatch recovery."""

import concurrent.futures
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tauceti_queue import github_adapter as ga
from tauceti_queue.controller import Controller
from tauceti_queue.fakes import make_controller, reopen_controller
from tauceti_queue.feeds import ControllerFeedSource
from tauceti_queue.ports import DispatchNotAccepted

PROJECT = "TauCetiProject/TauCeti"
CI_METHODS = {
    "declaration_names",
    "dispatch_candidate",
    "find_candidate_run",
    "read_run",
    "cancel_run",
    "commit_status",
}


class RolePort:
    def __init__(self, backend, *, execution):
        self.backend = backend
        self.execution = execution
        self.calls = []

    def __getattr__(self, name):
        if (name in CI_METHODS) != self.execution:
            raise AssertionError(f"operation {name} went to the wrong backend")
        method = getattr(self.backend, name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return method(*args, **kwargs)

        return call


class Crashed(BaseException):
    pass


class Dispatches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.controller, self.backend, self.repo, self.clock = make_controller(self.root)
        self.addCleanup(self.controller.store.close)

    def approve(self):
        head = self.backend.create_pr(101, {"A.lean": "a\n"})
        self.assertTrue(self.controller.approve(PROJECT, 101, head, "reviewer")["accepted"])

    def attempt(self):
        head = self.controller.store.row("batches", {"state": "head"})
        return self.controller.store.row("ci_attempts", {"batch_id": head["id"]}, order="id DESC")

    def restart(self):
        self.controller.store.close()
        self.controller = reopen_controller(self.root, self.backend, self.repo, self.clock)
        self.addCleanup(self.controller.store.close)
        self.controller.recover()

    def prepare(self):
        self.approve()
        send = self.controller._send_prepared
        self.controller._send_prepared = lambda *_: None
        self.controller.tick()
        self.controller._send_prepared = send
        return self.attempt()

    def test_real_queue_cycle_keeps_source_operations_off_execution_port(self):
        source = RolePort(self.backend, execution=False)
        execution = RolePort(self.backend, execution=True)
        self.controller = Controller(
            self.controller.store,
            source,
            str(self.repo.clone),
            PROJECT,
            revision="separate-ports",
            clock=self.clock,
            execution=execution,
        )
        self.approve()
        self.controller.tick()
        self.assertEqual(self.attempt()["dispatch_state"], "accepted")
        self.backend.start_runs()
        self.clock.advance(600)
        self.backend.complete_runs()
        self.controller.tick()
        self.assertIsNone(self.controller.store.row("batches", {"state": "head"}))
        self.assertIn("fetch_pr", source.calls)
        self.assertIn("update_ref", source.calls)
        self.assertIn("fast_forward", source.calls)
        self.assertIn("dispatch_candidate", execution.calls)
        self.assertIn("commit_status", execution.calls)
        self.assertIs(self.controller.github, source)

    def test_accepted_dispatch_with_lost_response_survives_restart_without_resend(self):
        self.approve()
        dispatch = self.backend.dispatch_candidate

        def accepted(*args, **kwargs):
            dispatch(*args, **kwargs)
            raise OSError("response lost")

        self.backend.dispatch_candidate = accepted
        self.controller.tick()
        attempt = self.attempt()
        self.assertEqual(attempt["dispatch_state"], "uncertain")
        self.assertEqual(len(self.backend.runs), 1)
        self.restart()
        self.controller.tick()
        self.assertEqual(self.attempt()["id"], attempt["id"])
        self.assertEqual(self.attempt()["request_id"], attempt["request_id"])
        self.assertEqual(self.attempt()["run_id"], self.backend.runs[0].run_id)
        self.assertEqual(self.attempt()["dispatch_state"], "accepted")
        self.assertEqual(len(self.backend.runs), 1)
        self.assertEqual(self.controller._state("counters")["ci_dispatches"], 1)
        self.assertIsNotNone(self.controller._head()["first_dispatch_at"])

    def test_process_crash_after_send_does_not_erase_the_sending_intent(self):
        self.approve()
        dispatch = self.backend.dispatch_candidate

        def crashed(*args, **kwargs):
            dispatch(*args, **kwargs)
            raise Crashed()

        self.backend.dispatch_candidate = crashed
        with self.assertRaises(Crashed):
            self.controller.tick()
        attempt = self.attempt()
        self.assertEqual(attempt["dispatch_state"], "sending")
        self.restart()
        self.controller.tick()
        self.assertEqual(self.attempt()["id"], attempt["id"])
        self.assertEqual(len(self.backend.runs), 1)
        self.assertEqual(self.attempt()["dispatch_state"], "accepted")

    def test_crash_during_source_preparation_resumes_the_same_request(self):
        self.approve()
        update = self.backend.update_ref
        armed = [True]

        def interrupted(*args, **kwargs):
            if armed[0] and str(args[1]).startswith("refs/heads/staging-candidate-"):
                armed[0] = False
                raise Crashed()
            return update(*args, **kwargs)

        self.backend.update_ref = interrupted
        with self.assertRaises(Crashed):
            self.controller.tick()
        attempt = self.attempt()
        self.assertEqual(attempt["dispatch_state"], "prepared")
        self.assertFalse(self.backend.runs)
        self.restart()
        self.controller.tick()
        self.assertEqual(self.attempt()["id"], attempt["id"])
        self.assertEqual(self.attempt()["request_id"], attempt["request_id"])
        self.assertEqual(len(self.backend.runs), 1)

    def test_two_recovery_connections_claim_only_one_send(self):
        self.approve()
        original = self.controller._send_prepared
        self.controller._send_prepared = lambda *_: None
        self.controller.tick()
        self.controller._send_prepared = original
        attempt = self.attempt()
        head = self.controller.store.row("batches", {"state": "head"})
        other = reopen_controller(self.root, self.backend, self.repo, self.clock)
        self.addCleanup(other.store.close)
        barrier = threading.Barrier(2)
        update = self.backend.update_ref

        def overlapping(*args, **kwargs):
            if str(args[1]).startswith("refs/heads/staging-candidate-"):
                barrier.wait(timeout=5)
            return update(*args, **kwargs)

        self.backend.update_ref = overlapping
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(c._send_prepared, head, attempt, self.clock()) for c in [self.controller, other]]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(self.backend.runs), 1)
        self.assertEqual(self.attempt()["dispatch_state"], "accepted")
        self.assertEqual(self.attempt()["request_id"], attempt["request_id"])

    def test_unknown_send_is_not_automatically_retried_after_timeout(self):
        self.approve()
        calls = []

        def unknown(*_args, **_kwargs):
            calls.append(1)
            raise OSError("connection outcome unknown")

        self.backend.dispatch_candidate = unknown
        self.controller.tick()
        initial = self.attempt()
        self.clock.advance(86400)
        for _ in range(3):
            self.controller.tick()
        self.assertEqual(calls, [1])
        self.assertEqual(self.attempt()["id"], initial["id"])
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertEqual(self.controller.status()["head"]["ci"]["dispatch_state"], "uncertain")
        retry = self.controller.retry_ci(PROJECT, "operator")
        self.assertTrue(retry["retried"])
        self.assertEqual(retry["forgotten"], 1)
        self.controller.tick()
        self.assertEqual(calls, [1, 1])
        self.assertNotEqual(self.attempt()["request_id"], initial["request_id"])

    def test_prepared_resume_uses_send_time_after_a_long_restart(self):
        attempt = self.prepare()
        self.clock.advance(86400)
        self.restart()
        self.controller.tick()
        sent = self.attempt()
        self.assertEqual(sent["id"], attempt["id"])
        self.assertEqual(sent["sent_at"], self.clock())
        self.backend.find_candidate_run = lambda *_args, **_kwargs: None
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertEqual(len(self.backend.runs), 1)

    def test_source_preparation_delay_is_excluded_from_send_timeout(self):
        self.approve()
        update = self.backend.update_ref

        def delayed(*args, **kwargs):
            if str(args[1]).startswith("refs/heads/staging-candidate-"):
                self.clock.advance(1200)
            return update(*args, **kwargs)

        self.backend.update_ref = delayed
        self.controller.tick()
        self.assertEqual(self.attempt()["sent_at"], self.clock())
        self.backend.find_candidate_run = lambda *_args, **_kwargs: None
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.assertEqual(len(self.backend.runs), 1)

    def test_prepared_resume_respects_publication_disabled(self):
        attempt = self.prepare()
        self.controller.set_setting("publication_enabled", False, "operator")
        self.restart()
        self.controller.tick()
        self.assertEqual(self.attempt()["id"], attempt["id"])
        self.assertEqual(self.attempt()["dispatch_state"], "prepared")
        self.assertFalse(self.backend.runs)

    def test_superseded_preparation_cannot_be_sent(self):
        attempt = self.prepare()
        self.controller.store.update("ci_attempts", {"id": attempt["id"]}, outcome="superseded")
        self.controller._send_prepared(self.controller._head(), attempt, self.clock())
        self.assertFalse(self.backend.runs)

    def test_superseded_send_is_not_revived_by_a_late_failure_or_acknowledgement(self):
        for fails in (True, False):
            with self.subTest(fails=fails):
                attempt = self.prepare() if fails else self.attempt()
                if not fails:
                    self.controller.store.update(
                        "ci_attempts", {"id": attempt["id"]}, outcome="pending", dispatch_state="prepared"
                    )
                    attempt = self.attempt()

                def superseded(*_args, attempt=attempt, fails=fails, **_kwargs):
                    self.controller.store.update("ci_attempts", {"id": attempt["id"]}, outcome="superseded")
                    if fails:
                        raise OSError("late ambiguous response")
                    return "late acknowledgement"

                self.backend.dispatch_candidate = superseded
                self.controller._send_prepared(self.controller._head(), attempt, self.clock())
                self.assertEqual(self.attempt()["outcome"], "superseded")
                self.assertEqual(self.controller._state("counters", {}).get("ci_dispatches", 0), 0)

    def test_operator_retry_observes_an_already_completed_unknown_send(self):
        self.approve()
        dispatch = self.backend.dispatch_candidate

        def lost(*args, **kwargs):
            dispatch(*args, **kwargs)
            raise OSError("ack lost")

        self.backend.dispatch_candidate = lost
        self.controller.tick()
        self.backend.start_runs()
        self.clock.advance(600)
        self.backend.complete_runs()
        self.assertEqual(self.controller.retry_ci(PROJECT, "operator")["forgotten"], 0)
        self.assertEqual(self.attempt()["outcome"], "pass")
        self.controller.tick()
        self.assertEqual(len(self.backend.runs), 1)
        self.assertIsNone(self.controller._head())

    def test_operator_retry_refuses_when_observation_fails_before_a_completed_lookup(self):
        self.approve()
        dispatch = self.backend.dispatch_candidate

        def lost(*args, **kwargs):
            dispatch(*args, **kwargs)
            raise OSError("ack lost")

        self.backend.dispatch_candidate = lost
        self.controller.tick()
        self.backend.start_runs()
        self.clock.advance(600)
        self.backend.complete_runs()
        find = self.backend.find_candidate_run
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("transient read failure")
            return find(*args, **kwargs)

        self.backend.find_candidate_run = flaky
        retry = self.controller.retry_ci(PROJECT, "operator")
        self.assertEqual(retry["reason"], "github-unavailable")
        self.assertEqual(calls, [1])
        self.assertEqual(self.attempt()["outcome"], "pending")
        self.controller.tick()
        self.assertEqual(len(self.backend.runs), 1)
        self.assertIsNone(self.controller._head())

    def test_late_run_readoption_preserves_its_sent_attempt_and_candidate_ref(self):
        self.approve()
        self.backend.ci_policy = lambda *_: ("infrastructure", [])
        self.controller.tick()
        sent = self.attempt()
        find = self.backend.find_candidate_run
        self.backend.find_candidate_run = lambda *_args, **_kwargs: None
        self.clock.advance(1200)
        self.controller.tick()

        def rejected(*_args, **_kwargs):
            raise DispatchNotAccepted("permission refused", retryable=False)

        self.backend.dispatch_candidate = rejected
        self.controller._dispatch(self.controller._head(), self.clock(), retry=1)
        rejected_attempt = self.attempt()
        self.assertGreater(rejected_attempt["id"], sent["id"])
        self.backend.find_candidate_run = find
        self.clock.advance(600)
        self.controller.tick()
        head = self.controller._head()
        current = self.controller._current_attempt(head)
        self.assertEqual(current["id"], sent["id"])
        self.assertEqual(current["dispatch_state"], "accepted")
        self.assertEqual(self.attempt()["outcome"], "superseded")
        feed = ControllerFeedSource(self.controller)._batch_view(head, {})
        self.assertEqual(feed.candidate_id, sent["id"])
        self.assertEqual(feed.staging_ref, self.controller.staging_candidate_ref(sent["id"], head["version"]))
        self.backend.start_runs()
        self.backend.complete_runs()
        self.clock.advance(600)
        self.controller.set_setting("infrastructure_retry_limit", 0, "operator")
        self.controller.tick()
        self.assertEqual(len(self.backend.runs), 1)
        self.assertEqual(self.controller._current_attempt(head)["outcome"], "infrastructure")
        self.assertTrue(any("exhausted" in r for r in self.controller._reasons))

    def test_a_late_observation_cannot_overwrite_supersession(self):
        self.approve()
        self.controller.tick()
        attempt = self.attempt()
        self.backend.start_runs()
        self.backend.complete_runs()
        find = self.backend.find_candidate_run

        def superseded(*args, **kwargs):
            self.controller.store.update("ci_attempts", {"id": attempt["id"]}, outcome="superseded")
            return find(*args, **kwargs)

        self.backend.find_candidate_run = superseded
        self.controller._observe_attempt(self.controller._head(), attempt, self.clock())
        self.assertEqual(self.attempt()["outcome"], "superseded")

    def test_permanent_rejections_have_a_separate_finite_budget_and_alert(self):
        self.approve()
        calls = []

        def rejected(*_args, **_kwargs):
            calls.append(1)
            raise DispatchNotAccepted("missing workflow", retryable=False)

        self.backend.dispatch_candidate = rejected
        for _ in range(8):
            self.controller.tick()
            self.clock.advance(3600)
        self.assertEqual(len(calls), 1 + self.controller.setting("infrastructure_retry_limit"))
        self.assertTrue(any("dispatch rejected" in alert for alert in self.controller.status()["alerts"]))
        self.controller.retry_ci(PROJECT, "operator")
        self.controller.tick()
        self.assertEqual(len(calls), 2 + self.controller.setting("infrastructure_retry_limit"))

    def test_rate_limit_deadline_survives_restart_without_consuming_run_budget(self):
        self.approve()
        calls = []
        deadline = self.clock() + 3600

        def limited(*_args, **_kwargs):
            calls.append(1)
            raise DispatchNotAccepted("quota", retry_at=deadline)

        self.backend.dispatch_candidate = limited
        self.controller.tick()
        self.restart()
        self.clock.advance(1200)
        self.controller.tick()
        self.assertEqual(calls, [1])
        for _ in range(6):
            self.clock.advance(3600)
            self.controller.tick()
        self.assertEqual(len(calls), 7)
        self.assertFalse(self.backend.runs)
        self.assertFalse(any("exhausted" in reason for reason in self.controller._reasons))

    def test_superseded_unknown_send_discovers_and_cancels_its_run(self):
        self.approve()
        dispatch = self.backend.dispatch_candidate

        def lost(*args, **kwargs):
            dispatch(*args, **kwargs)
            raise OSError("ack lost")

        self.backend.dispatch_candidate = lost
        self.controller.tick()
        head = self.controller._head()
        self.controller._cancel_superseded_runs(head["id"], self.clock(), reason="test rebuild")
        self.assertEqual(self.attempt()["outcome"], "superseded")
        self.assertIsNotNone(self.attempt()["cancelled_at"])

    def test_proven_rejection_retains_receipt_and_uses_bounded_retry(self):
        self.approve()
        calls = []

        def rejected(*_args, **_kwargs):
            calls.append(1)
            raise DispatchNotAccepted("permission refused")

        self.backend.dispatch_candidate = rejected
        self.controller.tick()
        first = self.attempt()
        self.assertEqual(first["dispatch_state"], "rejected")
        self.assertEqual(first["outcome"], "infrastructure")
        self.clock.advance(600)
        self.controller.tick()
        self.assertGreater(self.attempt()["id"], first["id"])
        self.assertNotEqual(self.attempt()["request_id"], first["request_id"])
        self.assertEqual(calls, [1, 1])


class OneSend(unittest.TestCase):
    def test_credential_preparation_failure_is_a_proven_non_send(self):
        class Tokens:
            def token(self):
                raise OSError("token endpoint unavailable")

        calls = []
        adapter = ga.GitHubAdapter(ga.GitHubClient(Tokens(), lambda *args: calls.append(args)), PROJECT)
        with self.assertRaises(DispatchNotAccepted):
            adapter.dispatch_candidate(
                PROJECT, head_sha="a" * 40, base_sha="b" * 40, base_ref="main", batch_id="1", members=[101]
            )
        self.assertFalse(calls)

    def test_server_failure_never_retransmits_repository_dispatch(self):
        calls, sleeps = [], []

        def transport(method, url, headers, body):
            calls.append((method, url, json.loads(body)))
            return 500, {}, b"server outcome unknown"

        client = ga.GitHubClient(None, transport, sleep=sleeps.append)
        adapter = ga.GitHubAdapter(client, PROJECT)
        with self.assertRaises(ga.GitHubError):
            adapter.dispatch_candidate(
                PROJECT, head_sha="a" * 40, base_sha="b" * 40, base_ref="main", batch_id="1", members=[101]
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(sleeps)

    def test_401_invalidates_credentials_without_retransmitting_the_dispatch(self):
        class Tokens:
            invalidations = 0

            def token(self):
                return "fixture-token"

            def invalidate(self):
                self.invalidations += 1

        tokens = Tokens()
        calls = []

        def transport(*args):
            calls.append(args)
            return 401, {}, b"bad credential"

        adapter = ga.GitHubAdapter(ga.GitHubClient(tokens, transport), PROJECT)
        with self.assertRaises(DispatchNotAccepted) as raised:
            adapter.dispatch_candidate(
                PROJECT, head_sha="a" * 40, base_sha="b" * 40, base_ref="main", batch_id="1", members=[101]
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(tokens.invalidations, 1)

    def test_rate_limit_records_wait_without_a_second_send_or_sleep(self):
        calls, sleeps = [], []

        def transport(*args):
            calls.append(args)
            return 429, {"retry-after": "5"}, b"limited"

        client = ga.GitHubClient(None, transport, sleep=sleeps.append)
        adapter = ga.GitHubAdapter(client, PROJECT)
        with self.assertRaises(DispatchNotAccepted):
            adapter.dispatch_candidate(
                PROJECT, head_sha="a" * 40, base_sha="b" * 40, base_ref="main", batch_id="1", members=[101]
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(sleeps)
        self.assertGreater(client.stats()["blocked_until"].get("secondary", 0), 0)

    def test_definite_http_rejection_is_classified_without_a_second_send(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return 403, {}, b"permission refused"

        adapter = ga.GitHubAdapter(ga.GitHubClient(None, transport), PROJECT)
        with self.assertRaises(DispatchNotAccepted) as raised:
            adapter.dispatch_candidate(
                PROJECT, head_sha="a" * 40, base_sha="b" * 40, base_ref="main", batch_id="1", members=[101]
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()

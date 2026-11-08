#!/usr/bin/env python3
"""Controller source-candidate dispatch, restart and fresh promotion evidence."""

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_requests import CONFIG, policy

from tauceti_queue import monitoring
from tauceti_queue.ci_execution import CandidateObservation
from tauceti_queue.ci_requests import RequestLedger
from tauceti_queue.controller import LOG_GRACE_SECONDS, Controller
from tauceti_queue.fakes import make_controller, run_git
from tauceti_queue.ports import CandidateRun, DispatchNotAccepted, PromotionRefused
from tauceti_queue.source_ci import SourceCandidateCI
from tauceti_queue.store import Store

PROJECT = "team/Math"


class VerifiedExecution:
    """Trusted evidence boundary fake; HTTP/artifact validation is tested separately."""

    def __init__(self):
        self.dispatches = []
        self.runs = {}
        self.cancelled = []
        self.lost_response = False
        self.observations = 0
        self.discoveries = 0

    def preflight(self, frozen):
        return "fixture-only"

    def dispatch(self, frozen, *, prepared=None):
        self.dispatches.append(frozen)
        if self.lost_response:
            raise OSError("lost response")
        return frozen.request.request_id

    def add(self, frozen, run_id="101", *, classification=None, attempt=1):
        proof = dict(
            kind="request",
            artifact_id=201,
            artifact_digest="sha256:" + "a" * 64,
            run_id=int(run_id),
            request_digest=frozen.request.digest,
            receipt_attempt=1,
            observed_attempt=attempt,
        )
        result = None
        if classification:
            result = dict(
                proof,
                kind="result",
                attempt=attempt,
                classification=classification,
                verdicts={
                    "build": "success",
                    "scope": "success",
                    "bump_guard": "success",
                    "publication": "not_required",
                },
            )
        run = CandidateRun(
            run_id,
            attempt,
            "completed" if classification else "in_progress",
            "success" if classification == "passed" else None,
            100,
            101,
            102 if classification else None,
            None,
            (),
            None,
            html_url="https://github.com/team/Execution/actions/runs/" + run_id,
            updated_at=102,
        )
        self.runs[run_id] = (frozen.request.digest, CandidateObservation(run, proof, result))

    def discover(self, frozen, *, since, known_run_ids=()):
        self.discoveries += 1
        return [obs for digest, obs in self.runs.values() if digest == frozen.request.digest], False

    def observe(self, frozen, run_id, *, owned=False):
        self.observations += 1
        digest, observation = self.runs[run_id]
        if digest != frozen.request.digest:
            raise ValueError("wrong request")
        return observation

    def status(self, frozen, run_id):
        return self.observe(frozen, run_id, owned=True).run

    def cancel(self, frozen, run_id, *, proof):
        self.cancelled.append(run_id)
        return "cancelled"


class SourceController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initial, self.source, self.repo, self.clock = make_controller(self.root, repository=PROJECT)
        self.store = initial.store
        self.addCleanup(lambda: self.store.close())
        self.execution = VerifiedExecution()
        self.source.repository_identity = policy().source
        self.retained = []
        self.approvals = []
        self.deny_approval = False

        def retain(frozen):
            self.retained.append(frozen)
            for ref, sha in frozen.retained_refs():
                self.source.update_ref(PROJECT, ref, sha)

        def approval(frozen):
            self.approvals.append(frozen)
            if self.deny_approval:
                raise PromotionRefused("fresh approval revoked")

        self.source.retain_candidate = retain
        self.source.verify_candidate_approval = approval
        self.connect()

    def connect(self):
        self.ledger = RequestLedger(self.store, self.repo.clone, policy(), CONFIG, workflow_revision="b" * 40)
        self.ci = SourceCandidateCI(self.ledger, self.execution, clock=self.clock)
        self.controller = Controller(
            self.store,
            self.source,
            str(self.repo.clone),
            PROJECT,
            revision="source-fixture",
            clock=self.clock,
            candidate_ci=self.ci,
        )

    def start(self, *, lost=False):
        sha = self.source.create_pr(17, {"A.lean": "a\n"})
        self.assertTrue(self.controller.approve(PROJECT, 17, sha, "reviewer")["accepted"])
        self.execution.lost_response = lost
        self.controller.tick()
        return self.store.rows("ci_attempts")[0]

    def restart(self):
        self.store.close()
        self.store = Store(str(self.root / "queue.sqlite"))
        self.connect()
        self.controller.recover()

    def test_full_controller_dispatch_and_fresh_result_promotion(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.assertEqual(self.execution.dispatches, [frozen])
        self.assertEqual(self.retained, [frozen])
        self.assertEqual(self.source.runs, [])  # legacy execution is never called
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.approvals, [frozen])
        self.assertEqual(self.store.rows("batches")[0]["state"], "landed")
        self.assertGreaterEqual(self.execution.observations, 2)  # observation and fresh promotion

    def check_unknown_dispatch_recovery(self, *, lost):
        attempt = self.start(lost=lost)
        self.clock.advance(86400)
        self.restart()
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 1)
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "pending")
        self.assertIn("reconciliation-required:", current["outcome_detail"])
        self.assertTrue(self.controller.status()["head"]["ci"]["reconciliation_required"])
        discovered = self.execution.discoveries
        self.controller.tick()
        self.assertEqual(self.execution.discoveries, discovered)
        self.assertFalse(self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"])["retried"])
        self.execution.add(self.execution.dispatches[0], classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(self.store.rows("batches")[0]["state"], "landed")

    def test_acknowledged_dispatch_never_resends_without_evidence(self):
        self.check_unknown_dispatch_recovery(lost=False)

    def test_lost_dispatch_never_resends_without_evidence(self):
        self.check_unknown_dispatch_recovery(lost=True)

    def test_receipt_cannot_pass_partial_rerun_or_expired_result(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.clock.advance(60)
        self.controller.tick()
        self.execution.add(frozen, classification="passed", attempt=2)
        digest, observation = self.execution.runs["101"]
        self.execution.runs["101"] = (digest, replace(observation, result=None))
        self.clock.advance(60)
        self.controller.tick()
        self.clock.advance(LOG_GRACE_SECONDS + 1)
        self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "pending")
        self.assertIn("reconciliation-required:", current["outcome_detail"])
        self.assertEqual(self.approvals, [])
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(self.store.count("ci_request_runs"), 1)

    def test_revoked_approval_blocks_cached_pass_and_unowned_cancel(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        self.deny_approval = True
        self.clock.advance(60)
        self.controller.tick()
        self.assertNotEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.approvals, [frozen])
        self.assertEqual(self.controller._perform("cancel_run", {"run_id": "999"}, self.clock())[:7], "gave-up")
        self.assertEqual(self.execution.cancelled, [])
        self.assertEqual(self.controller._perform("cancel_run", {"run_id": "101"}, self.clock()), "cancelled")
        self.assertEqual(self.execution.cancelled, ["101"])

    def test_membership_change_blocks_fresh_promotion(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        self.store.update("members", {"batch_id": attempt["batch_id"]}, accepted_head=frozen.request.base)
        self.clock.advance(60)
        self.controller.tick()
        self.assertNotEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.approvals, [])

    def test_rerun_between_pass_and_promotion_requires_its_own_result(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        observe = self.execution.observe

        def rerun(*args, **kwargs):
            observation = observe(*args, **kwargs)
            if self.execution.observations >= 2:
                return replace(observation, run=replace(observation.run, attempt=2), result=None)
            return observation

        self.execution.observe = rerun
        self.clock.advance(60)
        self.controller.tick()
        self.assertNotEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.approvals, [])
        promotion = self.store.row("outbox", {"kind": "promote"})
        self.assertEqual(promotion["state"], "done")
        self.assertEqual(promotion["result"], "source-evidence-deferred")

    def test_failed_freeze_rolls_back_attempt_before_source_or_execution_io(self):
        def invalid(*_args, **_kwargs):
            raise ValueError("missing accepted Git object")

        self.ci.freeze = invalid
        sha = self.source.create_pr(17, {"A.lean": "a\n"})
        self.controller.approve(PROJECT, 17, sha, "reviewer")
        reasons = self.controller.tick()
        self.assertTrue(any("cannot freeze source CI request" in reason for reason in reasons))
        self.assertIsNotNone(self.controller._state("last_tick_at"))
        self.assertEqual(self.store.count("ci_attempts"), 0)
        self.assertEqual(self.store.count("ci_requests"), 0)
        self.assertEqual(self.execution.dispatches, [])
        self.assertEqual(self.retained, [])

    def test_explicit_retry_names_exact_request_and_requires_owned_run_completion(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.controller.retry_ci(PROJECT, "reviewer")["reason"], "request-identity-required")
        self.assertEqual(
            self.controller.retry_ci(PROJECT, "reviewer", request_id="0" * 32)["reason"], "request-identity-required"
        )
        self.assertEqual(
            self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"])["reason"], "live-run"
        )
        digest, observation = self.execution.runs["101"]
        self.execution.runs["101"] = (digest, replace(observation, run=replace(observation.run, status="completed")))
        retried = self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"])
        self.assertEqual(retried, {"retried": True, "forgotten": 1})
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)
        self.assertNotEqual(self.execution.dispatches[1].request.request_id, frozen.request.request_id)
        self.assertEqual(self.ledger.owner_run(attempt["id"]), "101")

    def test_late_duplicate_is_owned_without_replacing_owner_and_blocks_manual_retry(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        self.assertEqual(self.ci.observe(attempt["id"]).run.run_id, "101")
        self.execution.add(frozen, run_id="102")
        retried = self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"])
        self.assertEqual(retried["reason"], "live-run")
        self.assertEqual(retried["run_id"], "102")
        self.assertEqual(self.ledger.owner_run(attempt["id"]), "101")
        self.assertEqual({row["run_id"] for row in self.ledger.owned_runs(attempt["id"])}, {"101", "102"})

    def test_bound_ledger_refuses_legacy_restart_and_unqualified_public_projection(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "requires its qualified CI coordinator"):
            Controller(self.store, self.source, str(self.repo.clone), PROJECT, revision="unsafe-restart")
        with self.assertRaisesRegex(ValueError, "qualified public monitoring policy"):
            monitoring.project(self.store.path, public_repositories=(PROJECT,))

    def test_git_preparation_and_fresh_approval_do_not_hold_controller_or_db_lock(self):
        git = self.ledger._git

        def read(*args, **kwargs):
            self.assertFalse(self.store._connection.in_transaction)
            self.assertIsNone(self.controller._lock._owner)
            return git(*args, **kwargs)

        self.ledger._git = read
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")

        def approval(_frozen):
            self.assertIsNone(self.controller._lock._owner)
            self.approvals.append(_frozen)

        self.source.verify_candidate_approval = approval
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.approvals, [frozen])
        self.assertEqual(self.repo.main_sha(), frozen.request.head)

    def test_withdrawn_heads_do_not_become_approval_members_and_refusal_is_settled_once(self):
        active = {"number": 17, "accepted_head": "a" * 40, "withdrawn": 0}
        withdrawn = {"number": 18, "accepted_head": "b" * 40, "withdrawn": 1}
        self.assertEqual([member.pr for member in self.ci.accepted([active, withdrawn])], [17])
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        self.deny_approval = True
        self.clock.advance(60)
        self.controller.tick()
        self.clock.advance(86400)
        self.controller.tick()
        self.assertEqual(self.approvals, [frozen])
        self.assertEqual(self.store.row("outbox", {"kind": "promote"})["result"], "source-reconciliation-required")
        self.assertTrue(self.controller.status()["head"]["ci"]["reconciliation_required"])

    def test_retryable_nonsend_reuses_frozen_request_and_preflights_before_retention(self):
        def unavailable(_frozen):
            raise DispatchNotAccepted("temporary preflight outage", retryable=True)

        self.execution.preflight = unavailable
        attempt = self.start()
        self.assertEqual(attempt["dispatch_state"], "prepared")
        frozen = self.ledger.load(attempt["id"])
        self.assertEqual(self.retained, [])
        self.clock.advance(60)
        self.controller.tick()
        self.restart()
        self.clock.advance(3600)
        self.controller.tick()
        self.assertEqual(self.store.count("ci_attempts"), 1)
        self.assertEqual(self.store.count("ci_requests"), 1)
        self.assertEqual(self.retained, [])
        self.assertEqual(self.execution.dispatches, [])
        self.execution.preflight = lambda _frozen: "fixture-only"
        self.clock.advance(3600)
        self.controller.tick()
        self.assertEqual(self.execution.dispatches, [frozen])
        self.assertEqual(self.retained, [frozen])

    def test_bad_scope_preflight_exhausts_finite_budget_without_publishing_source_refs(self):
        def refused(_frozen):
            raise DispatchNotAccepted("credential scope mismatch", retryable=False)

        self.execution.preflight = refused
        self.start()
        for _ in range(8):
            self.clock.advance(3600)
            self.controller.tick()
        self.assertEqual(self.store.count("ci_attempts"), 1 + self.controller.setting("infrastructure_retry_limit"))
        self.assertEqual(self.retained, [])
        self.assertEqual(self.execution.dispatches, [])

    def test_promotion_deferral_crash_rolls_back_outcome_and_generation_together(self):
        class Crash(BaseException):
            pass

        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        journal = self.store.journal

        def interrupted(now, kind, *args, **kwargs):
            if kind == "source-promotion-deferred":
                raise Crash()
            return journal(now, kind, *args, **kwargs)

        self.deny_approval = True
        self.store.journal = interrupted
        self.clock.advance(60)
        with self.assertRaises(Crash):
            self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pass")
        self.assertIsNone(self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}"))
        self.assertIsNone(self.store.get_setting(f"source-ci:promotion-generation:{attempt['id']}"))
        self.restart()
        self.deny_approval = False
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)

    def test_missing_original_head_is_fetched_after_real_gc_before_request_is_frozen(self):
        accepted = self.source.create_pr(17, {"A.lean": "a\n"})
        self.controller.approve(PROJECT, 17, accepted, "reviewer")
        dispatch = self.controller._dispatch
        self.controller._dispatch = lambda *_args, **_kwargs: None
        self.controller.tick()
        self.controller._dispatch = dispatch
        head = self.store.row("batches", {"state": "head"})
        self.assertNotEqual(head["tip"], accepted)
        refs = run_git(self.repo.clone, "for-each-ref", "--format=%(refname)", "--points-at", accepted).splitlines()
        for ref in refs:
            run_git(self.repo.clone, "update-ref", "-d", ref)
        run_git(self.repo.clone, "reflog", "expire", "--expire=now", "--all")
        run_git(self.repo.clone, "gc", "--prune=now", "--quiet")
        with self.assertRaises(RuntimeError):
            run_git(self.repo.clone, "cat-file", "-e", accepted)
        self.source.calls.clear()
        self.clock.advance(60)
        self.controller.tick()
        [frozen] = self.execution.dispatches
        self.assertEqual(frozen.request.accepted_members[0].head, accepted)
        self.assertTrue(any(call[0] == "fetch_refs" for call in self.source.calls))

    def test_declaration_reads_do_not_use_the_native_name_on_an_execution_backend(self):
        def wrong_backend(*_args):
            raise AssertionError("native declaration read reached legacy backend")

        self.source.declaration_names = wrong_backend
        self.assertFalse(self.controller._fetch_declaration_names(self.clock()))

    def test_completed_owned_history_does_not_require_repository_wide_discovery_before_automatic_retry(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="infrastructure")
        self.ci.observe(attempt["id"])

        def oversized_history(*_args, **_kwargs):
            raise AssertionError("unbounded historical run listing")

        self.execution.discover = oversized_history
        self.assertIsNone(self.ci.live_attempt([attempt]))

    def test_crash_during_preflight_leaves_prepared_request_resumable(self):
        class Crash(BaseException):
            pass

        def preflight(_frozen):
            [row] = self.store.rows("ci_attempts")
            self.assertEqual(row["dispatch_state"], "prepared")
            raise Crash()

        self.execution.preflight = preflight
        with self.assertRaises(Crash):
            self.start()
        [attempt] = self.store.rows("ci_attempts")
        frozen = self.ledger.load(attempt["id"])
        self.assertEqual(attempt["dispatch_state"], "prepared")
        self.execution.preflight = lambda _frozen: "fixture-only"
        self.restart()
        self.controller.tick()
        self.assertEqual(self.execution.dispatches, [frozen])

    def test_retry_uses_displayed_owner_nonce_after_newer_rejection_is_superseded(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        rejected_id = self.store.insert(
            "ci_attempts",
            batch_id=attempt["batch_id"],
            batch_version=attempt["batch_version"],
            head_sha=attempt["head_sha"],
            base_sha=attempt["base_sha"],
            base_ref="main",
            dispatched_at=self.clock(),
            request_id="0" * 32,
            dispatch_state="rejected",
            outcome="superseded",
        )
        self.assertGreater(rejected_id, attempt["id"])
        digest, observation = self.execution.runs["101"]
        self.execution.runs["101"] = (digest, replace(observation, run=replace(observation.run, status="completed")))
        self.assertEqual(self.controller.status()["head"]["ci"]["request_id"], attempt["request_id"])
        self.assertEqual(
            self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"]),
            {"retried": True, "forgotten": 1},
        )

    def test_owned_cancelled_run_uses_bounded_infrastructure_recovery(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        digest, observation = self.execution.runs["101"]
        self.execution.runs["101"] = (
            digest,
            replace(
                observation,
                run=replace(observation.run, status="completed", conclusion="cancelled"),
                result_absent=True,
            ),
        )
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.clock.advance(LOG_GRACE_SECONDS + 1)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "infrastructure")
        self.clock.advance(3600)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)
        self.assertEqual(self.store.rows("ci_attempts")[-1]["infrastructure_retry"], 1)

    def test_repeated_preparation_failure_alerts_and_exact_retry_can_retire_prepared_request(self):
        def unavailable(_frozen):
            raise DispatchNotAccepted("preflight outage", retryable=True)

        self.execution.preflight = unavailable
        attempt = self.start()
        for _ in range(3):
            self.clock.advance(3600)
            self.controller.tick()
        status = self.controller.status()
        self.assertTrue(any("preparation failed" in value for value in status["alerts"]))
        self.assertGreaterEqual(status["head"]["ci"]["dispatch_failures"], 3)
        self.assertEqual(
            self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"]),
            {"retried": True, "forgotten": 1},
        )

    def test_repeated_fresh_pass_after_transient_promotion_failure_is_counted_once(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        checked = []

        def approval(_frozen):
            checked.append(_frozen)
            if len(checked) == 1:
                raise OSError("temporary source observation failure")

        self.source.verify_candidate_approval = approval
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.controller._state("counters")["ci_passes"], 1)
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(len(checked), 2)
        self.assertEqual(self.controller._state("counters")["ci_passes"], 1)
        self.assertEqual(self.store.count("journal", {"kind": "source-ci-result"}), 1)
        self.assertEqual(
            [row["key"] for row in self.store.rows("settings") if row["key"].startswith("source-ci:")],
            ["source-ci:binding", "source-ci:epoch"],
        )

    def test_transient_result_outage_after_completion_grace_preserves_source_failure(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="source_failure")
        digest, original = self.execution.runs["101"]
        original = replace(original, run=replace(original.run, conclusion="failure", completed_at=self.clock() - 3600))
        self.execution.runs["101"] = (digest, replace(original, result=None, result_absent=False))
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["run_completed_at"], self.clock())
        self.clock.advance(LOG_GRACE_SECONDS + 1)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.assertEqual(len(self.execution.dispatches), 1)
        self.execution.runs["101"] = (digest, original)
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "source-failure")
        self.assertEqual(self.store.count("tasks", {"kind": "repair"}), 1)
        self.assertEqual(len(self.execution.dispatches), 1)

    def test_source_ci_reads_release_lock_and_cannot_revive_changed_request(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        observe = self.execution.observe

        def changed(*args, **kwargs):
            self.assertIsNone(self.controller._lock._owner)
            observation = observe(*args, **kwargs)
            self.store.update("ci_attempts", {"id": attempt["id"]}, outcome="superseded")
            return observation

        self.execution.observe = changed
        self.clock.advance(60)
        self.controller.tick()
        self.assertNotEqual(self.repo.main_sha(), frozen.request.head)
        self.assertEqual(self.approvals, [])
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "superseded")


if __name__ == "__main__":
    unittest.main()

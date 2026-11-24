#!/usr/bin/env python3
"""Controller source-candidate dispatch, restart and fresh promotion evidence."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from queue_ci_requests import CONFIG, policy
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable

from tauceti_queue import monitoring
from tauceti_queue import runtime as rt
from tauceti_queue.ci_execution import CandidateObservation
from tauceti_queue.ci_requests import RequestLedger
from tauceti_queue.controller import LOG_GRACE_SECONDS, SOURCE_PHASES_INTERVAL_SECONDS, Controller, ControllerError
from tauceti_queue.fakes import make_controller, run_git
from tauceti_queue.github_adapter import RateLimited
from tauceti_queue.ports import CandidatePhase, CandidateRun, DispatchNotAccepted, PromotionRefused
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
        self.deleted_runs = set()
        self.cancel_failures = 0
        self.cancel_error = None

    def preflight(self, frozen):
        return "fixture-only"

    def caller(self, policy):
        return self.caller_snapshot

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
        if run_id in self.deleted_runs:
            raise EvidenceUnavailable("execution evidence transport failed")
        digest, observation = self.runs[run_id]
        if digest != frozen.request.digest:
            raise ValueError("wrong request")
        return observation

    def status(self, frozen, run_id):
        return self.observe(frozen, run_id, owned=True).run

    def deleted(self, frozen, run_id):
        return run_id in self.deleted_runs

    def cancel(self, execution, run_id, *, request_digest, proof):
        if proof.get("request_digest") != request_digest or str(proof.get("run_id")) != run_id:
            raise ValueError("cancellation requires the stored request ownership proof")
        if self.cancel_error is not None:
            raise self.cancel_error()
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise OSError("owned execution cancellation is unconfirmed")
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
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.clock.advance(86400)
        self.restart()
        self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "infrastructure")
        self.assertTrue(current["outcome_detail"].startswith("unowned-run:"))
        self.clock.advance(3600)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)
        first, second = self.execution.dispatches
        self.assertNotEqual(second.request.request_id, first.request.request_id)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)
        self.execution.add(second, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)
        self.assertEqual(self.store.rows("batches")[0]["state"], "landed")

    def running_phases(self, step):
        """GitHub's jobs listing for a run in its build job, at the 1-based `step` of three."""
        names = ["Set up job", "Build exact candidate under bwrap", "Complete job"]
        phases = [CandidatePhase("build / Build", 101, None, "in_progress")]
        for index, name in enumerate(names, 1):
            status = "completed" if index < step else ("in_progress" if index == step else "queued")
            started = 100 + index if index <= step else None
            phases.append(CandidatePhase(f"build / Build: {name}", started, started if index < step else None, status))
        return tuple(phases)

    def test_source_attempt_records_github_phases_for_status_and_stall(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        reads = []
        self.step = 2

        def progress(frozen_request, run):
            self.assertEqual(frozen_request, frozen)
            reads.append(run.run_id)
            return replace(run, phases=self.running_phases(self.step), runner_name="staging-1", host="staging-1")

        self.execution.progress = progress
        self.clock.advance(60)
        self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(reads, ["101"])
        self.assertEqual(current["runner_name"], "staging-1")
        status = self.controller.status()
        ci = status["head"]["ci"]
        self.assertEqual(len(ci["phases"]), 4)
        self.assertEqual(
            ci["current_step"],
            {
                "job": "build / Build",
                "name": "Build exact candidate under bwrap",
                "index": 2,
                "total": 3,
                "started_at": 102,
            },
        )
        self.assertIn("step 2/3 Build exact candidate under bwrap (build / Build), running", rt.summarize(status))
        # Observation continues every polling interval; the jobs listing is read only every
        # SOURCE_PHASES_INTERVAL_SECONDS while the run is in progress.
        self.store.set_setting("polling_interval_seconds", SOURCE_PHASES_INTERVAL_SECONDS // 2)
        observations = self.execution.observations
        self.clock.advance(SOURCE_PHASES_INTERVAL_SECONDS // 2)
        self.controller.tick()
        self.assertGreater(self.execution.observations, observations)
        self.assertEqual(reads, ["101"])
        # Unchanged phases are silence: the stall report names the recorded phase, not None.
        interval = int(self.controller.setting("stall_interval_seconds"))
        self.clock.advance(interval)
        self.controller.tick()
        self.assertEqual(len(reads), 2)
        stall = self.controller.status()["head"]["stall"]
        self.assertIsNotNone(stall)
        self.assertEqual(stall["phase"], "build / Build: Complete job")
        # A step completing is progress and clears the stall.
        self.step = 3
        self.clock.advance(SOURCE_PHASES_INTERVAL_SECONDS)
        self.controller.tick()
        self.assertEqual(len(reads), 3)
        self.assertIsNone(self.controller.status()["head"]["stall"])
        self.assertEqual(self.controller.status()["head"]["ci"]["current_step"]["index"], 3)
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")

    def test_source_phases_are_read_once_at_completion_and_never_decide(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        reads = []

        def progress(_frozen, run):
            reads.append(run.status)
            if run.status == "completed":
                raise OSError("jobs listing unavailable")
            return replace(run, phases=self.running_phases(2))

        self.execution.progress = progress
        self.clock.advance(60)
        self.controller.tick()
        self.execution.add(frozen, classification="passed")
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(reads, ["in_progress", "completed"])
        # A failed phase read leaves the verified result as the only authority.
        self.assertEqual(self.store.rows("batches")[0]["state"], "landed")

    def test_failed_phase_read_keeps_observing(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)

        def progress(_frozen, _run):
            raise OSError("rate limited")

        self.execution.progress = progress
        self.clock.advance(60)
        reasons = self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["run_id"], "101")
        self.assertEqual(current["outcome"], "pending")
        self.assertEqual(current["phases"], "[]")
        self.assertTrue(any("source CI phases unavailable" in reason for reason in reasons), reasons)
        self.assertIsNone(self.controller.status()["head"]["ci"]["current_step"])

    def test_eviction_reads_source_phases_afresh_and_needs_them(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        publishing = (
            CandidatePhase("sandboxed-build", 100, None, "in_progress"),
            CandidatePhase(
                "sandboxed-build: Build exact candidate under bwrap, offline", 100, 200, "completed", "success"
            ),
            CandidatePhase("publish-merge-group-cache / publish", 210, None, "in_progress"),
        )
        phases = {"value": self.running_phases(2)}
        reads = []

        def progress(_frozen, run):
            reads.append(run.run_id)
            if phases["value"] is None:
                raise OSError("rate limited")
            return replace(run, phases=phases["value"])

        self.execution.progress = progress
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(len(reads), 1)
        head = self.store.rows("batches")[0]
        phases["value"] = publishing
        with self.controller._lock:
            # Within SOURCE_PHASES_INTERVAL_SECONDS of the last read: the eviction check reads them again.
            row = self.controller._eviction_observation(head, attempt, self.clock())
            self.assertEqual(len(reads), 2)
            self.assertEqual(self.controller._protection(head, row, observed=True), "it is publishing its final oleans")
            phases["value"] = None
            with self.assertRaises(ControllerError):
                self.controller._eviction_observation(head, row, self.clock())
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")

    def test_acknowledged_dispatch_without_evidence_resends_once_after_timeout(self):
        self.check_unknown_dispatch_recovery(lost=False)

    def test_lost_dispatch_without_evidence_resends_once_after_timeout(self):
        self.check_unknown_dispatch_recovery(lost=True)

    def check_unavailable_listing_never_resends(self, discover):
        attempt = self.start()
        self.execution.discover = discover
        for _ in range(3):
            self.clock.advance(86400)
            self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 1)
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "pending")
        self.assertIn("reconciliation-required:", current["outcome_detail"])
        self.assertTrue(self.controller.status()["head"]["ci"]["reconciliation_required"])

    def test_incomplete_listing_does_not_resend(self):
        self.check_unavailable_listing_never_resends(lambda *_args, **_kwargs: ([], True))

    def test_listing_api_error_does_not_resend(self):
        def outage(*_args, **_kwargs):
            raise EvidenceUnavailable("execution run listing is incomplete")

        self.check_unavailable_listing_never_resends(outage)

    def test_deleted_owned_run_resends(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        self.execution.deleted_runs.add("101")
        self.assertIsNone(self.ci.live_attempt([attempt], discover_completed=True))
        self.clock.advance(60)
        self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "infrastructure")
        self.assertTrue(current["outcome_detail"].startswith("lost-run:"))
        self.clock.advance(3600)
        self.controller.tick()
        self.assertEqual(len(self.execution.dispatches), 2)

    def test_unreadable_owned_run_keeps_waiting(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        self.execution.deleted = lambda *_args: False
        self.execution.deleted_runs.add("101")
        for _ in range(3):
            self.clock.advance(86400)
            self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.assertEqual(len(self.execution.dispatches), 1)

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
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.clock.advance(LOG_GRACE_SECONDS + 1)
        self.controller.tick()
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual(current["outcome"], "infrastructure")
        self.assertIn("no verified result after publication grace", current["outcome_detail"])
        self.assertEqual(self.approvals, [])
        self.assertEqual(len(self.execution.dispatches), 1)
        self.assertEqual(self.store.count("ci_request_runs"), 1)

    def owned_live_attempt_with_retired_policy_field(self):
        """An owned live run whose attempt was frozen with a policy field this service no longer parses."""
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.clock.advance(60)
        self.controller.tick()
        attempt = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual((attempt["run_id"], self.store.count("ci_request_runs")), ("101", 1))
        row = self.store.row("ci_requests", {"attempt_id": attempt["id"]})
        stored = json.loads(row["policy_json"])
        self.store.update(
            "ci_requests",
            {"attempt_id": attempt["id"]},
            policy_json=json.dumps(dict(stored, harness_revision="a" * 40)),
        )
        with self.assertRaisesRegex(ValueError, "invalid execution policy fields"):
            self.ledger.load(attempt["id"])
        return attempt

    def test_a_run_frozen_under_a_retired_policy_field_can_still_be_cancelled(self):
        attempt = self.owned_live_attempt_with_retired_policy_field()
        self.assertEqual(self.ci.cancel_run("101"), "cancelled")
        self.controller._cancel_superseded_runs(attempt["batch_id"], self.clock(), reason="head rebuilt")
        self.assertEqual(self.execution.cancelled, ["101", "101"])
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertEqual((current["outcome"], current["cancel_result"]), ("superseded", "cancelled"))
        self.assertEqual(self.controller._cancel_intents(), {})

    def test_cancellation_still_requires_a_stored_ownership_row(self):
        attempt = self.owned_live_attempt_with_retired_policy_field()
        for run_id in ("999", "0101"):
            with self.assertRaisesRegex(ValueError, "not owned"):
                self.ci.cancel_run(run_id)
        self.store.update("ci_request_runs", {"run_id": "101"}, receipt_json=json.dumps({"kind": "request"}))
        with self.assertRaisesRegex(ValueError, "ownership proof"):
            self.ci.cancel_run("101")
        self.store.execute("DELETE FROM ci_request_runs")
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.ci.cancel_run("101")
        self.controller._cancel_superseded_runs(attempt["batch_id"], self.clock(), reason="head rebuilt")
        self.clock.advance(3600)
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.execution.cancelled, [])
        self.assertIn(attempt["id"], self.controller._cancel_intents(), "an unproven run is never cancelled")

    def test_a_failed_superseded_cancel_is_retried_with_backoff_until_settled(self):
        attempt = self.owned_live_attempt_with_retired_policy_field()
        self.execution.cancel_failures = 2
        self.controller._cancel_superseded_runs(attempt["batch_id"], self.clock(), reason="head rebuilt")
        current = self.store.row("ci_attempts", {"id": attempt["id"]})
        self.assertTrue(current["cancel_result"].startswith("failed:"), current["cancel_result"])
        self.assertEqual(self.controller._cancel_intents()[attempt["id"]]["reason"], "head rebuilt")
        self.assertEqual(self.store.count("outbox", {"kind": "cancel_run"}), 0)
        retry = self.controller.setting("outbox_retry_seconds")
        self.assertEqual(self.controller._cancel_intents()[attempt["id"]]["retry_at"], self.clock() + retry)
        self.controller._settle_cancel_intents(self.clock())  # the first retry waits out its backoff
        self.clock.advance(retry)
        self.controller._settle_cancel_intents(self.clock())  # fails again and backs off further
        intent = self.controller._cancel_intents()[attempt["id"]]
        self.assertEqual(intent["tries"], 2)
        self.assertEqual(intent["retry_at"], self.clock() + 2 * retry)
        self.clock.advance(2 * retry - 1)
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.execution.cancelled, [])
        self.clock.advance(1)
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.execution.cancelled, ["101"])
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["cancel_result"], "cancelled")
        self.assertEqual(self.controller._cancel_intents(), {})

    def test_a_rate_limited_cancel_waits_for_githubs_deadline_measured_after_the_failure(self):
        attempt = self.owned_live_attempt_with_retired_policy_field()
        retry = self.controller.setting("outbox_retry_seconds")

        def limited(slow, deadline):
            start = self.clock()

            def error():
                self.clock.advance(slow)  # the failing request itself took this long
                return RateLimited("rate limited", 429, retry_at=start + deadline, resource="core")

            return start, error

        # GitHub's deadline is later than the backoff: the intent waits for GitHub.
        start, self.execution.cancel_error = limited(0, 3 * retry)
        self.controller._cancel_superseded_runs(attempt["batch_id"], start, reason="head rebuilt")
        self.assertEqual(self.controller._cancel_intents()[attempt["id"]]["retry_at"], start + 3 * retry)
        self.clock.advance(3 * retry - 1)
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.controller._cancel_intents()[attempt["id"]]["tries"], 1, "not retried early")
        # A slow failure: the backoff is measured from after it, not from the start of the pass.
        self.clock.advance(1)
        start, self.execution.cancel_error = limited(4 * retry, 1)
        self.controller._settle_cancel_intents(start)
        intent = self.controller._cancel_intents()[attempt["id"]]
        self.assertEqual((intent["tries"], intent["retry_at"]), (2, self.clock() + 2 * retry))
        self.execution.cancel_error = None
        self.clock.advance(2 * retry)
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.execution.cancelled, ["101"])
        self.assertEqual(self.controller._cancel_intents(), {})

    def test_an_empty_complete_discovery_settles_an_accepted_dispatch_past_its_timeout(self):
        attempt = self.start()  # dispatched, and no run ever appears
        self.assertEqual((attempt["dispatch_state"], attempt["run_id"]), ("accepted", None))
        self.controller._cancel_superseded_runs(attempt["batch_id"], self.clock(), reason="head rebuilt")
        self.assertIn(attempt["id"], self.controller._cancel_intents(), "inside its timeout the run may yet appear")
        self.clock.advance(self.controller.setting("dispatch_timeout_seconds"))
        discoveries = self.execution.discoveries
        self.controller._settle_cancel_intents(self.clock())
        self.assertEqual(self.execution.discoveries, discoveries + 1)
        self.assertEqual(self.controller._cancel_intents(), {}, "NoOwner is an empty lookup, not a failure")
        self.assertEqual(self.execution.cancelled, [])
        # A send that may or may not have reached GitHub is never settled by age.
        self.store.update("ci_attempts", {"id": attempt["id"]}, dispatch_state="uncertain")
        self.controller._write_cancel_intent(self.store.row("ci_attempts", {"id": attempt["id"]}), "head rebuilt", 0)
        self.clock.advance(10 * self.controller.setting("dispatch_timeout_seconds"))
        self.controller._settle_cancel_intents(self.clock())
        self.assertIn(attempt["id"], self.controller._cancel_intents())
        self.assertEqual(self.execution.cancelled, [])

    def test_a_superseded_accepted_dispatch_without_any_run_past_its_timeout_writes_no_intent(self):
        attempt = self.start()
        self.clock.advance(self.controller.setting("dispatch_timeout_seconds"))
        self.controller._cancel_superseded_runs(attempt["batch_id"], self.clock(), reason="head rebuilt")
        self.assertEqual(self.controller._cancel_intents(), {})
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "superseded")

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

    def test_operator_resumes_repaired_promotion_with_same_successful_request(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="passed")
        self.deny_approval = True
        self.clock.advance(60)
        self.controller.tick()

        def retry(nonce):
            return self.controller.retry_ci(PROJECT, "operator", request_id=nonce)

        self.assertEqual(retry("0" * 32)["reason"], "request-identity-required")
        self.assertEqual(retry(frozen.request.request_id)["reason"], "promotion-refused")
        self.assertTrue(self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}"))
        self.deny_approval = False
        self.assertTrue(retry(frozen.request.request_id)["promotion_only"])
        self.controller.tick()
        self.assertEqual(self.repo.main_sha(), frozen.request.head, self.controller._reasons)
        self.assertEqual(self.store.count("ci_attempts"), 1)
        self.assertEqual(self.execution.dispatches, [frozen])

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

    def test_bad_scope_preflight_keeps_retrying_without_publishing_source_refs(self):
        def refused(_frozen):
            raise DispatchNotAccepted("credential scope mismatch", retryable=False)

        self.execution.preflight = refused
        self.start()
        for _ in range(8):
            self.clock.advance(3600)
            self.controller.tick()
        self.assertEqual(self.store.count("ci_attempts"), 9)
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

    def _owned_missing_result_uses_bounded_recovery(self, conclusion, *, absent=True):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        digest, observation = self.execution.runs["101"]
        self.execution.runs["101"] = (
            digest,
            replace(
                observation,
                run=replace(observation.run, status="completed", conclusion=conclusion),
                result_absent=absent,
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

    def test_owned_cancelled_run_uses_bounded_infrastructure_recovery(self):
        self._owned_missing_result_uses_bounded_recovery("cancelled")

    def test_owned_failed_run_with_confirmed_absence_uses_bounded_infrastructure_recovery(self):
        self._owned_missing_result_uses_bounded_recovery("failure")

    def test_owned_failed_run_with_unreadable_result_uses_bounded_infrastructure_recovery(self):
        self._owned_missing_result_uses_bounded_recovery("failure", absent=False)

    def test_owned_successful_run_without_result_uses_bounded_infrastructure_recovery(self):
        self._owned_missing_result_uses_bounded_recovery("success", absent=False)

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

    def test_verified_source_failure_supplies_actual_findings_to_repair(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="source_failure")
        digest, original = self.execution.runs["101"]
        finding = {"kind": "lint", "module": "", "message": "lint driver timed out (exit 124)"}
        self.execution.runs["101"] = (digest, replace(original, run=replace(original.run, diagnostics=(finding,))))
        self.clock.advance(60)
        self.controller.tick()
        import json

        task = self.store.rows("tasks", {"kind": "repair"})[-1]
        self.assertIn(finding, json.loads(task["diagnostics"]))
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "source-failure")

    def test_result_outage_within_completion_grace_preserves_source_failure(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen, classification="source_failure")
        digest, original = self.execution.runs["101"]
        original = replace(original, run=replace(original.run, conclusion="failure", completed_at=self.clock() - 3600))
        self.execution.runs["101"] = (digest, replace(original, result=None, result_absent=False))
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(
            self.store.row("ci_attempts", {"id": attempt["id"]})["run_completed_at"], original.run.completed_at
        )
        self.assertEqual(self.store.get_setting(f"source-ci:completion-seen:{attempt['id']}")["at"], self.clock())
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "pending")
        self.execution.runs["101"] = (digest, original)
        self.clock.advance(60)
        self.controller.tick()
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "source-failure")
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

    def test_live_readoption_does_not_undo_an_operator_retry_during_unlocked_discovery(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        live = self.ci.live_attempt

        def retry_during_discovery(attempts, **kwargs):
            self.assertIsNone(self.controller._lock._owner)
            stale = live(attempts, **kwargs)
            self.ci.live_attempt = live  # The operator performs a separate, fresh check.
            digest, observation = self.execution.runs["101"]
            self.execution.runs["101"] = (
                digest,
                replace(observation, run=replace(observation.run, status="completed")),
            )
            self.assertEqual(
                self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"]),
                {"retried": True, "forgotten": 1},
            )
            return stale

        self.ci.live_attempt = retry_during_discovery
        with self.controller._lock:
            self.assertTrue(self.controller._readopt_live_run(self.controller._head(), self.clock()))
        self.assertEqual(self.store.row("ci_attempts", {"id": attempt["id"]})["outcome"], "superseded")
        self.assertEqual(self.store.count("journal", {"kind": "run-readopted"}), 0)

    def test_retry_reports_changed_head_during_unlocked_liveness_check(self):
        attempt = self.start()
        frozen = self.ledger.load(attempt["id"])
        self.execution.add(frozen)
        self.ci.observe(attempt["id"])
        status = self.execution.status

        def changed(*args, **kwargs):
            run = status(*args, **kwargs)
            self.store.update("batches", {"id": attempt["batch_id"]}, version=attempt["batch_version"] + 1)
            return run

        self.execution.status = changed
        self.assertEqual(
            self.controller.retry_ci(PROJECT, "reviewer", request_id=attempt["request_id"]),
            {"retried": False, "reason": "head-changed"},
        )


if __name__ == "__main__":
    unittest.main()

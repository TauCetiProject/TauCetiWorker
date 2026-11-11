"""Connect durable source requests to trusted execution observations.

This component owns request/CI evidence only. Native approval, sandboxing and
source publication remain with the source port and controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tauceti_tools import ci_candidate as cc
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable

from .ci_requests import MissingCandidateObjects
from .ports import DispatchNotAccepted, PromotionRefused
from .store import loads


@dataclass(frozen=True)
class SourceDispatch:
    frozen: object
    execution: object = field(repr=False)


class SourceCandidateCI:
    def __init__(self, ledger, execution, *, clock):
        self.ledger, self.execution, self.clock = ledger, execution, clock

    @staticmethod
    def accepted(members):
        return tuple(
            cc.Member(member["number"], member["accepted_head"]) for member in members if not member.get("withdrawn")
        )

    def prepare(self, head, members, request_id, *, fetch_missing=None):
        attempt = dict(
            batch_id=head["id"],
            batch_version=head["version"],
            head_sha=head["tip"],
            base_sha=head["base"],
            base_ref=self.ledger.policy.base_branch,
            request_id=request_id,
        )
        accepted = self.accepted(members)
        try:
            return self.ledger.prepare(attempt, accepted)
        except MissingCandidateObjects as error:
            if fetch_missing is None:
                raise
            recovered = set()
            for member in accepted:
                if member.head in error.heads and member.head not in recovered:
                    if fetch_missing(member.pr, member.head):
                        recovered.add(member.head)
            return self.ledger.prepare(attempt, accepted)

    def freeze(self, attempt_id, members, *, prepared=None):
        return self.ledger.freeze(attempt_id, self.accepted(members), now=self.clock(), prepared=prepared)

    def prepare_source(self, attempt_id, source):
        frozen = self.ledger.load(attempt_id)
        if getattr(source, "repository_identity", None) != frozen.policy.source:
            raise ValueError("candidate source port belongs to another repository")
        self.execution.preflight(frozen)
        self.ledger.restore_membership(frozen)
        source.retain_candidate(frozen)
        return SourceDispatch(frozen, self.execution.preflight(frozen))

    def dispatch(self, attempt_id, *, prepared):
        # All snapshot/database reads occur before the send claim. Validate the
        # process-local binding without loading a request after claiming it.
        if not isinstance(prepared, SourceDispatch) or prepared.frozen.attempt_id != attempt_id:
            raise DispatchNotAccepted("source dispatch preparation belongs to another attempt", retryable=False)
        return self.execution.dispatch(prepared.frozen, prepared=prepared.execution)

    def observe(self, attempt_id):
        frozen = self.ledger.load(attempt_id)
        owner = self.ledger.owner_run(attempt_id)
        if owner is None:
            self._discover(frozen)
            owner = self.ledger.owner_run(attempt_id)
        if owner is None:
            # Even a complete empty listing is not proof that an acknowledged
            # event cannot arrive later. Keep the single-send intent for explicit
            # operator reconciliation instead of automatically retransmitting it.
            raise EvidenceUnavailable("source dispatch has no verified owner; reconcile without resending")
        return self.execution.observe(frozen, owner, owned=True)

    def _discover(self, frozen):
        attempt = self.ledger.store.row("ci_attempts", {"id": frozen.attempt_id})
        known = {row["run_id"] for row in self.ledger.owned_runs(frozen.attempt_id)}
        observations, unavailable = self.execution.discover(
            frozen, since=attempt["sent_at"] or attempt["dispatched_at"], known_run_ids=known
        )
        for observation in observations:
            if observation.ownership is None:
                if observation.run.run_id not in known:
                    raise ValueError("unowned discovery has no verified ownership proof")
                continue
            self.ledger.record_owned_run(frozen.attempt_id, observation.run, observation.ownership, now=self.clock())
        return unavailable

    def _owned(self, run_id):
        binding = self.ledger.store.get_setting("source-ci:binding")
        row = self.ledger.store.row("ci_request_runs", {"execution_id": binding["execution"]["id"], "run_id": run_id})
        if row is None:
            raise ValueError("execution run is not owned by this source ledger")
        request = self.ledger.store.row("ci_requests", {"request_digest": row["request_digest"]})
        if request is None:
            raise ValueError("owned execution request snapshot is unavailable")
        return self.ledger.load(request["attempt_id"]), loads(row["receipt_json"])

    def read_run(self, run_id):
        frozen, _proof = self._owned(run_id)
        return self.execution.status(frozen, run_id)

    def cancel_run(self, run_id):
        frozen, proof = self._owned(run_id)
        return self.execution.cancel(frozen, run_id, proof=proof)

    def live_attempt(self, attempts, *, discover_completed=False):
        for attempt in reversed(attempts):
            if attempt["cancelled_at"] is not None:
                continue
            if attempt["dispatch_state"] not in {"accepted", "sending", "uncertain"}:
                continue
            frozen = self.ledger.load(attempt["id"])
            known = self.ledger.owned_runs(attempt["id"])
            for row in known:
                run = self.execution.status(frozen, row["run_id"])
                if run.status in {"queued", "in_progress"}:
                    return attempt, run
            # Completed owned attempts need no historical full-repository scan
            # on every automatic retry. An explicit operator retry additionally
            # discovers late duplicates; background cleanup is a separate gate.
            if known and not discover_completed:
                continue
            if self._discover(frozen) or self.ledger.owner_run(attempt["id"]) is None:
                raise EvidenceUnavailable("source dispatch ownership cannot be completely reconciled")
            # A duplicate event can produce several valid runs. A completed
            # selected owner does not authorize cancelling another live run.
            for row in self.ledger.owned_runs(attempt["id"]):
                run = self.execution.status(frozen, row["run_id"])
                if run.status in {"queued", "in_progress"}:
                    return attempt, run
        return None

    def verify_promotion(self, attempt_id, members, source):
        frozen = self.ledger.load(attempt_id)
        expected = self.accepted(members)
        if frozen.request.accepted_members != expected:
            raise PromotionRefused("source membership changed after its CI request was frozen")
        observation = self.observe(attempt_id)
        if observation.result is None or observation.result.get("classification") != "passed":
            raise EvidenceUnavailable("source promotion has no current verified passing result")
        if getattr(source, "repository_identity", None) != frozen.policy.source:
            raise PromotionRefused("promotion source belongs to another repository")
        source.verify_candidate_approval(frozen)

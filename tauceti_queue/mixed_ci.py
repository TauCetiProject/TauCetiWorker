"""One frozen mixed candidate, with CI authority separate from source approval.

The existing request ledger and GitHub attestation machinery are reused. The
Forgejo source retains the immutable candidate objects; every member separately
names its original enrolled repository. Queue/task retention stays on GitHub.
"""

from __future__ import annotations

from pathlib import Path

from tauceti_tools import ci_candidate as cc

from .ci_execution import GitHubSourceExecution
from .ci_requests import RequestLedger
from .control_policy_file import read_control_policy
from .mixed_identity import SourceNumber
from .mixed_profile import private_json
from .ports import PromotionRefused
from .source_ci import SourceCandidateCI


class MixedCandidateCI(SourceCandidateCI):
    def __init__(self, ledger, execution, registry, controller, *, clock):
        super().__init__(ledger, execution, clock=clock)
        self.registry, self.controller = registry, controller

    @classmethod
    def build(cls, registry, controller, *, execution=None):
        from .github_adapter import UrllibTransport
        from .github_app import InstallationTokenSource, load_execution_credentials

        profile = registry.profile.raw["execution"]
        if not isinstance(profile, dict) or set(profile) != {
            "configuration_file",
            "policy_file",
            "credentials_dir",
        }:
            raise ValueError("mixed admission requires explicit source execution inputs")
        for key in ("configuration_file", "policy_file", "credentials_dir"):
            if not isinstance(profile[key], str) or not Path(profile[key]).is_absolute():
                raise ValueError("source execution inputs require explicit absolute paths")
        configuration = read_control_policy(Path(profile["configuration_file"]))
        policy = cc.ExecutionPolicy.parse(private_json(profile["policy_file"]), configuration=configuration)
        if (
            policy.source != registry.profile.forgejo
            or set(policy.member_sources) != {registry.profile.github, registry.profile.forgejo}
            or policy.base_branch != controller.canonical_branch
        ):
            raise ValueError("mixed execution policy differs from the enrolled source identities")
        if execution is None:
            credentials = load_execution_credentials(Path(profile["credentials_dir"]))
            tokens = InstallationTokenSource(
                credentials,
                UrllibTransport(),
                repository_id=policy.execution.native_id,
                permissions={"contents": "write", "actions": "write"},
            )
            execution = GitHubSourceExecution(tokens)
        revision, policy, configuration = execution.caller(policy)
        ledger = RequestLedger(
            controller.store,
            controller.clone,
            policy,
            configuration,
            workflow_revision=revision,
            mixed_sources=True,
        )
        return cls(ledger, execution, registry, controller, clock=controller.clock)

    def accepted(self, members):
        accepted = []
        for member in members:
            if member.get("withdrawn"):
                continue
            number = member["number"]
            source = number.source if isinstance(number, SourceNumber) else self.registry.profile.github
            if source not in (self.registry.profile.github, self.registry.profile.forgejo):
                raise ValueError("batch member is not from an enrolled source")
            accepted.append(cc.Member(int(number), member["accepted_head"], source))
        return tuple(accepted)

    def prepare(self, head, members, request_id, *, fetch_missing=None):
        revision, policy, configuration = self.execution.caller(self.ledger.policy)
        self.ledger.workflow_revision, self.ledger.policy, self.ledger.configuration = revision, policy, configuration

        # The inherited helper only has a native number. Resolve missing members
        # using the qualified source before any fetch.
        def fetch(number, sha):
            for member in members:
                if int(member["number"]) == number and member["accepted_head"] == sha:
                    return self.controller._fetch_head(member["number"], sha)
            return False

        return super().prepare(head, members, request_id, fetch_missing=fetch)

    def prepare_source(self, attempt_id, source):
        return super().prepare_source(attempt_id, self.registry.native)

    def verify_promotion(self, attempt_id, members, source):
        frozen = self.ledger.load(attempt_id)
        if frozen.request.accepted_members != self.accepted(members):
            raise PromotionRefused("mixed candidate membership changed after CI was frozen")
        observation = self.observe(attempt_id)
        if observation.result is None or observation.result.get("classification") != "passed":
            from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable

            raise EvidenceUnavailable("mixed candidate has no verified exact passing result")
        if not self.registry.supports_candidate(frozen.request.base, frozen.request.head):
            raise PromotionRefused("mixed candidate exceeds the current native path policy")
        for member in members:
            if member.get("withdrawn"):
                continue
            number = member["number"]
            if isinstance(number, SourceNumber):
                self.registry.check_native(number)
                snapshot = self.registry.native.admission_snapshot(
                    self.registry.profile.forgejo.repository.name, int(number)
                )
                decision, _, _, _, _ = self.registry.native.admission_policy.evaluate(snapshot)
            else:
                self.registry.check_github()
                snapshot = self.registry.github.admission_snapshot(self.registry.repository, int(number))
                admission = self.controller.admission.github
                if admission is None:
                    raise PromotionRefused("mixed candidate needs the current GitHub admission policy")
                decision, _, _, _, _ = admission.policy.evaluate(snapshot)
            if (
                snapshot.pull.head_sha != member["accepted_head"]
                or snapshot.pull.state != "open"
                or snapshot.pull.draft
                or snapshot.pull.base_ref != self.controller.canonical_branch
            ):
                raise PromotionRefused("an original source head or target changed after candidate CI")
            if (
                self.controller._blocking_labels(snapshot.pull)
                or not decision["review_safe"]
                or decision.get("recovery_authority_hold")
                or member["accepted_head"] in decision.get("recovery_heads", ())
            ):
                raise PromotionRefused("an original source review or hold no longer authorizes this candidate")

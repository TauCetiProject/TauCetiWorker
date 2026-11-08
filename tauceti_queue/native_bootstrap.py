"""Explicit trusted wiring for a Forgejo source and separate GitHub execution.

No environment credential discovery or production selection happens here. The
operator/rehearsal supplies an approved configuration snapshot and independently
loaded credentials. The existing Service owns the store, HTTP server and loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import ForgeError, Forgejo, Incomplete

from .admission import AdmissionPoller
from .admission import policy as review_policy
from .ci_execution import GitHubSourceExecution
from .ci_requests import RequestLedger
from .controller import Controller
from .forgejo_source import ForgejoSourceAdapter, NativeAdmissionPolicy, NativePublishers
from .server import ServerConfig
from .source_authority import (
    NativeQueueAuthority,
    QueueControlPolicy,
    QueueRepositoryAccess,
    SourceAccessDenied,
    SourceAuthorityUnavailable,
)
from .source_ci import SourceCandidateCI
from .workflow_token import WorkflowTokenSource


@dataclass(frozen=True)
class NativeComponents:
    source: ForgejoSourceAdapter
    controller: Controller
    poller: AdmissionPoller
    http: ServerConfig


@dataclass(frozen=True)
class NativeBootstrap:
    configuration: bytes = field(repr=False)
    execution_policy: cc.ExecutionPolicy
    workflow_revision: str
    publishers: NativePublishers
    control_policy: QueueControlPolicy = field(repr=False)
    source_client: object = field(repr=False)
    source_git_token: str = field(repr=False)
    execution_tokens: object = field(repr=False)
    source_webhook_secret: bytes = field(repr=False)
    source_git_client_factory: object = field(default=Forgejo, repr=False, kw_only=True)

    def build(self, config, store, *, revision, clock=time.time):
        """Validate every binding before constructing the live controller."""
        approved = self.execution_policy
        if not isinstance(approved, cc.ExecutionPolicy) or approved.source.repository.forge.kind != "forgejo":
            raise ValueError("native service requires an approved Forgejo source/execution policy")
        if (
            not isinstance(self.publishers, NativePublishers)
            or self.publishers.source != approved.source
            or not isinstance(self.control_policy, QueueControlPolicy)
            or self.control_policy.source != approved.source
            or config.repository != approved.source.repository.name
            or config.canonical_branch != approved.base_branch
            or config.admission_source != "policy"
        ):
            raise ValueError("native service source, branch, controls or admission configuration differ")
        if (
            getattr(self.source_client, "forge", None) != approved.source.repository.forge
            or getattr(self.execution_tokens, "repository_id", None) != approved.execution.native_id
            or not isinstance(self.source_webhook_secret, bytes)
            or len(self.source_webhook_secret) < 32
            or any(not 33 <= c <= 126 for c in self.source_webhook_secret)
        ):
            raise ValueError("native service credentials must bind the approved source and execution origins")
        if self.control_policy.permits(self.source_git_token):
            raise ValueError("queue controls must not enroll the controller publication credential")
        if isinstance(self.execution_tokens, WorkflowTokenSource):
            job = self.execution_tokens.job
            if (
                job.repository != approved.execution
                or job.revision != self.workflow_revision
                or job.branch != approved.execution_branch
            ):
                raise ValueError("native workflow credential launch differs from the approved execution policy")
        # Constructors validate exact configuration bytes, repository IDs,
        # immutable workflow revision and token-provider resource restrictions.
        # No generic gh/PAT fallback can supply execution credentials.
        execution = GitHubSourceExecution(self.execution_tokens, clock=clock)
        authority = NativeQueueAuthority(approved.source, control_policy=self.control_policy)
        try:
            access = QueueRepositoryAccess.from_effective(
                approved.source.repository,
                self.source_client.request("GET", f"repositories/{approved.source.native_id}/effective_access"),
            )
            git_client = self.source_git_client_factory(
                approved.source.repository.forge, token=self.source_git_token, timeout=10, max_bytes=1024 * 1024
            )
            git_access = QueueRepositoryAccess.from_effective(
                approved.source.repository,
                git_client.request("GET", f"repositories/{approved.source.native_id}/effective_access"),
            )
        except ForgeError as error:
            if error.status in {401, 403, 404}:
                raise SourceAccessDenied("native source credential was refused") from None
            raise SourceAuthorityUnavailable(
                "native service requires current effective source credential access"
            ) from None
        except Incomplete:
            raise SourceAuthorityUnavailable(
                "native service requires current effective source credential access"
            ) from None
        authority.require_admin(access)
        authority.require_admin(git_access)
        if access.repository_id != approved.source.native_id:
            raise SourceAccessDenied("native service source repository identity changed")
        if (
            git_access.repository_id != approved.source.native_id
            or git_access.actor.principal != access.actor.principal
        ):
            raise SourceAccessDenied("native Git publication credential belongs to another actor or repository")
        if (
            access.actor.principal not in self.publishers.statuses
            or access.actor.principal not in self.publishers.recoveries
        ):
            raise SourceAccessDenied("controller source actor is not an approved status and recovery publisher")
        if access.actor.principal in self.publishers.reviews:
            raise SourceAccessDenied("controller publication actor must be separate from trusted review publishers")
        ledger = RequestLedger(
            store, config.content_clone, approved, self.configuration, workflow_revision=self.workflow_revision
        )
        root = config.admission_lean_root
        admission = NativeAdmissionPolicy(
            self.publishers,
            rubrics=config.admission_rubrics.split(","),
            prefix=root.replace(".", "/") + "/",
            allow_files=review_policy.default_allow_files(root)
            + [s for s in config.admission_extra_files.split(",") if s],
            base_branch=approved.base_branch,
            clock=clock,
        )
        source = ForgejoSourceAdapter(
            approved.source,
            self.source_client,
            config.content_clone,
            token=self.source_git_token,
            admission_policy=admission,
            clock=clock,
        )
        coordinator = SourceCandidateCI(ledger, execution, clock=clock)
        controller = Controller(
            store,
            source,
            str(config.content_clone),
            config.repository,
            revision=revision,
            candidate_ci=coordinator,
            source_authority=authority,
            canonical_branch=approved.base_branch,
            clock=clock,
        )
        controller.source_actor = access.actor
        poller = AdmissionPoller(controller, source, config.repository, admission, clock=clock)
        http = ServerConfig(
            webhook_secret=self.source_webhook_secret,
            repositories={approved.source.native_id: config.repository},
            adapter=source,
            host=config.listen_host,
            port=config.listen_port,
            review_app_id=None,
            native_webhooks=True,
        )
        return NativeComponents(source, controller, poller, http)

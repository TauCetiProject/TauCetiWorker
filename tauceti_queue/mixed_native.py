"""Native queue source reads for the live two-forge phase.

Review authority comes from explicitly enrolled native user IDs and the native
PR resource. Native commit statuses never grant candidate CI authority. Existing
review decisions run before admission; trusted candidate CI remains mandatory
before landing, including per-member size/scope/dependency and full lint gates.
"""

from __future__ import annotations

import dataclasses

from tauceti_tools.forge import Incomplete

from .admission import AdmissionPolicy, policy
from .forgejo_source import ForgejoSourceAdapter, NativeAdmissionPolicy
from .mixed_identity import SourceNumber


class MixedNativePolicy(NativeAdmissionPolicy):
    def evaluate(self, snapshot):
        # Admission here reserves a candidate for execution, never permission
        # to land. No native success status or build result is fabricated.
        result, authorization, evidence, comment, meta = super().evaluate(snapshot)
        preliminary = dataclasses.replace(
            snapshot, statuses={**snapshot.statuses, "build": "success", "scope": "success", "bump-guard": "success"}
        )
        static, _, _, _, _ = AdmissionPolicy.evaluate(self, preliminary)
        if result["review_safe"] and not result.get("recovery_heads"):
            result["merge"] = static["merge"]
            result["reason"] = (
                "review and static policy ready; exact mixed candidate CI required"
                if static["merge"]
                else static["reason"]
            )
        evidence.update(
            source=self.publishers.source.record(), candidate_ci_required=True, observed_source_ci="unknown"
        )
        return result, authorization, evidence, comment, meta


class MixedNativeSource(ForgejoSourceAdapter):
    def __init__(self, profile, config, clone, *, client=None, token=None, clock):
        from .mixed_profile import credential

        root = config.admission_lean_root
        admission = MixedNativePolicy(
            profile.publishers(),
            clock=clock,
            base_branch=config.canonical_branch or "main",
            rubrics=config.admission_rubrics.split(","),
            prefix=root.replace(".", "/") + "/",
            allow_files=policy.default_allow_files(root) + [s for s in config.admission_extra_files.split(",") if s],
        )
        super().__init__(
            profile.forgejo,
            client or profile.client(),
            clone,
            token=token or credential(profile.token_file),
            admission_policy=admission,
            clock=clock,
        )
        self.profile, self.mode = profile, config.forgejo_mode

    def _statuses(self, head, rows=None):
        return {}  # Source commit statuses never stand in for execution evidence.

    def list_open_pull_requests(self, repo, **options):
        return super().list_open_pull_requests(repo, admission=True)

    def _publisher(self):
        if self.mode != "admit":
            raise PermissionError("Forgejo observation cannot publish")
        actor = self.client.current_user()
        if type(actor.get("id")) is not int or actor["id"] != self.profile.publisher:
            raise Incomplete("native queue publisher identity changed")

    def post_comment(self, *args, **kwargs):
        self._publisher()
        return super().post_comment(*args, **kwargs)

    def set_labels(self, *args, **kwargs):
        self._publisher()
        return super().set_labels(*args, **kwargs)

    def ensure_label(self, *args, **kwargs):
        self._publisher()
        return super().ensure_label(*args, **kwargs)

    def close_pr(self, *args, **kwargs):
        self._publisher()
        return super().close_pr(*args, **kwargs)

    def update_refs(self, *args, **kwargs):
        self._publisher()
        return super().update_refs(*args, **kwargs)

    def fast_forward(self, *args, **kwargs):
        raise PermissionError("native main uses its separately configured writer")

    def number(self, number):
        return SourceNumber(int(number), self.repository_identity)

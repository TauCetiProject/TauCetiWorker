"""Qualified task identities; message metadata never supplies runner authority."""

from __future__ import annotations

import re
from dataclasses import dataclass

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import GITHUB

TASK_SCHEMA = "tauceti-queue.source-task/v2"
BINDING_SCHEMA = "tauceti-queue.task-binding/v1"
NATIVE_GIT_REFS = "tauceti-queue.forgejo-git/v1"


def native_retained_ref(ref):
    if not isinstance(ref, str) or not ref.startswith("refs/tauceti-queue/"):
        raise ValueError("expected a controller-local queue ref")
    return "refs/heads/tauceti-ci/queue/" + ref.removeprefix("refs/tauceti-queue/")


def native_submission_ref(epoch, task_id, claim_id):
    if (
        not isinstance(epoch, str)
        or re.fullmatch(r"[0-9a-f]{32}", epoch) is None
        or any(type(i) is not int or not 0 < i < 2**63 for i in (task_id, claim_id))
    ):
        raise ValueError("invalid native submission identity")
    return f"refs/heads/tauceti-submissions/{epoch}/{task_id}/{claim_id}"


@dataclass(frozen=True)
class TaskBinding:
    source: cc.RepositoryIdentity
    execution: cc.RepositoryIdentity
    policy_digest: str
    queue_epoch: str

    def __post_init__(self):
        if (
            not isinstance(self.source, cc.RepositoryIdentity)
            or self.source.repository.forge.kind != "forgejo"
            or not isinstance(self.execution, cc.RepositoryIdentity)
            or self.execution.repository.forge != GITHUB
            or not isinstance(self.policy_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.policy_digest) is None
            or not int(self.policy_digest, 16)
            or not isinstance(self.queue_epoch, str)
            or re.fullmatch(r"[0-9a-f]{32}", self.queue_epoch) is None
        ):
            raise ValueError("invalid qualified task binding")

    @classmethod
    def from_policy(cls, policy: cc.ExecutionPolicy, *, queue_epoch: str):
        if not isinstance(policy, cc.ExecutionPolicy):
            raise ValueError("a trusted source execution policy is required")
        return cls(policy.source, policy.execution, policy.digest, queue_epoch)

    @classmethod
    def parse(cls, row):
        if (
            not isinstance(row, dict)
            or set(row) != {"schema", "source", "execution", "policy_digest", "queue_epoch"}
            or row["schema"] != BINDING_SCHEMA
        ):
            raise ValueError("invalid qualified task binding fields")
        return cls(
            cc.RepositoryIdentity.parse(row["source"]),
            cc.RepositoryIdentity.parse(row["execution"]),
            row["policy_digest"],
            row["queue_epoch"],
        )

    def record(self):
        return {
            "schema": BINDING_SCHEMA,
            "source": self.source.record(),
            "execution": self.execution.record(),
            "policy_digest": self.policy_digest,
            "queue_epoch": self.queue_epoch,
        }

    def pull_key(self, number: int) -> str:
        if type(number) is not int or not 0 < number < 2**63:
            raise ValueError("a native source pull number is required")
        return f"{self.source.repository.forge.key}/repositories/{self.source.native_id}/pulls/{number}"


class TaskBindingMismatch(ValueError):
    """A task is not bound to this runner's trusted source/execution policy."""


class NativeTaskExecutionUnavailable(RuntimeError):
    """The native transport/runner adapters are required before task execution."""


def validate_native_task(row):
    """Validate task references without Git, credentials or source I/O."""
    if (
        type(row.get("task_id")) not in (int, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", str(row["task_id"])) is None
        or int(row["task_id"]) >= 2**63
        or not isinstance(row.get("members"), list)
        or len(row["members"]) > cc.MAX_RETAINED_MEMBERS
        or not isinstance(row.get("extra"), dict)
        or type(row.get("expires_at")) not in (int, float)
        or not 0 < row["expires_at"] < 2**53
        or not isinstance(row.get("claim_token"), str)
        or re.fullmatch(r"[0-9a-f]{32}", row["claim_token"]) is None
    ):
        raise TaskBindingMismatch("invalid qualified task references or lease")
    batch_id = row.get("batch_id")
    if (
        type(batch_id) not in (int, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", str(batch_id)) is None
        or int(batch_id) >= 2**63
        or type(row.get("batch_version")) is not int
        or not 0 <= row["batch_version"] < 2**63
    ):
        raise TaskBindingMismatch("invalid qualified task batch identity")
    branch = row["extra"].get("canonical_branch")
    if (
        not isinstance(branch, str)
        or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*", branch) is None
    ):
        raise TaskBindingMismatch("invalid qualified task canonical branch")
    for section in (row, row["extra"]):
        if "renew_after" in section and (
            type(section["renew_after"]) not in (int, float) or not 0 < section["renew_after"] <= 3600
        ):
            raise TaskBindingMismatch("invalid qualified task renewal interval")
    for key in ("base", "tip", "onto"):
        value = row.get(key)
        if (
            not isinstance(value, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None
            or not int(value, 16)
        ):
            raise TaskBindingMismatch("invalid qualified task commit")

    def oid(value):
        return (
            isinstance(value, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None
            and bool(int(value, 16))
        )

    warm = row.get("warm_revs", [])
    if not isinstance(warm, list) or any(not oid(value) for value in warm):
        raise TaskBindingMismatch("invalid qualified task warm revision")
    pre = row["extra"].get("pre_rebased")
    if pre is not None and (
        not isinstance(pre, dict)
        or not oid(pre.get("source"))
        or not oid(pre.get("batch_base"))
        or (pre.get("head_base") is not None and not oid(pre["head_base"]))
        or (
            pre.get("head_batch_id") is not None
            and (type(pre["head_batch_id"]) is not int or not 0 < pre["head_batch_id"] < 2**63)
        )
    ):
        raise TaskBindingMismatch("invalid qualified pre-rebased task references")
    transport = row["extra"].get("git_transport")
    if transport not in (None, NATIVE_GIT_REFS):
        raise TaskBindingMismatch("unknown native Git transport")
    prefix = "refs/heads/tauceti-ci/queue/" if transport == NATIVE_GIT_REFS else "refs/tauceti-queue/"
    refs = row.get("retained_refs")
    if (
        not isinstance(refs, list)
        or not refs
        or any(
            not isinstance(ref, str)
            or re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", ref) is None
            for ref in refs
        )
    ):
        raise TaskBindingMismatch("invalid qualified task retained refs")
    expected = f"refs/tauceti-queue/submissions/{row['task_id']}/{row['extra'].get('claim_id')}"
    if transport == NATIVE_GIT_REFS:
        binding = TaskBinding.parse(row.get("binding"))
        expected = native_submission_ref(binding.queue_epoch, int(row["task_id"]), row["extra"].get("claim_id"))
    if (
        type(row["extra"].get("claim_id")) is not int
        or row["extra"]["claim_id"] < 1
        or row.get("submission_ref") != expected
    ):
        raise TaskBindingMismatch("invalid qualified task submission ref")
    seen = set()
    for member in row["members"]:
        if (
            not isinstance(member, dict)
            or type(member.get("number")) is not int
            or member["number"] < 1
            or member["number"] in seen
        ):
            raise TaskBindingMismatch("invalid or repeated qualified task member")
        seen.add(member["number"])
        head = member.get("accepted_head")
        if (
            not isinstance(head, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
            or not int(head, 16)
        ):
            raise TaskBindingMismatch("invalid qualified member head")
        retained = f"{prefix}inputs/{member['number']}/{head}"
        if (
            member.get("retained_ref") != retained
            or retained not in refs
            or (member.get("commit") is not None and not oid(member["commit"]))
        ):
            raise TaskBindingMismatch("invalid qualified member commit or retained ref")

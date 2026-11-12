"""Durable source-candidate identities, approved configuration and retained Git data.

Freezing a request performs local Git reads and one database transaction. Source
retention, execution dispatch and observations happen only after it is committed.
Old requests keep their own approved policy/configuration across process upgrades.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass

from tauceti_tools import ci_candidate as cc
from tauceti_tools.project import parse

from .store import dumps, loads


class MissingCandidateObjects(ValueError):
    def __init__(self, heads):
        super().__init__("accepted source commit objects are unavailable")
        self.heads = tuple(heads)


def git_object(kind, raw):
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest()


def manifest_objects(raw, heads, *, epoch, request_id):
    """Canonical bytes reconstruct the same objects after GC, without a clock."""
    tree = b"100644 " + cc.MEMBER_MANIFEST_FILE.encode() + b"\0" + bytes.fromhex(git_object("blob", raw))
    commit = (
        "tree "
        + git_object("tree", tree)
        + "\n"
        + "".join("parent " + head + "\n" for head in heads)
        + "author TauCeti Queue <queue@example.invalid> 0 +0000\n"
        + "committer TauCeti Queue <queue@example.invalid> 0 +0000\n\n"
        + f"Retained source membership {epoch}/{request_id}\n"
    ).encode("ascii")
    return tree, commit


@dataclass(frozen=True)
class FrozenRequest:
    attempt_id: int
    request: cc.CandidateRequest
    policy: cc.ExecutionPolicy
    configuration: bytes
    member_manifest: bytes | None
    manifest_commit: bytes | None

    def retained_refs(self):
        request = self.request
        pairs = [(request.retained_ref, request.head)]
        if request.member_manifest is not None:
            pairs.append((request.manifest_ref, request.member_manifest.commit))
        else:
            pairs.extend((request.member_ref(member), member.head) for member in request.accepted_members)
        return tuple(pairs)


class RequestLedger:
    def __init__(self, store, clone, policy, configuration, *, workflow_revision, mixed_sources=False):
        self.store, self.clone = store, os.fspath(clone)
        if mixed_sources and (not policy.member_sources or store.get_setting("source-ci:binding") is not None):
            raise ValueError("mixed requests require explicit enrolled sources and a legacy GitHub queue")
        self.binding_key = "mixed-ci:binding" if mixed_sources else "source-ci:binding"
        self.epoch_key = "mixed-ci:epoch" if mixed_sources else "source-ci:epoch"
        if cc.ExecutionPolicy.parse(policy.record(with_config=False), configuration=configuration) != policy:
            raise ValueError("CI policy differs from its trusted configuration")
        if not (mixed_sources and workflow_revision is None) and (
            not isinstance(workflow_revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", workflow_revision)
            or workflow_revision == "0" * 40
        ):
            raise ValueError("CI caller requires an immutable revision")
        self.check_configuration(policy, configuration)
        self.policy, self.configuration, self.workflow_revision = policy, configuration, workflow_revision
        self.git = shutil.which("git")
        if not self.git:
            raise OSError("Git is unavailable")
        self.environment = {
            key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "SSL_CERT_FILE"}
        }
        self.environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0"
        )
        binding = self.binding(policy)
        with store.transaction():
            previous = store.get_setting(self.binding_key)
            if previous is None:
                # Never reinterpret unqualified GitHub approvals/claims as native
                # authority. A source cutover needs an explicit ledger migration.
                if not mixed_sources and any(
                    store.count(table) for table in ("inputs", "batches", "members", "claims", "tasks", "ci_attempts")
                ):
                    raise ValueError("source CI cannot adopt an unqualified source ledger")
                store.set_setting(self.binding_key, binding)
                store.set_setting(self.epoch_key, secrets.token_hex(16))
            elif previous != binding:
                raise ValueError("source CI repository roles changed")
            self.epoch = store.get_setting(self.epoch_key)
            if not isinstance(self.epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", self.epoch):
                raise ValueError("source CI epoch is unavailable")

    @staticmethod
    def check_configuration(policy, configuration):
        project = parse(configuration)
        if (project.repository_ref("content"), project.ci_repository_ref(), project.branch) != (
            policy.source.repository,
            policy.execution.repository,
            policy.base_branch,
        ):
            raise ValueError("project source/execution differs from the CI policy")

    @staticmethod
    def binding(policy):
        # Revisions/configuration may change through approved service upgrades;
        # native repository roles and the source branch cannot silently change.
        value = {
            "source": policy.source.record(),
            "execution": policy.execution.record(),
            "harness": policy.harness.record(),
            "credential_origin": policy.credential_origin.key,
            "base_branch": policy.base_branch,
        }
        if policy.member_sources:
            value["member_sources"] = [source.record() for source in policy.member_sources]
        return value

    def _git(self, *arguments, raw=None):
        process = subprocess.run(
            [self.git, "--no-replace-objects", "-C", self.clone, "-c", "core.hooksPath=/dev/null", *arguments],
            input=raw,
            capture_output=True,
            env=self.environment,
            timeout=30,
        )
        if process.returncode:
            raise OSError("local source-candidate Git operation failed")
        return process.stdout

    def prepare(self, attempt, members):
        """Read immutable Git data before acquiring the database write transaction."""
        if self.workflow_revision is None:
            raise ValueError("select a protected execution caller before preparing source CI")
        if attempt["base_ref"] != self.policy.base_branch:
            raise ValueError("source request base branch differs from trusted policy")
        members = tuple(members)
        if (
            not members
            or len(members) > cc.MAX_RETAINED_MEMBERS
            or not all(isinstance(member, cc.Member) for member in members)
        ):
            raise ValueError("invalid accepted source members")
        if any(not re.fullmatch(r"[0-9a-f]{40}", attempt[key]) for key in ("head_sha", "base_sha")):
            raise ValueError("source attempt has invalid commit identities")
        required = tuple(
            dict.fromkeys((attempt["head_sha"], attempt["base_sha"], *(member.head for member in members)))
        )
        observed = (
            self._git(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
                raw="".join(head + "\n" for head in required).encode(),
            )
            .decode("ascii")
            .splitlines()
        )
        if observed != [head + " commit" for head in required]:
            missing = [head for head, line in zip(required, observed, strict=True) if line == head + " missing"]
            if missing:
                raise MissingCandidateObjects(missing)
            raise ValueError("accepted source commit objects are unavailable")
        tree = self._git("rev-parse", attempt["head_sha"] + "^{tree}").decode("ascii").strip()
        raw = commit = descriptor = None
        if len(members) > cc.MAX_MEMBERS:
            raw = cc.member_manifest_bytes(self.epoch, attempt["request_id"], members)
            heads = tuple(dict.fromkeys(member.head for member in members))
            _, commit = manifest_objects(raw, heads, epoch=self.epoch, request_id=attempt["request_id"])
            descriptor = cc.MemberManifest(
                git_object("commit", commit), hashlib.sha256(raw).hexdigest(), len(members), len(raw)
            )
        request = cc.CandidateRequest(
            attempt["request_id"],
            self.epoch,
            self.policy.digest,
            self.policy.source,
            self.policy.execution,
            self.workflow_revision,
            self.policy.harness_revision,
            self.policy.config_digest,
            attempt["batch_id"],
            attempt["batch_version"],
            attempt["head_sha"],
            attempt["base_sha"],
            tree,
            members,
            descriptor,
        )
        # Check the exact transport bound as well as the in-memory object.
        cc.CandidateRequest.parse(request.record(), policy=self.policy)
        return FrozenRequest(0, request, self.policy, self.configuration, raw, commit)

    def freeze(self, attempt_id, members, *, now, prepared=None):
        """Atomically insert a previously prepared immutable request snapshot."""
        if self.store.row("ci_requests", {"attempt_id": attempt_id}) is not None:
            return self.load(attempt_id)
        attempt = self.store.row("ci_attempts", {"id": attempt_id})
        if attempt is None:
            raise ValueError("source request attempt is unavailable")
        prepared = self.prepare(attempt, members) if prepared is None else prepared
        request = prepared.request
        with self.store.transaction():
            if self.store.row("ci_requests", {"attempt_id": attempt_id}) is not None:
                return self.load(attempt_id)
            attempt = self.store.row("ci_attempts", {"id": attempt_id})
            if attempt is None or attempt["dispatch_state"] != "prepared" or attempt["outcome"] != "pending":
                raise ValueError("source request needs a pending prepared attempt")
            if (
                prepared.policy != self.policy
                or prepared.configuration != self.configuration
                or (
                    request.batch_id,
                    request.batch_version,
                    request.head,
                    request.base,
                    request.request_id,
                    prepared.policy.base_branch,
                )
                != (
                    attempt["batch_id"],
                    attempt["batch_version"],
                    attempt["head_sha"],
                    attempt["base_sha"],
                    attempt["request_id"],
                    attempt["base_ref"],
                )
            ):
                raise ValueError("source request preparation no longer matches its attempt")
            self.store.insert(
                "ci_requests",
                attempt_id=attempt_id,
                request_id=request.request_id,
                request_digest=request.digest,
                request_json=dumps(request.record()),
                policy_json=dumps(prepared.policy.record(with_config=False)),
                configuration=prepared.configuration,
                member_manifest=prepared.member_manifest,
                manifest_commit=prepared.manifest_commit,
                created_at=now,
            )
            return self.load(attempt_id)

    def load(self, attempt_id):
        row = self.store.row("ci_requests", {"attempt_id": attempt_id})
        attempt = self.store.row("ci_attempts", {"id": attempt_id})
        if row is None or attempt is None:
            raise ValueError("source request snapshot is unavailable")
        policy = cc.ExecutionPolicy.parse(loads(row["policy_json"]), configuration=row["configuration"])
        if self.binding(policy) != self.store.get_setting(self.binding_key):
            raise ValueError("archived request repository roles changed")
        request = cc.CandidateRequest.parse(loads(row["request_json"]), policy=policy)
        if request.member_manifest is not None:
            request = request.bind_members(row["member_manifest"], policy=policy)
            _, commit = manifest_objects(
                row["member_manifest"], request.accepted_heads, epoch=request.epoch, request_id=request.request_id
            )
            if commit != row["manifest_commit"] or git_object("commit", commit) != request.member_manifest.commit:
                raise ValueError("archived membership commit differs from its request")
        elif row["member_manifest"] is not None or row["manifest_commit"] is not None:
            raise ValueError("inline request carries unrelated retained membership")
        if (
            request.digest != row["request_digest"]
            or request.request_id != row["request_id"]
            or request.epoch != self.epoch
            or request.request_id != attempt["request_id"]
            or (request.batch_id, request.batch_version, request.head, request.base, policy.base_branch)
            != (
                attempt["batch_id"],
                attempt["batch_version"],
                attempt["head_sha"],
                attempt["base_sha"],
                attempt["base_ref"],
            )
        ):
            raise ValueError("archived request differs from its attempt")
        return FrozenRequest(
            attempt_id, request, policy, row["configuration"], row["member_manifest"], row["manifest_commit"]
        )

    def restore_membership(self, frozen):
        """Restore only the exact data objects, without executing candidate code."""
        request = frozen.request
        if request.member_manifest is None:
            return
        tree, commit = manifest_objects(
            frozen.member_manifest, request.accepted_heads, epoch=request.epoch, request_id=request.request_id
        )
        if commit != frozen.manifest_commit or git_object("commit", commit) != request.member_manifest.commit:
            raise ValueError("membership objects differ from the archived request")
        for kind, raw in (("blob", frozen.member_manifest), ("tree", tree), ("commit", commit)):
            actual = self._git("hash-object", "-w", "-t", kind, "--stdin", raw=raw).decode("ascii").strip()
            if actual != git_object(kind, raw):
                raise ValueError("restored membership Git identity changed")

    def record_owned_run(self, attempt_id, run, proof, *, now):
        """Persist adapter-verified ownership and select one owner with a CAS.

        The caller must obtain the proof with the trusted execution artifact
        reader, never from a webhook, run title or source commit status. Both a
        request receipt and a verified final result can establish run ownership.
        Ownership alone grants neither a build verdict nor source approval.
        """
        frozen = self.load(attempt_id)
        if not isinstance(run.run_id, str) or not re.fullmatch(r"[1-9][0-9]*", run.run_id):
            raise ValueError("execution run needs a canonical native ID")
        if type(run.attempt) is not int or run.attempt < 1:
            raise ValueError("execution run needs a positive attempt")
        if (
            not isinstance(proof, dict)
            or type(proof.get("run_id")) is not int
            or str(proof["run_id"]) != run.run_id
            or proof.get("request_digest") != frozen.request.digest
            or proof.get("kind") not in {"request", "result"}
            or type(proof.get("artifact_id")) is not int
            or proof["artifact_id"] < 1
            or not isinstance(proof.get("artifact_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", proof["artifact_digest"])
        ):
            raise ValueError("execution ownership proof differs from the request")
        if proof["kind"] == "request":
            if (
                type(proof.get("receipt_attempt")) is not int
                or not 0 < proof["receipt_attempt"] <= run.attempt
                or type(proof.get("observed_attempt")) is not int
                or proof["observed_attempt"] != run.attempt
            ):
                raise ValueError("request receipt differs from the observed attempt")
        elif type(proof.get("attempt")) is not int or proof["attempt"] != run.attempt:
            raise ValueError("result proof differs from the observed attempt")
        with self.store.transaction():
            row = self.store.row(
                "ci_request_runs", {"execution_id": frozen.request.execution.native_id, "run_id": run.run_id}
            )
            if row is not None and row["request_digest"] != frozen.request.digest:
                raise ValueError("execution run is already owned by another request")
            if row is None:
                self.store.insert(
                    "ci_request_runs",
                    request_digest=frozen.request.digest,
                    execution_id=frozen.request.execution.native_id,
                    run_id=run.run_id,
                    receipt_json=dumps(proof),
                    first_observed_at=now,
                )
            # A duplicated accepted dispatch can produce several valid runs.
            # Record every proven run, but never overwrite the selected owner.
            self.store.update("ci_requests", {"attempt_id": attempt_id, "owner_run_id": None}, owner_run_id=run.run_id)
            return self.store.row("ci_requests", {"attempt_id": attempt_id})["owner_run_id"]

    def owned_runs(self, attempt_id):
        frozen = self.load(attempt_id)
        return self.store.rows("ci_request_runs", {"request_digest": frozen.request.digest}, order="id")

    def owner_run(self, attempt_id):
        self.load(attempt_id)
        return self.store.row("ci_requests", {"attempt_id": attempt_id})["owner_run_id"]

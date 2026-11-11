"""Explicit private-file launch inputs for the existing native queue bootstrap.

Each input is snapshotted through the existing checked-descriptor reader. Source
credentials have their own origin binding; project/runtime snapshots have approved
digests. No environment or gh credential discovery supplies a native launch.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from tauceti_tools import ci_candidate as cc
from tauceti_tools.forge import Forge, Forgejo, PrincipalRef
from tauceti_tools.project import parse as parse_project

from . import config as configuration
from .control_policy_file import MAX_POLICY_BYTES, read_control_policy
from .forgejo_source import NativePublishers
from .github_adapter import UrllibTransport
from .github_app import InstallationTokenSource
from .github_app import load_execution_credentials as _execution_credentials
from .native_bootstrap import NativeBootstrap
from .source_authority import QueueControlPolicy

SCHEMA = "tauceti-queue.native-launch/v1"
_REQUIRED_RUNTIME = {
    "repository",
    "canonical_branch",
    "content_clone",
    "database",
    "credentials_dir",
    "log_dir",
    "listen_host",
    "listen_port",
    "public_url",
    "admission_source",
    "admission_rubrics",
    "admission_lean_root",
}


class NativeLaunchError(configuration.ConfigError):
    """Launch inputs are unavailable or do not match their approved bindings."""


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("invalid launch fields")
    return value


def _path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n"):
        raise ValueError("an explicit launch path is required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute launch path is required")
    return path


def _read_json(path):
    return cc.read_json(read_control_policy(path), max_bytes=MAX_POLICY_BYTES)


def _snapshot(path, digest):
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid launch digest")
    value = read_control_policy(path)
    if hashlib.sha256(value).hexdigest() != digest:
        raise ValueError("launch snapshot changed")
    return value


def _token(path):
    value = read_control_policy(_path(path)).strip()
    if not 0 < len(value) <= 4096 or any(not 33 <= char <= 126 for char in value):
        raise ValueError("invalid native credential")
    return value.decode("ascii")


def _publishers(value, source):
    _fields(value, {"generation", "reviews", "statuses", "recoveries"})
    roles = {}
    for role in ("reviews", "statuses", "recoveries"):
        ids = value[role]
        if (
            not isinstance(ids, list)
            or not 0 < len(ids) <= 256
            or any(type(number) is not int or not 0 < number < 2**63 for number in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("invalid native publisher identities")
        roles[role] = frozenset(PrincipalRef(source.repository.forge, str(number)) for number in ids)
    return NativePublishers(source, **roles, generation=value["generation"])


@dataclass(frozen=True)
class NativeLaunch:
    config: configuration.Config
    bootstrap: NativeBootstrap = field(repr=False)

    def summary(self):
        """Local configuration evidence only; no credentials or control fingerprints."""
        policy = self.bootstrap.execution_policy
        return {
            "schema": SCHEMA,
            "source": policy.source.record(),
            "execution": policy.execution.record(),
            "workflow_revision": self.bootstrap.workflow_revision,
            "configuration_sha256": policy.config_digest,
        }

    @classmethod
    def load(cls, path):
        phase = "profile"
        try:
            row = _read_json(_path(str(path)))
            _fields(
                row,
                {
                    "schema",
                    "queue_configuration",
                    "queue_configuration_sha256",
                    "project_configuration",
                    "project_configuration_sha256",
                    "execution_policy",
                    "workflow_revision",
                    "publishers",
                    "control_policy_file",
                    "source_credentials",
                    "source_webhook_secret_file",
                },
            )
            if row["schema"] != SCHEMA:
                raise ValueError("invalid native launch schema")
            phase = "runtime configuration"
            queue_path = _path(row["queue_configuration"])
            queue_bytes = _snapshot(queue_path, row["queue_configuration_sha256"])
            flat = configuration.read_file(queue_path, snapshot=queue_bytes)
            if not _REQUIRED_RUNTIME <= flat.keys():
                raise ValueError("native runtime destinations must be explicit")
            for key in ("content_clone", "database", "credentials_dir", "log_dir"):
                _path(flat[key])
            config = configuration.load(queue_path, environ={}, snapshot=queue_bytes)
            phase = "project configuration"
            project_bytes = _snapshot(_path(row["project_configuration"]), row["project_configuration_sha256"])
            project = parse_project(project_bytes)
            phase = "source/execution policy"
            policy = cc.ExecutionPolicy.parse(row["execution_policy"], configuration=project_bytes)
            if (
                project.repository_ref("content") != policy.source.repository
                or project.ci_repository_ref() != policy.execution.repository
                or project.branch != policy.base_branch
                or config.repository != policy.source.repository.name
                or config.canonical_branch != policy.base_branch
                or config.admission_source != "policy"
                or config.admission_lean_root != project.roots[0]
            ):
                raise ValueError("native configuration differs from approved policy")
            rubrics = project.raw.get("review", {}).get("rubrics")
            if not isinstance(rubrics, list) or not rubrics or config.admission_rubrics.split(",") != rubrics:
                raise ValueError("native review configuration differs from approved project")
            phase = "workflow revision"
            revision = row["workflow_revision"]
            if (
                not isinstance(revision, str)
                or re.fullmatch(r"[0-9a-f]{40}", revision) is None
                or not int(revision, 16)
            ):
                raise ValueError("native workflow revision is invalid")
            phase = "publisher identities"
            publishers = _publishers(row["publishers"], policy.source)
            phase = "control policy"
            control = QueueControlPolicy.load(_path(row["control_policy_file"]), source=policy.source)
            phase = "source credential binding"
            source = _fields(row["source_credentials"], {"origin", "api_token_file", "git_token_file"})
            origin = _fields(source["origin"], {"kind", "url"})
            origin = Forge(origin["kind"], origin["url"])
            if origin != policy.credential_origin:
                raise ValueError("source credential origin differs from approved policy")
            phase = "source credentials"
            api_token, git_token = _token(source["api_token_file"]), _token(source["git_token_file"])
            if control.permits(api_token) or control.permits(git_token):
                raise ValueError("queue controls must not enroll controller publication credentials")
            secret = _token(row["source_webhook_secret_file"]).encode("ascii")
            if len(secret) < 32:
                raise ValueError("source webhook secret is too short")
            phase = "execution credentials"
            credentials = _execution_credentials(config.credentials_dir)
            tokens = InstallationTokenSource(
                credentials,
                UrllibTransport(),
                repository_id=policy.execution.native_id,
                permissions={"contents": "write", "actions": "write"},
            )
            client = Forgejo(origin, token=api_token, timeout=10, max_bytes=8 * 1024 * 1024)
            bootstrap = NativeBootstrap(
                project_bytes, policy, revision, publishers, control, client, git_token, tokens, secret
            )
            return cls(config, bootstrap)
        except Exception:
            # Parser errors can include source lines. Report only the failed
            # phase, never private JSON/TOML, credential bytes or fingerprints.
            raise NativeLaunchError(f"native launch {phase} is unavailable or invalid") from None

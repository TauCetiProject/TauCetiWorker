"""Fresh native caller/ACL observations for the source-bound queue HTTP path.

No bearer, actor or permission cache is used. Each request supplies its own token
to a new origin-bound client. Native effective-access evidence must include the
credential's repository restrictions and scopes as well as the current user ACL.
Source publication still needs its own native branch and approval checks.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from dataclasses import dataclass, field, fields

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Forgejo, Incomplete

from tauceti_queue.control_policy_file import MAX_POLICY_BYTES, ControlPolicyError, read_control_policy
from tauceti_worker.source_identity import SourceActor, SourceRepositoryAccess

CONTROL_POLICY_SCHEMA = "tauceti-queue.control-policy/v1"


class SourceAccessDenied(PermissionError):
    """The source definitively refused this caller or target binding."""

    def __init__(self, message, *, status=403):
        super().__init__(message)
        self.status = status


class SourceAuthorityUnavailable(RuntimeError):
    """Current native identity/ACL evidence could not be obtained."""


def control_token_fingerprint(source: RepositoryIdentity, token: str) -> str:
    """Hash one deliberately provisioned control bearer for one native source.

    This is trusted operator configuration, never a value accepted from an HTTP
    request or a Forgejo profile. Keep it out of task records and public output.
    Register only independently generated high-entropy control credentials.
    """
    if not isinstance(source, RepositoryIdentity) or source.repository.forge.kind != "forgejo":
        raise ValueError("control credentials require a qualified native source")
    if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("a native control bearer is required")
    audience = f"tauceti-queue.control-token/v1\0{source.repository.forge.key}\0{source.native_id}\0"
    return hashlib.sha256(audience.encode() + token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class QueueControlPolicy:
    """An explicit audience for dedicated queue-control credentials.

    A repository-wide write grant alone does not enroll a third-party token.
    Rotation provisions a new immutable policy and a new authority instance;
    evidence issued by the old instance cannot survive that replacement.
    """

    source: RepositoryIdentity
    fingerprints: frozenset[str] = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.source, RepositoryIdentity) or self.source.repository.forge.kind != "forgejo":
            raise ValueError("control policy requires a qualified native source")
        if (
            type(self.fingerprints) is not frozenset
            or not 0 < len(self.fingerprints) <= 1024
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None or value == "0" * 64
                for value in self.fingerprints
            )
        ):
            raise ValueError("control policy requires a bounded nonempty set of SHA-256 fingerprints")

    def permits(self, token: str) -> bool:
        candidate = control_token_fingerprint(self.source, token)
        # Examine every configured fingerprint, without leaking which entry
        # matched through short-circuit comparisons.
        return bool(sum(hmac.compare_digest(candidate, approved) for approved in self.fingerprints))

    def record(self) -> dict:
        """Private operator configuration; never attach this to status or tasks."""
        return {
            "schema": CONTROL_POLICY_SCHEMA,
            "source": self.source.record(),
            "fingerprints": sorted(self.fingerprints),
        }

    @classmethod
    def load(cls, path, *, source: RepositoryIdentity):
        """Read an explicit trusted file for this independently configured source."""
        from tauceti_tools.ci_candidate import read_json

        if not isinstance(source, RepositoryIdentity) or source.repository.forge.kind != "forgejo":
            raise ControlPolicyError("queue control policy needs an independently configured native source")
        payload = read_control_policy(path)
        loaded = None
        try:
            row = read_json(payload, max_bytes=MAX_POLICY_BYTES)
            if (
                not isinstance(row, dict)
                or set(row) != {"schema", "source", "fingerprints"}
                or row["schema"] != CONTROL_POLICY_SCHEMA
                or RepositoryIdentity.parse(row["source"]) != source
                or type(row["fingerprints"]) is not list
                or not 0 < len(row["fingerprints"]) <= 1024
                or len(set(row["fingerprints"])) != len(row["fingerprints"])
            ):
                raise ValueError
            loaded = cls(source, frozenset(row["fingerprints"]))
        except (ValueError, TypeError, KeyError):
            pass
        finally:
            # Do not retain JSONDecodeError.doc/UnicodeDecodeError.object in a
            # public exception chain, or private bytes in this traceback frame.
            del payload
            row = None
        if loaded is None:
            raise ControlPolicyError("queue control policy schema, source or fingerprints are invalid")
        return loaded


@dataclass(frozen=True)
class QueueRepositoryAccess(SourceRepositoryAccess):
    """User ACL and effective scoped credential rights from one native request."""

    credential_code_can_read: bool
    credential_code_can_push: bool
    credential_is_repo_admin: bool

    def __post_init__(self):
        super().__post_init__()
        if any(
            type(value) is not bool
            for value in (self.credential_code_can_read, self.credential_code_can_push, self.credential_is_repo_admin)
        ):
            raise ValueError("credential access flags must be explicit booleans")
        if (
            (self.credential_code_can_read and not self.user_code_can_read)
            or (
                self.credential_code_can_push
                and (not self.credential_code_can_read or not self.user_code_can_push or self.archived or self.mirror)
            )
            or (self.credential_is_repo_admin and not self.user_is_repo_admin)
        ):
            raise ValueError("credential access is inconsistent with its reduced user ACL")

    @classmethod
    def from_effective(cls, repository, row):
        if not isinstance(row, dict):
            raise Incomplete("native effective repository access is unavailable")
        actor = SourceActor.from_native(repository.forge, row.get("user"))
        acl = SourceRepositoryAccess.from_native(repository, actor, row.get("repository"))
        keys = ("code_read", "code_write", "repository_admin")
        if any(type(row.get(key)) is not bool for key in keys):
            raise Incomplete("native effective repository access flags are unavailable")
        try:
            return cls(
                **{entry.name: getattr(acl, entry.name) for entry in fields(SourceRepositoryAccess)},
                credential_code_can_read=row["code_read"],
                credential_code_can_push=row["code_write"],
                credential_is_repo_admin=row["repository_admin"],
            )
        except ValueError:
            raise Incomplete("native effective repository access is inconsistent with its user ACL") from None


def principal_key(actor: SourceActor) -> str:
    """An immutable qualified identity for journals, never a display login."""
    if not isinstance(actor, SourceActor):
        raise ValueError("source actor is required")
    return f"{actor.principal.forge.key}/users/{actor.principal.native_id}"


@dataclass(frozen=True)
class RequestAccess:
    """Process-local evidence; never persist or accept it from HTTP JSON."""

    access: QueueRepositoryAccess
    checked_at: float
    issuer: object = field(repr=False, compare=False)


class NativeQueueAuthority:
    @classmethod
    def from_control_file(cls, source: RepositoryIdentity, path, **kwargs):
        """Construct one authority shared by the server and controller at startup."""
        return cls(source, control_policy=QueueControlPolicy.load(path, source=source), **kwargs)

    def __init__(
        self,
        source: RepositoryIdentity,
        *,
        control_policy: QueueControlPolicy,
        client_factory=Forgejo,
        clock=time.monotonic,
    ):
        if not isinstance(source, RepositoryIdentity) or source.repository.forge.kind != "forgejo":
            raise ValueError("native queue authority requires a qualified Forgejo repository ID")
        if not isinstance(control_policy, QueueControlPolicy) or control_policy.source != source:
            raise ValueError("control credential policy must match the queue's qualified native source")
        self.source = source
        self._control_policy = control_policy
        self._client_factory = client_factory
        self.clock = clock

    def authenticate(self, token: str) -> RequestAccess:
        checked_at = self.clock()
        access = self.observe(token)
        return RequestAccess(access, checked_at, self)

    def validate(self, evidence: RequestAccess) -> QueueRepositoryAccess:
        """Bound freshness across a wait for the controller lock, with no I/O."""
        if (
            not isinstance(evidence, RequestAccess)
            or evidence.issuer is not self
            or not math.isfinite(evidence.checked_at)
            or not 0 <= self.clock() - evidence.checked_at <= 15
        ):
            raise SourceAuthorityUnavailable("native source authority needs a fresh request observation")
        access = evidence.access
        if (
            not isinstance(access, QueueRepositoryAccess)
            or access.repository != self.source.repository
            or access.repository_id != self.source.native_id
        ):
            raise SourceAccessDenied("native source authority belongs to another repository")
        return access

    def observe(self, token: str) -> QueueRepositoryAccess:
        """Read this token's actor and effective rights without retaining the token.

        Call after reading/validating the HTTP body, immediately before the queue
        operation. A prior observation never substitutes for a failed new read.
        A server without the effective-access endpoint cannot authorize controls.
        """
        self.require_control_credential(token)
        try:
            api = self._client_factory(self.source.repository.forge, token=token, timeout=10, max_bytes=1024 * 1024)
            access = QueueRepositoryAccess.from_effective(
                self.source.repository,
                api.request("GET", f"repositories/{self.source.native_id}/effective_access"),
            )
        except Incomplete:
            raise SourceAuthorityUnavailable("native source identity or repository ACL is incomplete") from None
        except ForgeError as error:
            if error.status in {401, 403, 404}:
                raise SourceAccessDenied(
                    "native source denied caller or repository access", status=401 if error.status == 401 else 403
                ) from None
            raise SourceAuthorityUnavailable("native source authority could not be read") from None
        if access.repository_id != self.source.native_id:
            raise SourceAccessDenied("native source repository identity changed")
        return access

    def require_control_credential(self, token: str) -> None:
        """Cheap local enrollment only; this never supplies actor or ACL evidence."""
        if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
            raise SourceAccessDenied("a native source bearer token is required", status=401)
        if not self._control_policy.permits(token):
            raise SourceAccessDenied("a dedicated credential approved for this queue source is required")

    def require_admin(self, access: QueueRepositoryAccess) -> None:
        """Validate a fresh observation against this queue's writable source."""
        if (
            not isinstance(access, QueueRepositoryAccess)
            or access.repository != self.source.repository
            or access.repository_id != self.source.native_id
            or not access.user_is_repo_admin
            or not access.user_code_can_push
            or not access.credential_code_can_read
            or not access.credential_code_can_push
            or not access.credential_is_repo_admin
            or access.archived
            or access.mirror
        ):
            raise SourceAccessDenied(
                "current source repository admin and scoped credential Code write access are required"
            )

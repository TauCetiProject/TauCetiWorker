"""Fresh native caller/ACL observations for the source-bound queue HTTP path.

No bearer, actor or permission cache is used. Each request supplies its own token
to a new origin-bound client. These observations authorize queue controls under
their policy; source publication still needs its own native scope, branch and
approval checks.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import ForgeError, Forgejo, Incomplete

from tauceti_worker.source_identity import SourceActor, SourceRepositoryAccess


class SourceAccessDenied(PermissionError):
    """The source definitively refused this caller or target binding."""

    def __init__(self, message, *, status=403):
        super().__init__(message)
        self.status = status


class SourceAuthorityUnavailable(RuntimeError):
    """Current native identity/ACL evidence could not be obtained."""


def principal_key(actor: SourceActor) -> str:
    """An immutable qualified identity for journals, never a display login."""
    if not isinstance(actor, SourceActor):
        raise ValueError("source actor is required")
    return f"{actor.principal.forge.key}/users/{actor.principal.native_id}"


@dataclass(frozen=True)
class RequestAccess:
    """Process-local evidence; never persist or accept it from HTTP JSON."""

    access: SourceRepositoryAccess
    checked_at: float
    issuer: object = field(repr=False, compare=False)


class NativeQueueAuthority:
    def __init__(self, source: RepositoryIdentity, *, client_factory=Forgejo, clock=time.monotonic):
        if not isinstance(source, RepositoryIdentity) or source.repository.forge.kind != "forgejo":
            raise ValueError("native queue authority requires a qualified Forgejo repository ID")
        self.source = source
        self._client_factory = client_factory
        self.clock = clock

    def authenticate(self, token: str) -> RequestAccess:
        checked_at = self.clock()
        access = self.observe(token)
        return RequestAccess(access, checked_at, self)

    def validate(self, evidence: RequestAccess) -> SourceRepositoryAccess:
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
            not isinstance(access, SourceRepositoryAccess)
            or access.repository != self.source.repository
            or access.repository_id != self.source.native_id
        ):
            raise SourceAccessDenied("native source authority belongs to another repository")
        return access

    def observe(self, token: str) -> SourceRepositoryAccess:
        """Read this token's actor and user ACL fresh, without retaining the token.

        Call after reading/validating the HTTP body, immediately before the queue
        operation. A prior observation never substitutes for a failed new read.
        The user ACL does not establish the token's write scopes.
        """
        if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
            raise SourceAccessDenied("a native source bearer token is required", status=401)
        try:
            api = self._client_factory(self.source.repository.forge, token=token, timeout=10, max_bytes=1024 * 1024)
            actor = SourceActor.from_native(self.source.repository.forge, api.request("GET", "user"))
            access = SourceRepositoryAccess.from_native(
                self.source.repository,
                actor,
                api.request("GET", "repos/" + self.source.repository.name),
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

    def require_admin(self, access: SourceRepositoryAccess) -> None:
        """Validate a fresh observation against this queue's writable source."""
        if (
            not isinstance(access, SourceRepositoryAccess)
            or access.repository != self.source.repository
            or access.repository_id != self.source.native_id
            or not access.user_is_repo_admin
            or not access.user_code_can_push
            or access.archived
            or access.mirror
        ):
            raise SourceAccessDenied("current source repository admin and Code write access are required")

"""Qualified source actors and native user ACL observations for survey adapters.

Logins are display metadata. A repository ACL observation does not prove that
the supplied token has write scopes or authorize bypassing native write checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from tauceti_tools.forge import Forge, Incomplete, PrincipalRef, RepositoryRef


def _login(row):
    if not isinstance(row, dict):
        raise Incomplete("source actor record is unavailable")
    login = row.get("login")
    if not isinstance(login, str) or not login or login.strip() != login or any(c in login for c in "\0\r\n"):
        raise Incomplete("source actor display login is unavailable")
    try:
        login.encode("utf-8")
    except UnicodeError:
        raise Incomplete("source actor display login is not valid UTF-8") from None
    return login


@dataclass(frozen=True)
class SourceActor:
    principal: PrincipalRef
    login: str = field(compare=False)

    @property
    def qualified_id(self) -> str:
        return self.principal.forge.key + "/users/" + self.principal.native_id

    def __post_init__(self):
        if (
            not isinstance(self.principal, PrincipalRef)
            or not isinstance(self.principal.forge, Forge)
            or not re.fullmatch(r"[1-9][0-9]*", self.principal.native_id)
        ):
            raise ValueError("source actor requires a canonical qualified native user ID")
        try:
            _login({"login": self.login})
        except Incomplete:
            raise ValueError("source actor requires a valid display login") from None

    @classmethod
    def from_native(cls, forge, row):
        if not isinstance(forge, Forge):
            raise Incomplete("source actor origin is unavailable")
        login = _login(row)
        if type(row.get("id")) is not int or row["id"] < 1:
            raise Incomplete("source actor has no authenticatable native identity")
        return cls(PrincipalRef(forge, str(row["id"])), login)

    @classmethod
    def from_projection(cls, forge, row):
        if not isinstance(forge, Forge):
            raise Incomplete("projected actor origin is unavailable")
        login = _login(row)
        identity = row.get("id")
        prefix = forge.key + "/users/"
        if (
            not isinstance(identity, str)
            or not identity.startswith(prefix)
            or not re.fullmatch(r"[1-9][0-9]*", identity[len(prefix) :])
        ):
            raise Incomplete("projected actor has no identity on the selected source")
        return cls(PrincipalRef(forge, identity[len(prefix) :]), login)


@dataclass(frozen=True)
class SourceRepositoryAccess:
    """Native Code-unit user ACL and repo role; no token or branch authority."""

    repository: RepositoryRef
    repository_id: int
    actor: SourceActor
    user_code_can_read: bool
    user_code_can_push: bool
    user_is_repo_admin: bool
    archived: bool
    mirror: bool

    def __post_init__(self):
        if (
            not isinstance(self.repository, RepositoryRef)
            or not isinstance(self.repository.forge, Forge)
            or not isinstance(self.actor, SourceActor)
            or self.actor.principal.forge != self.repository.forge
            or type(self.repository_id) is not int
            or self.repository_id < 1
        ):
            raise ValueError("repository access requires matching qualified source identities")
        if any(
            type(value) is not bool
            for value in (
                self.user_code_can_read,
                self.user_code_can_push,
                self.user_is_repo_admin,
                self.archived,
                self.mirror,
            )
        ):
            raise ValueError("repository access flags must be explicit booleans")
        if self.user_code_can_push and not self.user_code_can_read:
            raise ValueError("Code-unit write access requires Code-unit read access")

    @classmethod
    def from_native(cls, repository, actor, row):
        if (
            not isinstance(repository, RepositoryRef)
            or not isinstance(actor, SourceActor)
            or actor.principal.forge != repository.forge
        ):
            raise Incomplete("repository access actor belongs to another source")
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1:
            raise Incomplete("repository access has no native repository identity")
        if (
            not isinstance(row.get("full_name"), str)
            or row["full_name"].casefold() != repository.name.casefold()
            or not isinstance(row.get("html_url"), str)
        ):
            raise Incomplete("repository access did not identify its requested repository")
        url, expected = urlsplit(row["html_url"]), urlsplit(repository.url)
        if (
            (url.scheme, url.netloc, url.path.casefold())
            != (expected.scheme, expected.netloc, expected.path.casefold())
            or url.query
            or url.fragment
        ):
            raise Incomplete("repository access belongs to another origin or repository")
        permissions = row.get("permissions")
        if not isinstance(permissions, dict) or any(
            type(permissions.get(key)) is not bool for key in ("pull", "push", "admin")
        ):
            raise Incomplete("native repository user ACL is unavailable")
        if permissions["push"] and not permissions["pull"]:
            raise Incomplete("native repository user ACL is inconsistent")
        if any(type(row.get(key)) is not bool for key in ("archived", "mirror")):
            raise Incomplete("native repository archive or mirror state is unavailable")
        return cls(
            repository,
            row["id"],
            actor,
            permissions["pull"],
            permissions["push"],
            permissions["admin"],
            row["archived"],
            row["mirror"],
        )

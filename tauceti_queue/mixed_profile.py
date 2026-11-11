"""Explicit, private enrollment of the second queue source.

Nothing calls this loader in disabled mode. Source credentials and CI/primary
writer credentials have independent roles; no ambient gh token enrolls Forgejo.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from tauceti_tools.ci_candidate import RepositoryIdentity, read_json
from tauceti_tools.forge import GITHUB, Forgejo, PrincipalRef
from tauceti_tools.system import ensure_ssl_cert_file

from .config import ConfigError
from .control_policy_file import read_control_policy
from .forgejo_source import NativePublishers

SCHEMA = "tauceti-queue.mixed-sources/v1"


def private_json(path):
    return read_json(read_control_policy(Path(path)))


def credential(path):
    value = read_control_policy(Path(path)).decode("ascii").strip()
    if not 0 < len(value) <= 4096 or not all(33 <= ord(char) <= 126 for char in value):
        raise ValueError("invalid source credential")
    return value


@dataclass(frozen=True)
class MixedProfile:
    github: RepositoryIdentity
    forgejo: RepositoryIdentity
    reviewers: frozenset[int]
    publisher: int
    token_file: Path = field(repr=False)
    raw: dict = field(repr=False)

    @classmethod
    def load(cls, path, config):
        try:
            raw = private_json(path)
            required = {"schema", "github", "forgejo", "reviewer_ids", "publisher_id", "source_token_file"}
            optional = {"execution", "mirror"}
            if not isinstance(raw, dict) or set(raw) - optional != required or raw["schema"] != SCHEMA:
                raise ValueError
            github = RepositoryIdentity.parse(raw["github"])
            native = RepositoryIdentity.parse(raw["forgejo"])
            if (
                github.repository.forge != GITHUB
                or github.repository.name != config.repository
                or native.repository.forge.kind != "forgejo"
            ):
                raise ValueError
            reviewers = raw["reviewer_ids"]
            if (
                not isinstance(reviewers, list)
                or not 0 < len(reviewers) <= 256
                or any(type(i) is not int or not 0 < i < 2**63 for i in reviewers)
                or len(set(reviewers)) != len(reviewers)
            ):
                raise ValueError
            publisher = raw["publisher_id"]
            if type(publisher) is not int or not 0 < publisher < 2**63:
                raise ValueError
            token_file = Path(raw["source_token_file"])
            if not token_file.is_absolute():
                raise ValueError
            if config.forgejo_mode == "admit" and ("execution" not in raw or "mirror" not in raw):
                raise ValueError("mixed admission requires explicit execution and mirror roles")
            return cls(github, native, frozenset(reviewers), publisher, token_file, raw)
        except (ValueError, KeyError, TypeError, OSError, UnicodeError):
            raise ConfigError("invalid or unavailable private Forgejo queue profile") from None

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True).encode()).hexdigest()

    def publishers(self):
        forge = self.forgejo.repository.forge
        return NativePublishers(
            self.forgejo,
            frozenset(PrincipalRef(forge, str(i)) for i in self.reviewers | {self.publisher}),
            frozenset({PrincipalRef(forge, str(self.publisher))}),
            recoveries=frozenset({PrincipalRef(forge, str(self.publisher))}),
            generation=self.digest[:32],
        )

    def client(self):
        ensure_ssl_cert_file()
        return Forgejo(self.forgejo.repository.forge, token=credential(self.token_file))

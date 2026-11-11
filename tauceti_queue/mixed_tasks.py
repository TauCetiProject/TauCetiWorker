"""Mixed repair tasks use GitHub retention with independently qualified members."""

from __future__ import annotations

import re

from tauceti_tools.ci_candidate import RepositoryIdentity, digest
from tauceti_tools.forge import GITHUB

from .mixed_identity import SourceNumber

SCHEMA = "tauceti-queue.mixed-task/v1"


def binding(registry):
    return {
        "schema": SCHEMA,
        "github": registry.profile.github.record(),
        "forgejo": registry.profile.forgejo.record(),
        "policy_digest": digest(registry.ci.ledger.binding(registry.ci.ledger.policy)),
        "epoch": registry.store.get_setting("mixed-ci:epoch"),
    }


def validate(row):
    value = row.get("mixed_binding")
    if (
        row.get("schema") != SCHEMA
        or not isinstance(value, dict)
        or set(value) != {"schema", "github", "forgejo", "policy_digest", "epoch"}
    ):
        raise ValueError("mixed task needs its explicit queue binding")
    if value["schema"] != SCHEMA:
        raise ValueError("mixed task binding schema is unavailable")
    github, native = RepositoryIdentity.parse(value["github"]), RepositoryIdentity.parse(value["forgejo"])
    if (
        github.repository.forge != GITHUB
        or native.repository.forge.kind != "forgejo"
        or row.get("repository") != github.repository.name
    ):
        raise ValueError("mixed task retention repository differs from its queue binding")
    if (
        not isinstance(value["policy_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["policy_digest"]) is None
        or not isinstance(value["epoch"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["epoch"]) is None
    ):
        raise ValueError("mixed task authority generation is unavailable")
    seen = set()
    for member in row.get("members", []):
        source = RepositoryIdentity.parse(member.get("source_identity"))
        number = member.get("number")
        if type(number) is not int or not 0 < number < 2**63 or source not in {github, native}:
            raise ValueError("mixed task member is outside the enrolled source identities")
        key = (source, number)
        if key in seen:
            raise ValueError("duplicate mixed task member")
        seen.add(key)
        expected = SourceNumber(number, native).source_pr if source == native else f"{github.repository.name}#{number}"
        if member.get("source_pr") != expected:
            raise ValueError("mixed task member provenance differs from its source identity")
    if not seen:
        raise ValueError("mixed task has no members")
    return github, native

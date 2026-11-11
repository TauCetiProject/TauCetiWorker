"""Source-qualified pull identities carried through existing queue records.

Legacy GitHub records keep their integer numbers and byte representation. New
native records retain the native number plus an explicit repository identity.
The integer projection is only for the existing presentation/protocol fields;
Python identity and durable metadata include the source server and repository ID.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from tauceti_tools.ci_candidate import RepositoryIdentity, digest
from tauceti_tools.forge import Forge, RepositoryRef


class SourceNumber(int):
    def __new__(cls, number, source):
        if type(number) is not int or not 0 < number < 2**63 or not isinstance(source, RepositoryIdentity):
            raise ValueError("a native pull requires its repository identity and positive number")
        if source.repository.forge.kind != "forgejo":
            raise ValueError("GitHub numbers retain their existing queue representation")
        result = int.__new__(cls, number)
        object.__setattr__(result, "source", source)
        return result

    def __setattr__(self, name, value):
        raise AttributeError("pull source identity is immutable")

    def __hash__(self):
        return hash((self.source, int(self)))

    def __eq__(self, other):
        return isinstance(other, SourceNumber) and self.source == other.source and int(self) == int(other)

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        return self.source_pr

    def __reduce__(self):
        return (SourceNumber, (int(self), self.source))

    @property
    def source_pr(self):
        return f"{self.source.repository.forge.key}/{self.source.repository.name}@{self.source.native_id}#{int(self)}"

    @property
    def ref_key(self):
        return digest(self.source.record()) + "/" + str(int(self))

    @property
    def pull_url(self):
        return self.source.repository.pull_url(int(self))

    def record(self):
        return {"number": int(self), "source_identity": self.source.record()}


def from_source_pr(value):
    if not isinstance(value, str) or not value.startswith("forgejo:") or "@" not in value:
        return int(value.rsplit("#", 1)[1])
    location, number = value.rsplit("#", 1)
    url, identifier = location.removeprefix("forgejo:").rsplit("@", 1)
    parsed = urlsplit(url)
    prefix, owner, name = parsed.path.rsplit("/", 2)
    origin = parsed.scheme + "://" + parsed.netloc + prefix
    source = RepositoryIdentity(RepositoryRef(Forge("forgejo", origin), owner + "/" + name), int(identifier))
    return SourceNumber(int(number), source)


def source_record(number):
    return (
        json.dumps(number.source.record(), sort_keys=True, separators=(",", ":"))
        if isinstance(number, SourceNumber)
        else ""
    )


def qualify_record(row):
    """Decode only an explicit durable column; never guess from a display login."""
    if row.get("source_identity") and type(row.get("number")) is int:
        raw = row["source_identity"]
        identity = RepositoryIdentity.parse(json.loads(raw) if isinstance(raw, str) else raw)
        row["number"] = SourceNumber(row["number"], identity)
    return row


def encode(value):
    if isinstance(value, SourceNumber):
        return {"__tauceti_queue_pull__": value.source.record(), "number": int(value)}
    if isinstance(value, dict):
        return {str(key) if isinstance(key, SourceNumber) else key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode(value):
    if isinstance(value, dict) and set(value) == {"__tauceti_queue_pull__", "number"}:
        return SourceNumber(value["number"], RepositoryIdentity.parse(value["__tauceti_queue_pull__"]))
    return value


def public(value):
    """Qualified printable identities for diagnostic files and agent-facing listings."""
    if isinstance(value, SourceNumber):
        return str(value)
    if isinstance(value, dict):
        return {str(key) if isinstance(key, SourceNumber) else key: public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public(item) for item in value]
    return value

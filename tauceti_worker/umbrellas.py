"""tauceti_worker.umbrellas — named sets of roadmap areas that `--roadmap-only NAME` can steer toward.

An umbrella NAME is the file `umbrellas/NAME.toml` on the default branch of the roadmap repository
(docs/umbrellas.md). It is read the way config.roadmap_areas reads that repository: from the shared
snapshot, else through the worker's GitHub client, and a read that fails counts as "no umbrella". A
roadmap area of the same name always wins, so a name that is neither an area nor an umbrella keeps
exactly the meaning it had before umbrellas existed.
"""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import tomllib
from dataclasses import dataclass

from .config import Die, log, roadmap_areas, roadmap_only, roadmap_skip
from .constants import ROADMAP, ROADMAP_DIR

UMBRELLA_DIR = "umbrellas"
# A file name, never a path. Anything else is not an umbrella and keeps its old meaning.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class Member:
    """One `[[area]]` of an umbrella file."""

    area: str
    sections: tuple[str, ...] = ()
    weight: int = 1


@dataclass(frozen=True)
class Umbrella:
    name: str
    description: str
    members: tuple[Member, ...]

    def member(self, area: str | None) -> Member | None:
        return next((m for m in self.members if m.area == area), None)


@dataclass(frozen=True)
class UmbrellaRound:
    """An umbrella as one round uses it: `scope` is the member areas whose pull requests the round
    maintains (every member outside --roadmap-skip), and `area` the member drawn for authoring, or None
    when a target list chooses the area or the survey is only displayed."""

    umbrella: Umbrella
    scope: frozenset[str]
    area: str | None = None


def is_candidate(name: str | None) -> bool:
    """Whether `name` could name an umbrella at all: not unset, all areas, or a path."""
    return bool(name) and name not in ("any", "auto") and bool(_NAME_RE.fullmatch(name))


def parse_umbrella(name: str, data: bytes) -> Umbrella:
    """Parse an umbrella file; Die, naming the file, when it does not parse or lists no area."""
    where = f"umbrella {name} ({UMBRELLA_DIR}/{name}.toml in {ROADMAP})"
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Die(f"{where} does not parse: {error}") from None
    description = document.get("description", "")
    if not isinstance(description, str):
        raise Die(f"{where} does not parse: description must be a string")
    tables = document.get("area", [])
    if not isinstance(tables, list) or not all(isinstance(table, dict) for table in tables):
        raise Die(f"{where} does not parse: areas are written as [[area]] tables")
    members: list[Member] = []
    for index, table in enumerate(tables, 1):
        area = table.get("name")
        if not isinstance(area, str) or not _NAME_RE.fullmatch(area):
            raise Die(f"{where} does not parse: [[area]] {index} needs a name, a top-level directory of {ROADMAP_DIR}/")
        if any(member.area == area for member in members):
            raise Die(f"{where} does not parse: {area} is listed twice")
        sections = table.get("sections", [])
        if not isinstance(sections, list) or not all(isinstance(s, str) and s.strip() for s in sections):
            raise Die(f"{where} does not parse: sections of {area} must be a list of non-empty strings")
        weight = table.get("weight", 1)
        if type(weight) is not int or weight < 1:
            raise Die(f"{where} does not parse: weight of {area} must be a positive integer")
        members.append(Member(area, tuple(" ".join(s.split()) for s in sections), weight))
    if not members:
        raise Die(f"{where} lists no area: add an [[area]] table for each roadmap area it covers")
    return Umbrella(name, " ".join(description.split()), tuple(members))


def _umbrella_file(gh, name: str) -> bytes | None:
    """The bytes of umbrellas/NAME.toml, or None when it is absent, unreadable, or NAME is an area."""
    from .shared_repositories import borrowed_snapshot

    try:
        with borrowed_snapshot(ROADMAP, ttl=3600) as tree:
            if tree:
                if (tree / ROADMAP_DIR / name).is_dir():
                    return None  # an area of that name always wins
                path = tree / UMBRELLA_DIR / f"{name}.toml"
                return path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except (OSError, TimeoutError, subprocess.SubprocessError):
        pass
    encoded = gh.api_jq(f"repos/{ROADMAP}/contents/{UMBRELLA_DIR}/{name}.toml", 'select(.type == "file") | .content')
    if not encoded or name in roadmap_areas(gh):
        return None
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return None


def read_umbrella(gh, name: str | None) -> Umbrella | None:
    """The umbrella `name` names, or None when it names none (an area, no file, or no readable file)."""
    if not is_candidate(name):
        return None
    data = _umbrella_file(gh, name)
    return None if data is None else parse_umbrella(name, data)


def umbrella_scope(gh, name: str | None, *, display: bool = False) -> UmbrellaRound | None:
    """The umbrella `name` names, scoped by --roadmap-skip, or None. A file that does not parse stops a
    round; a display (`status`, the dashboard) logs it and shows the name as it would show an area."""
    try:
        umbrella = read_umbrella(gh, name)
    except Die as error:
        if not display:
            raise
        log(f"{error}; showing {name} without its umbrella")
        return None
    if umbrella is None:
        return None
    skip = set(roadmap_skip())
    return UmbrellaRound(umbrella, frozenset(m.area for m in umbrella.members if m.area not in skip))


def display_umbrella(gh) -> UmbrellaRound | None:
    """The umbrella a status or dashboard survey shows for the current --roadmap-only, if any."""
    return umbrella_scope(gh, roadmap_only(), display=True)


def umbrella_prompt(scope: UmbrellaRound | None, area: str, *, assigned: bool = False) -> str:
    """The roadmap prompt's umbrella sentences, or "" outside an umbrella round. They follow the sentence
    naming the assigned roadmap; the section preference applies only to a drawn area, not to an item a
    target list assigned."""
    if scope is None:
        return ""
    umbrella = scope.umbrella
    text = (
        f" It is a member of the umbrella `{umbrella.name}`"
        if assigned
        else f" It was drawn from the umbrella `{umbrella.name}`"
    )
    if umbrella.description:
        text += f": {umbrella.description}" + ("" if umbrella.description.endswith((".", "!", "?")) else ".")
    else:
        text += "."
    member = None if assigned else umbrella.member(area)
    if member is not None and member.sections:
        text += (
            f" Within `{area}`, prefer the targets of {', '.join(member.sections)}, which the umbrella's goal "
            f"cites; if all of them are done or claimed, any other target of `{area}` is acceptable."
        )
    return text

"""Which members of a failed candidate caused the failure, so escalation can withdraw them by name.

Bisection isolates a failing member in about log2(n) CI rounds and discards the repairs made to the
members it sends back; at a hundred members and an hour per round that is most of a day. So before
the controller bisects (`docs/queue/escalation.md`) it attributes the failure from evidence it already
holds, strongest first:

1. **Named.** A finding names its member: the source member review-size policy's
   `MEMBER-POLICY-REJECTED` record (`policy` findings carry the member's accepted head, PR number and
   source identity, so a Forgejo member is never confused with a GitHub PR of the same number), or a
   finding CI attributed itself (`source_pr`, `pr`, `number`). `policy_references` and
   `escalation.named_members` read these; the controller resolves them against the head's members.
2. **Changed.** A finding located in a file (`path`, or a `TauCeti.*` module) that some members'
   commits in the failed candidate changed names those members.
3. **Imported.** A finding in a file no member changed names the members that changed the nearest
   files it imports, following project imports breadth first: the first level of the import closure
   that holds any member's change gives the candidates.
4. **Build configuration.** A finding attributed by neither names the members that changed the
   build configuration (`lean-toolchain`, `lake-manifest.json`, the lakefile), when any did: a
   dependency or toolchain move breaks modules nobody touched.

Each finding so gets a set of plausible members. The attribution is the smallest set covering every
attributed finding, taken greedily: findings with fewer candidates first, and a finding already
explained by a chosen member adds nothing, so an ambiguous finding only adds its candidates when no
definite one explains it. Findings with no location (a verdict without log findings, an unlocated
lint) stay unattributed; when nothing at all is attributed, or the attribution names more members
than `max_members`, the caller bisects.

Everything here is pure: the controller passes each member's changed paths in the failed candidate
and a function giving a file's direct project imports in that candidate's tree.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .escalation import NON_SOURCE_KINDS

# Files whose change can break a module no member touched.
BUILD_CONFIGURATION = frozenset({"lean-toolchain", "lake-manifest.json", "lakefile.lean", "lakefile.toml"})
# The most files a breadth-first import search visits per finding.
MAX_VISITED = 20000


@dataclass
class Attribution:
    """`members` (keys, in candidate order) and why each was named; `unattributed` finding lines."""

    members: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    unattributed: list[str] = field(default_factory=list)
    refused: str = ""

    def summary(self, label: Callable[[str], str] = str) -> str:
        if self.refused:
            return self.refused
        if not self.members:
            return "no finding could be attributed to a member"
        return "; ".join(f"{label(key)}: {self.reasons[key]}" for key in self.members)


def policy_references(diagnostics: Iterable[Any]) -> list[dict[str, Any]]:
    """The members the review-size policy's findings name, as `{"head", "number", "source"}`."""
    references = []
    for finding in diagnostics:
        if isinstance(finding, Mapping) and finding.get("kind") == "policy" and finding.get("member_head"):
            references.append(
                {
                    "head": finding["member_head"],
                    "number": finding.get("source_pr"),
                    "source": finding.get("source"),
                }
            )
    return references


def finding_path(finding: Mapping[str, Any]) -> str | None:
    """The repository path a finding is located in, if any."""
    path = finding.get("path")
    if isinstance(path, str) and path.endswith(".lean"):
        return path
    module = finding.get("module")
    if not isinstance(module, str) or not module:
        return None
    if module.endswith(".lean"):
        return module
    if module.startswith("TauCeti.") or module == "TauCeti":
        return module.replace(".", "/") + ".lean"
    return None


def _label(finding: Mapping[str, Any]) -> str:
    message = " ".join(str(finding.get("message") or "").split())
    return f"{finding.get('kind') or 'finding'} {finding.get('module') or ''}: {message}"[:300]


def attribute(
    diagnostics: Iterable[Any],
    order: Sequence[str],
    changes: Mapping[str, Iterable[str]],
    imports: Callable[[str], Iterable[str]],
    *,
    max_members: int = 0,
) -> Attribution:
    """Attribute the source findings to the members `order` lists (keys into `changes`, each member's
    changed paths in the failed candidate); `imports(path)` gives a file's direct project imports."""
    changed = {key: set(changes.get(key, ())) for key in order}
    owners: dict[str, set[str]] = {}
    for key, paths in changed.items():
        for path in paths:
            owners.setdefault(path, set()).add(key)
    configuration = {key for key, paths in changed.items() if paths & BUILD_CONFIGURATION}
    result = Attribution()
    explained: list[tuple[set[str], str]] = []
    for finding in diagnostics:
        if not isinstance(finding, Mapping) or finding.get("kind") in NON_SOURCE_KINDS | {"policy"}:
            continue
        path = finding_path(finding)
        candidates: set[str] = set()
        why = ""
        if path is not None:
            candidates = set(owners.get(path, ()))
            if candidates:
                why = f"changed {path}, where CI reported {_label(finding)}"
            else:
                level, seen, found = [path], {path}, None
                while level and found is None and len(seen) < MAX_VISITED:
                    following = []
                    for current in level:
                        for provider in imports(current):
                            if provider not in seen:
                                seen.add(provider)
                                following.append(provider)
                    hits = {p: owners[p] for p in following if p in owners}
                    if hits:
                        found = hits
                    level = following
                if found:
                    candidates = set().union(*found.values())
                    provider = sorted(found)[0]
                    why = f"changed {provider}, which {path} imports, where CI reported {_label(finding)}"
        if not candidates and configuration and finding.get("kind") in ("compiler", "audit", "lint"):
            candidates = set(configuration)
            why = f"changed the build configuration; CI reported {_label(finding)} in a module no member changed"
        if candidates:
            explained.append((candidates, why))
        else:
            result.unattributed.append(_label(finding))
    chosen: set[str] = set()
    for candidates, why in sorted(explained, key=lambda item: len(item[0])):
        if candidates & chosen:
            continue
        chosen |= candidates
        for key in candidates:
            result.reasons.setdefault(key, why if len(candidates) == 1 else why + f" (one of {len(candidates)})")
    result.members = [key for key in order if key in chosen]
    if max_members and len(result.members) > max_members:
        result.refused = (
            f"the findings implicate {len(result.members)} members, more than the {max_members} "
            "`attribution_max_members` allows; that looks systemic rather than one member's doing"
        )
        result.members = []
    return result

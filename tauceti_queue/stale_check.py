"""A cheap static check of whether `main` has moved under a PR since its base (decision 175).

No Lean, only text diffs in the clone: the PR's diff (merge base B to head H) against main's diff
(B to M). It ranks queued members for the main-integration backfill (`main_integration.py`); it is
an ordering signal, not a verdict. On the staging failures of 21 to 24 Nov it flagged all ten
stale-class culprits and a quarter of the members that turned out fine (ref-175).

The checks, each reported separately:

- `textual`: `git merge-tree` of M and H conflicts;
- `overlap`: `.lean` files changed by both sides;
- `pr_changes_main_uses`: declarations the PR removes or renames whose names main's added lines use,
  and modules the PR deletes or renames that main's added lines name;
- `main_changes_pr_uses`: the reverse;
- `dropped_imports`: PR files from which main removed an import;
- `duplicates`: declarations both sides add under the same name (or the same distinctive last
  component);
- `pin`: main changed `lake-manifest.json` or `lean-toolchain`.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field

DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|partial|unsafe|nonrec|scoped|local)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+([^\s:({\[]+)"
)
IDENTIFIER = re.compile(r"[A-Za-z_À-ɏͰ-Ͽ][\w'.!?₀-ₜÀ-Ͽ]*")
IMPORT = re.compile(r"^\s*(?:public\s+)?(?:meta\s+)?import\s+(\S+)")
NAME = re.compile(r"^[A-Za-z_][\w'.]*$")
PINS = ("lake-manifest.json", "lean-toolchain")
# A diff larger than this is not read: the check reports `unknown` (which ranks as flagged).
MAX_DIFF_BYTES = 64 * 1024 * 1024
LIST_LIMIT = 10


class Unavailable(Exception):
    """A commit is missing or a diff is too large to read."""


def _git(clone: str, *args: str) -> str:
    result = subprocess.run(["git", "-C", os.fspath(clone), *args], capture_output=True, check=False, timeout=600)
    if result.returncode not in (0, 1):
        raise Unavailable(result.stderr.decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
    if len(result.stdout) > MAX_DIFF_BYTES:
        raise Unavailable(f"git {args[0]} output exceeds {MAX_DIFF_BYTES} bytes")
    return result.stdout.decode("utf-8", "replace")


def _last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _distinctive(component: str) -> bool:
    """A last name component unlikely to collide by accident (`orderOf_eq`, not `ext`)."""
    return len(component) >= 8 and ("_" in component or any(ch.isupper() for ch in component[1:]))


def _good(name: str) -> bool:
    return bool(NAME.match(name)) and _distinctive(_last(name))


@dataclass
class DiffSummary:
    files: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)
    deleted_modules: set[str] = field(default_factory=set)
    removed_imports: dict[str, set[str]] = field(default_factory=dict)
    pin: bool = False


def summarize(patch: str, name_status: str) -> DiffSummary:
    """Declarations, identifiers and imports a `git diff -U0` adds and removes."""
    summary = DiffSummary()
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        if parts[0][0] in "DR" and parts[1].endswith(".lean"):
            summary.deleted_modules.add(parts[1][: -len(".lean")].replace("/", "."))
        for path in parts[1:]:
            if path.endswith(".lean"):
                summary.files.add(path)
            if path in PINS:
                summary.pin = True
    current = None
    for line in patch.split("\n"):
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            continue
        if line.startswith("--- ") or not current or not current.endswith(".lean"):
            continue
        if line.startswith("+"):
            body = line[1:]
            declaration = DECLARATION.match(body)
            if declaration:
                summary.added.add(declaration.group(1))
            imported = IMPORT.match(body)
            if imported:
                summary.identifiers.add(imported.group(1))
                continue
            for token in IDENTIFIER.findall(body.split("--")[0]):
                summary.identifiers.add(token)
                summary.identifiers.update(part for part in token.split(".") if part)
        elif line.startswith("-"):
            body = line[1:]
            declaration = DECLARATION.match(body)
            if declaration:
                summary.removed.add(declaration.group(1))
            imported = IMPORT.match(body)
            if imported:
                summary.removed_imports.setdefault(current, set()).add(imported.group(1))
    return summary


def diff_summary(clone: str, a: str, b: str) -> DiffSummary:
    patch = _git(clone, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", a, b, "--", "*.lean")
    names = _git(clone, "diff", "--name-status", "--no-color", "-M", a, b)
    return summarize(patch, names)


class StaleCheck:
    """`check(base, head, main)` with main's side cached by (base, main), since members share both."""

    def __init__(self, clone: str, *, cache_size: int = 16) -> None:
        self.clone = os.fspath(clone)
        self._main: OrderedDict[tuple[str, str], DiffSummary] = OrderedDict()
        self._cache_size = cache_size

    def _main_side(self, base: str, main: str) -> DiffSummary:
        key = (base, main)
        if key in self._main:
            self._main.move_to_end(key)
            return self._main[key]
        summary = diff_summary(self.clone, base, main)
        self._main[key] = summary
        while len(self._main) > self._cache_size:
            self._main.popitem(last=False)
        return summary

    def check(self, head: str, main: str) -> dict:
        """The flags of `head` against `main`, from their merge base. `unknown` (with a reason) when
        the commits or diffs are unavailable; callers rank that as flagged."""
        try:
            base = _git(self.clone, "merge-base", head, main).strip()
            if not re.fullmatch(r"[0-9a-f]{40}", base):
                raise Unavailable("no merge base")
            pr = diff_summary(self.clone, base, head)
            ours = self._main_side(base, main)
            tree = _git(self.clone, "merge-tree", "--write-tree", "--name-only", "--no-messages", main, head)
        except (Unavailable, subprocess.TimeoutExpired, OSError) as error:
            return {"unknown": str(error)[:300], "flagged": True}
        textual = [path for path in tree.splitlines()[1:] if path.strip()]
        overlap = sorted(pr.files & ours.files)
        pr_changes = sorted(
            name for name in pr.removed if _good(name) and (name in ours.identifiers or _last(name) in ours.identifiers)
        )
        pr_changes += sorted(module for module in pr.deleted_modules if module in ours.identifiers)
        main_changes = sorted(
            name for name in ours.removed if _good(name) and (name in pr.identifiers or _last(name) in pr.identifiers)
        )
        main_changes += sorted(module for module in ours.deleted_modules if module in pr.identifiers)
        dropped = sorted(path for path in pr.files if ours.removed_imports.get(path))
        main_last = {_last(name) for name in ours.added if _good(name)}
        duplicates = sorted(
            name for name in pr.added if _good(name) and (name in ours.added or _last(name) in main_last)
        )
        result = {
            "base": base,
            "textual": textual[:LIST_LIMIT],
            "overlap": overlap[:LIST_LIMIT],
            "pr_changes_main_uses": pr_changes[:LIST_LIMIT],
            "main_changes_pr_uses": main_changes[:LIST_LIMIT],
            "dropped_imports": dropped[:LIST_LIMIT],
            "duplicates": duplicates[:LIST_LIMIT],
            "pin": ours.pin,
        }
        result["flagged"] = bool(textual or overlap or pr_changes or main_changes or dropped or duplicates or ours.pin)
        return result

"""Ownership and declaration context for the repair agent, computed before it starts.

The first production repair of a 1,197-member head spent dozens of tool calls working out which
member changes which file, which members add the same declaration name and which removed names other
members still use. ``compute`` derives that once, from the text of each member's squash commit:

- ``.tauceti-repair/files.jsonl``: per member (``position``, ``pr``, ``commit``), the ``files`` its
  commit changes;
- ``.tauceti-repair/decls.jsonl``: per member, the declaration names it ``added`` and ``removed`` and the
  ``renamed`` pairs (a removal and an addition in the same file sharing a dotted or underscored prefix
  or suffix), with ``approximate: true`` because the names come from a line scan of the sources
  (``theorem``, ``lemma``, ``def``, ``abbrev``, ``instance``, ``structure``, ``class``, ``inductive``,
  ``axiom``, ``alias`` under their ``namespace``/``section`` context) rather than from the compiled
  environment; a ``private`` declaration is skipped, an anonymous ``instance`` has no name;
- ``.tauceti-repair/collisions.jsonl``: every collision (a name added by two or more members) and
  rename ripple (a name removed or renamed by one member and still referenced, whole-word, in another
  member's changed files at the chain tip or in the target's version of a file no member changed);
- a prompt section, "Likely collisions and renames", listing the collisions and ripples with member
  positions and files, capped at ``SECTION_LINES`` bullets with the files named for the rest.

Each member's changed files come from one ``git diff-tree`` and each changed Lean file's two versions
from ``git cat-file --batch``, both over a thread pool, so a 1,197-member head takes seconds rather
than minutes; the whole computation never raises (an error is recorded on the result and the prompt
section is left out) because the repair runs without this context as it did before.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import time
from pathlib import Path

from tauceti_queue import declaration_names as compiled_names
from tauceti_queue.repair import LISTINGS_DIR, ChainCommit, git

FILES_FILE = "files.jsonl"
DECLS_FILE = "decls.jsonl"
COLLISIONS_FILE = "collisions.jsonl"
SECTION_LINES = 40  # bullets in the prompt section; the rest are in the collisions file
REFERENCES_SHOWN = 4  # referencing files named per ripple in the prompt
GREP_CHUNK = 200  # names per `git grep` invocation
SHORT_NAME = 4  # a declaration's written (unqualified) name is searched for only from this length

MODIFIERS = r"(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|scoped|local)\s+)*"
DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*(?P<modifiers>"
    + MODIFIERS
    + r")(?P<kind>theorem|lemma|def|abbrev|instance|structure|class(?:\s+inductive)?|inductive|axiom|alias)\s+"
    r"(?:\(\s*priority\s*:=[^)]*\)\s*)?(?P<name>[^\s:(\[{⦃⟨⟪|,]+)"
)
NAMESPACE = re.compile(r"^\s*namespace\s+(\S+)")
SECTION = re.compile(r"^\s*(?:noncomputable\s+)?section(?:\s+\S+)?\s*$")
MUTUAL = re.compile(r"^\s*mutual\s*$")
END = re.compile(r"^\s*end(?:\s+\S+)?\s*$")
COMMENT_TOKEN = re.compile(r"/-|-/|--")
UNIVERSES = re.compile(r"\.\{[^}]*\}$")
NAME_TOKEN = re.compile(r"[._]")


# ---- declarations from source text ----------------------------------------------------------------


def _strip_comments(lines: list[str]) -> list[str]:
    """The lines with block comments (nesting, possibly spanning lines) and line comments removed."""
    out: list[str] = []
    depth = 0
    for line in lines:
        if "-" not in line:
            out.append(line if depth == 0 else "")
            continue
        kept: list[str] = []
        position = 0
        for match in COMMENT_TOKEN.finditer(line):
            token = match.group()
            if depth == 0:
                if token == "--":
                    kept.append(line[position : match.start()])
                    position = len(line)
                    break
                if token == "/-":
                    kept.append(line[position : match.start()])
                    depth = 1
            elif token == "/-":
                depth += 1
            elif token == "-/":
                depth -= 1
                position = match.end()
            # a stray `-/` at depth 0 and a `--` inside a block comment change nothing
        if depth == 0:
            kept.append(line[position:])
        out.append("".join(kept))
    return out


def declarations(text: str) -> dict[str, str]:
    """The declarations the text makes, full name to the name as written, under their
    ``namespace``/``section``/``mutual`` nesting; ``private`` declarations and anonymous instances are
    skipped, ``_root_.`` is honoured, universe parameters are dropped."""
    found: dict[str, str] = {}
    stack: list[str | None] = []  # a namespace's name, or None for a section or mutual block
    for line in _strip_comments(text.split("\n")):
        if not line.strip():
            continue
        match = NAMESPACE.match(line)
        if match:
            stack.append(match.group(1))
            continue
        if SECTION.match(line) or MUTUAL.match(line):
            stack.append(None)
            continue
        if END.match(line):
            if stack:
                stack.pop()
            continue
        match = DECLARATION.match(line)
        if not match or "private" in match.group("modifiers").split():
            continue
        written = UNIVERSES.sub("", match.group("name")).rstrip(".")
        if not written or written in {"where", "with", "extends"}:
            continue
        if written.startswith("_root_."):
            full = written[len("_root_.") :]
        else:
            prefix = ".".join(name for name in stack if name)
            full = f"{prefix}.{written}" if prefix else written
        found.setdefault(full, written)
    return found


def _tokens(name: str) -> list[str]:
    return [token for token in NAME_TOKEN.split(name.rstrip("'")) if token]


def _shared_tokens(old: str, new: str) -> int:
    """How many dotted or underscored tokens two names share at the start or the end (the larger)."""
    a, b = _tokens(old), _tokens(new)
    if not a or not b or a == b:
        return 0
    prefix = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    suffix = next(
        (i for i, (x, y) in enumerate(zip(reversed(a), reversed(b), strict=False)) if x != y), min(len(a), len(b))
    )
    return max(prefix, suffix)


def rename_pairs(removed: set[str], added: set[str]) -> list[tuple[str, str]]:
    """Removals and additions in one file paired as renames when they share a token-boundary prefix or
    suffix, the best-scoring pairs first and each name used once."""
    scored = sorted(
        ((score, old, new) for old in removed for new in added if (score := _shared_tokens(old, new)) > 0),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    pairs: list[tuple[str, str]] = []
    used_old: set[str] = set()
    used_new: set[str] = set()
    for _, old, new in scored:
        if old in used_old or new in used_new:
            continue
        pairs.append((old, new))
        used_old.add(old)
        used_new.add(new)
    return sorted(pairs)


# ---- per-member changes -----------------------------------------------------------------------------


@dataclasses.dataclass
class FileChange:
    path: str
    old_blob: str | None  # None for an added file
    new_blob: str | None  # None for a deleted file


@dataclasses.dataclass
class MemberContext:
    position: int  # 1-based chain position
    pr: int | None
    commit: str
    files: list[str] = dataclasses.field(default_factory=list)
    added: dict[str, str] = dataclasses.field(default_factory=dict)  # full name -> file
    removed: dict[str, str] = dataclasses.field(default_factory=dict)  # full name -> file
    written: dict[str, str] = dataclasses.field(default_factory=dict)  # full name -> name as written
    renamed: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    declaration_names: dict = dataclasses.field(default_factory=dict)
    added_approximate: bool = True

    def files_record(self) -> dict:
        return {"position": self.position, "pr": self.pr, "commit": self.commit, "files": self.files}

    def decls_record(self) -> dict:
        record = {
            "position": self.position,
            "pr": self.pr,
            "commit": self.commit,
            "approximate": True,
            "added": sorted(self.added),
            "removed": sorted(self.removed),
            "renamed": [list(pair) for pair in self.renamed],
        }
        if self.declaration_names:
            record.update(
                declaration_names=self.declaration_names,
                compiled_added=self.declaration_names["added"],
                added_approximate=self.added_approximate,
                removed_approximate=True,
                renamed_approximate=True,
            )
        return record


def changed_files(clone: Path, commit: ChainCommit) -> list[FileChange]:
    """The files a chain commit changes against its parent, in diff order, with their blob ids."""
    if commit.empty:
        return []
    out = git(clone, "diff-tree", "-r", "-z", "--no-commit-id", "--no-abbrev", commit.parent, commit.sha)
    fields = out.split("\0")
    changes: list[FileChange] = []
    index = 0
    while index + 1 < len(fields) and fields[index]:
        meta, path = fields[index], fields[index + 1]
        index += 2
        parts = meta.split()
        if len(parts) < 5:
            continue
        old_blob, new_blob, status = parts[2], parts[3], parts[4]
        if status.startswith(("R", "C")):  # not requested, but harmless if a config enables it
            index += 1
        changes.append(
            FileChange(path, None if set(old_blob) == {"0"} else old_blob, None if set(new_blob) == {"0"} else new_blob)
        )
    return changes


def _blob_declarations(clone: Path, blobs: list[str]) -> dict[str, dict[str, str]]:
    """``declarations`` of each blob, read through one ``git cat-file --batch``."""
    if not blobs:
        return {}
    result = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "--batch"],
        input=("\n".join(blobs) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git cat-file --batch failed ({result.returncode}): {result.stderr.decode().strip()}")
    found: dict[str, dict[str, str]] = {}
    data = result.stdout
    offset = 0
    while offset < len(data):
        newline = data.index(b"\n", offset)
        header = data[offset:newline].decode().split()
        offset = newline + 1
        if len(header) < 3:  # `<sha> missing`
            found[header[0]] = {}
            continue
        size = int(header[2])
        found[header[0]] = declarations(data[offset : offset + size].decode("utf-8", errors="replace"))
        offset += size + 1
    return found


def _shards(items: list[str], count: int) -> list[list[str]]:
    count = max(1, min(count, len(items)))
    size = -(-len(items) // count)
    return [items[i : i + size] for i in range(0, len(items), size)]


def member_contexts(
    clone: Path,
    chain: list[ChainCommit],
    pool: concurrent.futures.Executor,
    jobs: int,
    *,
    declaration_names: dict[int, dict] | None = None,
    edited_prs: set[int] | None = None,
) -> list[MemberContext]:
    members = [MemberContext(index + 1, commit.pr, commit.sha) for index, commit in enumerate(chain)]
    changes = list(pool.map(lambda commit: changed_files(clone, commit), chain))
    blobs: set[str] = set()
    for member, member_changes in zip(members, changes, strict=True):
        member.files = [change.path for change in member_changes]
        for change in member_changes:
            if change.path.endswith(".lean"):
                blobs.update(blob for blob in (change.old_blob, change.new_blob) if blob)
    names: dict[str, dict[str, str]] = {}
    for shard_names in pool.map(lambda shard: _blob_declarations(clone, shard), _shards(sorted(blobs), jobs)):
        names.update(shard_names)
    for member, member_changes in zip(members, changes, strict=True):
        added: dict[str, str] = {}
        removed: dict[str, str] = {}
        pairs: list[tuple[str, str]] = []
        for change in member_changes:
            if not change.path.endswith(".lean"):
                continue
            old = names.get(change.old_blob or "", {})
            new = names.get(change.new_blob or "", {})
            file_added = {name for name in new if name not in old}
            file_removed = {name for name in old if name not in new}
            member.written.update({name: new[name] for name in file_added})
            member.written.update({name: old[name] for name in file_removed})
            added.update(dict.fromkeys(file_added, change.path))
            removed.update(dict.fromkeys(file_removed, change.path))
            pairs += rename_pairs(file_removed, file_added)
        moved = set(added) & set(removed)  # declared in one file and gone from another: not new, not gone
        member.added = {name: path for name, path in added.items() if name not in moved}
        member.removed = {name: path for name, path in removed.items() if name not in moved}
        member.renamed = [pair for pair in pairs if pair[0] not in moved and pair[1] not in moved]
    for member, commit in zip(members, chain, strict=True):
        record = compiled_names.accepted((declaration_names or {}).get(member.pr), commit.accepted_head or "")
        if record is None:
            continue
        member.declaration_names = record
        # An accepted snapshot cannot replace names changed by a prior agent round. Keep it
        # beside the current scan as attribution evidence instead. Empty members add nothing.
        if member.pr in (edited_prs or set()) or commit.empty:
            continue
        member.added = {
            name: module.replace(".", "/") + ".lean" for module, names in record["added"].items() for name in names
        }
        member.added_approximate = False
        for name in member.added:
            member.written.setdefault(name, name.rsplit(".", 1)[-1])
    return members


# ---- collisions and ripples ---------------------------------------------------------------------


def _who(member: MemberContext, path: str | None = None) -> dict:
    record = {"position": member.position, "pr": member.pr}
    if path is not None:
        record["file"] = path
    return record


def collisions(members: list[MemberContext]) -> list[dict]:
    """Names added by two or more members: ``{"kind": "collision", "name", "added_by": [{position, pr,
    file}]}`` in chain order of the first member."""
    by_name: dict[str, list[dict]] = {}
    for member in members:
        for name, path in member.added.items():
            by_name.setdefault(name, []).append(_who(member, path))
    return [
        {"kind": "collision", "name": name, "added_by": who}
        for name, who in by_name.items()
        if len({w["position"] for w in who}) > 1
    ]


def _grep(clone: Path, tree: str, names: list[str]) -> list[tuple[str, int, str]]:
    """``(path, line, text)`` for every whole-word occurrence of any of the names in the tree's ``.lean``
    files."""
    argv = ["grep", "-n", "-w", "-F", "-I"]
    for name in names:
        argv += ["-e", name]
    out = git(clone, *argv, tree, "--", "*.lean", check=False)
    hits: list[tuple[str, int, str]] = []
    for line in out.splitlines():
        rest = line[len(tree) + 1 :] if line.startswith(tree + ":") else line
        path, _, remainder = rest.partition(":")
        number, _, text = remainder.partition(":")
        if number.isdigit():
            hits.append((path, int(number), text))
    return hits


def _references(
    clone: Path, tree: str, patterns: dict[str, str], pool: concurrent.futures.Executor
) -> dict[str, dict[str, list[int]]]:
    """For each search pattern, the files in the tree referencing it (whole word) with their line
    numbers. ``patterns`` maps the text searched for to itself; the caller maps texts back to names."""
    if not patterns:
        return {}
    texts = sorted(patterns)
    chunks = [texts[i : i + GREP_CHUNK] for i in range(0, len(texts), GREP_CHUNK)]
    found: dict[str, dict[str, list[int]]] = {text: {} for text in texts}
    for chunk, hits in zip(chunks, pool.map(lambda chunk: _grep(clone, tree, chunk), chunks), strict=True):
        regex = re.compile(r"(?<![\w'])(?:" + "|".join(re.escape(text) for text in chunk) + r")(?![\w'])")
        for path, number, text in hits:
            for match in set(regex.findall(text.partition("--")[0])):  # not a reference in a line comment
                found[match].setdefault(path, []).append(number)
    return found


def ripples(
    clone: Path, members: list[MemberContext], tip: str, target: str, pool: concurrent.futures.Executor
) -> list[dict]:
    """Names removed or renamed by a member and still referenced elsewhere: in another member's changed
    files at the chain tip, or in the target's version of a file no member changed. Each:
    ``{"kind": "ripple", "name", "removed_by": [{position, pr, file, renamed_to?}], "references":
    [{"file", "position", "pr"} | {"file", "main": true}, with "lines"]}``."""
    removers: dict[str, list[MemberContext]] = {}
    for member in members:
        for name in member.removed:
            removers.setdefault(name, []).append(member)
    if not removers:
        return []
    # Search for the full name and, when long enough to be distinctive, the name as written in the
    # declaration (how code in the same namespace refers to it).
    texts: dict[str, set[str]] = {}  # searched text -> names it stands for
    for name, who in removers.items():
        texts.setdefault(name, set()).add(name)
        written = who[0].written.get(name, name)
        if written != name and len(written) >= SHORT_NAME:
            texts.setdefault(written, set()).add(name)
    owners: dict[str, list[MemberContext]] = {}
    for member in members:
        for path in member.files:
            owners.setdefault(path, []).append(member)
    at_tip = _references(clone, tip, dict.fromkeys(texts), pool)
    on_target = _references(clone, target, dict.fromkeys(texts), pool) if target else {}
    found: list[dict] = []
    for name in sorted(removers, key=lambda n: (removers[n][0].position, n)):
        removing = removers[name]
        remover_positions = {m.position for m in removing}
        adders = {m.position for m in members if name in m.added}
        references: dict[tuple[str, int | None], set[int]] = {}
        for text in (t for t, names in texts.items() if name in names):
            for path, lines in at_tip.get(text, {}).items():
                for owner in owners.get(path, []):
                    if owner.position not in remover_positions and owner.position not in adders:
                        references.setdefault((path, owner.position), set()).update(lines)
            for path, lines in on_target.get(text, {}).items():
                if path not in owners:
                    references.setdefault((path, None), set()).update(lines)
        if not references:
            continue
        by_position = {m.position: m for m in members}
        removed_by = []
        for member in removing:
            entry = _who(member, member.removed[name])
            renamed_to = [new for old, new in member.renamed if old == name]
            if renamed_to:
                entry["renamed_to"] = renamed_to[0]
            removed_by.append(entry)
        found.append(
            {
                "kind": "ripple",
                "name": name,
                "removed_by": removed_by,
                "references": [
                    (
                        {"file": path, "position": position, "pr": by_position[position].pr}
                        if position is not None
                        else {"file": path, "main": True}
                    )
                    | {"lines": sorted(lines)}
                    for (path, position), lines in sorted(
                        references.items(), key=lambda kv: (kv[0][1] is None, kv[0][1] or 0, kv[0][0])
                    )
                ],
            }
        )
    return found


# ---- the result -------------------------------------------------------------------------------------


def _member_text(who: dict, extra: str | None = None) -> str:
    """``position N (PR #M, `file`)``, with ``extra`` appended inside the parentheses."""
    details = [f"PR #{who['pr']}"] if who.get("pr") is not None else []
    if who.get("file"):
        details.append(f"`{who['file']}`")
    if extra:
        details.append(extra)
    return f"position {who['position']}" + (f" ({', '.join(details)})" if details else "")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def collision_line(record: dict) -> str:
    return f"- `{record['name']}` is added by " + _join([_member_text(w) for w in record["added_by"]])


def ripple_line(record: dict) -> str:
    removers = [
        _member_text(who, f"renamed to `{who['renamed_to']}`" if who.get("renamed_to") else None)
        for who in record["removed_by"]
    ]
    references = []
    for reference in record["references"][:REFERENCES_SHOWN]:
        if reference.get("main"):
            references.append(f"`main`'s `{reference['file']}` (changed by no member)")
        else:
            references.append(f"`{reference['file']}` (position {reference['position']}, PR #{reference['pr']})")
    more = len(record["references"]) - REFERENCES_SHOWN
    if more > 0:
        references.append(f"{more} more file{'s' if more > 1 else ''}")
    return f"- `{record['name']}` is removed by " + _join(removers) + " and still referenced in " + _join(references)


@dataclasses.dataclass
class RepairContext:
    members: list[MemberContext]
    collisions: list[dict]
    ripples: list[dict]
    seconds: float
    error: str | None = None

    def records(self) -> list[dict]:
        return [*self.collisions, *self.ripples]

    def summary(self) -> dict:
        """What the outcome record keeps: counts and timing, and the error when the context is missing."""
        summary = {
            "members": len(self.members),
            "files": sum(len(m.files) for m in self.members),
            "added": sum(len(m.added) for m in self.members),
            "removed": sum(len(m.removed) for m in self.members),
            "renamed": sum(len(m.renamed) for m in self.members),
            "collisions": len(self.collisions),
            "ripples": len(self.ripples),
            "seconds": round(self.seconds, 3),
        }
        if self.error:
            summary["error"] = self.error
        return summary

    def prompt_lines(self) -> list[str]:
        """The "Likely collisions and renames" section; empty when the computation failed."""
        if self.error:
            return []
        lines = [
            "",
            "## Likely collisions and renames",
            "",
            f"Derived from the text of each member's diff, so approximate: `{LISTINGS_DIR}/{FILES_FILE}` lists the files "
            f"each member changes (`position`, `pr`, `commit`, `files`), `{LISTINGS_DIR}/{DECLS_FILE}` the declaration "
            f"names it adds, removes and renames, and `{LISTINGS_DIR}/{COLLISIONS_FILE}` every collision and ripple "
            "below with line numbers; `grep` them for a name, a file or a PR number (all ignored by Git).",
            "",
        ]
        exact = sum(not m.added_approximate for m in self.members)
        if any(m.declaration_names for m in self.members):
            lines[3] = (
                f"`{LISTINGS_DIR}/{DECLS_FILE}` includes compiled `DECLARATION-NAMES` snapshots of the accepted PRs "
                f"with their head, tested base and build identity ({exact} clean members use compiled added names). "
                "Previously edited members retain the current text-derived additions alongside that accepted snapshot. "
                "Removed names, rename pairs and uses are approximate; missing, incomplete or stale records use the text scan. "
                f"`{LISTINGS_DIR}/{FILES_FILE}` lists file ownership and `{LISTINGS_DIR}/{COLLISIONS_FILE}` lists "
                "predicted collisions and ripples. The worker's audits remain the checks."
            )
        if not self.collisions and not self.ripples:
            lines.append(
                "No declaration name is added by two members and no removed name is still referenced, as far as the "
                "text shows."
            )
            return lines
        parts = []
        if self.collisions:
            n = len(self.collisions)
            parts.append(f"{n} name{'s are' if n > 1 else ' is'} added by two or more members")
        if self.ripples:
            n = len(self.ripples)
            parts.append(f"{n} removed or renamed name{'s are' if n > 1 else ' is'} still referenced elsewhere")
        lines.append(" and ".join(parts) + ":")
        lines.append("")
        bullets = [collision_line(r) for r in self.collisions] + [ripple_line(r) for r in self.ripples]
        lines += bullets[:SECTION_LINES]
        if len(bullets) > SECTION_LINES:
            lines.append(f"- and {len(bullets) - SECTION_LINES} more in `{LISTINGS_DIR}/{COLLISIONS_FILE}`")
        return lines


# ---- across two batches ----------------------------------------------------------------------------

CROSS_BATCH_LINES = 20  # bullets in the prompt's predicted-ripples section


def cross_batch(
    clone: Path,
    head: list[ChainCommit],
    following: list[ChainCommit],
    *,
    jobs: int | None = None,
    head_declaration_names: dict[int, dict] | None = None,
    following_declaration_names: dict[int, dict] | None = None,
    edited_prs: set[int] | None = None,
) -> dict:
    """The predicted interaction between the head's chain and the next batch's, for the next batch's
    pre-rebase: ``removed_used``, the names a head member removed or renamed that a next-batch member's
    changed files still reference at that batch's tip (``{"name", "removed_by": [{position, pr, file,
    renamed_to?}], "references": [{file, position, pr, lines}]}``, positions in the head and in the next
    batch respectively), and ``added_by_both``, the names a member of each batch adds (``{"name", "head":
    [...], "next": [...]}``). Text-derived like ``compute``, so ``approximate``; never raises (an error is
    recorded as ``error`` with both lists empty)."""
    started = time.monotonic()
    jobs = jobs or min(32, (os.cpu_count() or 4) * 2)
    record: dict = {"approximate": True, "removed_used": [], "added_by_both": []}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            ahead = member_contexts(
                clone, head, pool, jobs, declaration_names=head_declaration_names, edited_prs=edited_prs
            )
            behind = member_contexts(
                clone, following, pool, jobs, declaration_names=following_declaration_names, edited_prs=edited_prs
            )
            removers: dict[str, list[MemberContext]] = {}
            for member in ahead:
                for name in member.removed:
                    removers.setdefault(name, []).append(member)
            re_added = {name for member in behind for name in member.added}
            texts: dict[str, set[str]] = {}
            for name, who in removers.items():
                if name in re_added:
                    continue
                texts.setdefault(name, set()).add(name)
                written = who[0].written.get(name, name)
                if written != name and len(written) >= SHORT_NAME:
                    texts.setdefault(written, set()).add(name)
            owners: dict[str, list[MemberContext]] = {}
            for member in behind:
                for path in member.files:
                    owners.setdefault(path, []).append(member)
            found = _references(clone, following[-1].sha, dict.fromkeys(texts), pool) if following and texts else {}
        for name in sorted({n for names in texts.values() for n in names}, key=lambda n: (removers[n][0].position, n)):
            references: dict[tuple[str, int], set[int]] = {}
            for text in (t for t, names in texts.items() if name in names):
                for path, lines in found.get(text, {}).items():
                    for owner in owners.get(path, []):
                        references.setdefault((path, owner.position), set()).update(lines)
            if not references:
                continue
            by_position = {m.position: m for m in behind}
            removed_by = []
            for member in removers[name]:
                entry = _who(member, member.removed[name])
                renamed_to = [new for old, new in member.renamed if old == name]
                if renamed_to:
                    entry["renamed_to"] = renamed_to[0]
                removed_by.append(entry)
            record["removed_used"].append(
                {
                    "name": name,
                    "removed_by": removed_by,
                    "references": [
                        {"file": path, "position": position, "pr": by_position[position].pr, "lines": sorted(lines)}
                        for (path, position), lines in sorted(references.items(), key=lambda kv: (kv[0][1], kv[0][0]))
                    ],
                }
            )
        added_ahead: dict[str, list[dict]] = {}
        for member in ahead:
            for name, path in member.added.items():
                added_ahead.setdefault(name, []).append(_who(member, path))
        for member in behind:
            for name, path in sorted(member.added.items()):
                if name in added_ahead:
                    entry = next((e for e in record["added_by_both"] if e["name"] == name), None)
                    if entry is None:
                        entry = {"name": name, "head": added_ahead[name], "next": []}
                        record["added_by_both"].append(entry)
                    entry["next"].append(_who(member, path))
    except Exception as error:  # the pre-rebase goes ahead without the prediction
        record.update(removed_used=[], added_by_both=[], error=f"{type(error).__name__}: {error}")
    record["seconds"] = round(time.monotonic() - started, 3)
    return record


def cross_batch_lines(record: dict | None) -> list[str]:
    """The prompt's "Predicted ripples from the head" section for a pre-rebase; empty when there is
    nothing to say."""
    if not record or not (record.get("removed_used") or record.get("added_by_both")):
        return []
    bullets = []
    for entry in record.get("removed_used") or []:
        removers = [
            _member_text(who, f"renamed to `{who['renamed_to']}`" if who.get("renamed_to") else None)
            for who in entry["removed_by"]
        ]
        users = [
            f"`{r['file']}` (position {r['position']}, PR #{r['pr']})" for r in entry["references"][:REFERENCES_SHOWN]
        ]
        more = len(entry["references"]) - REFERENCES_SHOWN
        if more > 0:
            users.append(f"{more} more file{'s' if more > 1 else ''}")
        bullets.append(
            f"- `{entry['name']}` is removed by the head's "
            + _join(removers)
            + " and used in this batch's "
            + _join(users)
        )
    for entry in record.get("added_by_both") or []:
        bullets.append(
            f"- `{entry['name']}` is added by the head's "
            + _join([_member_text(w) for w in entry["head"]])
            + " and by this batch's "
            + _join([_member_text(w) for w in entry["next"]])
        )
    lines = [
        "",
        "## Predicted ripples from the head",
        "",
        "The head batch this chain is being rebased onto removes, renames or adds these declaration names; the "
        "names come from the text of each member's diff, so they are approximate. Each is likely to break this batch "
        "once it sits on the head, so fix the members of this batch that use them while you rebase:",
        "",
    ]
    lines += bullets[:CROSS_BATCH_LINES]
    if len(bullets) > CROSS_BATCH_LINES:
        lines.append(f"- and {len(bullets) - CROSS_BATCH_LINES} more in the task's `cross_batch` diagnostics")
    return lines


def write_files(context: RepairContext, worktree: Path) -> None:
    directory = worktree / LISTINGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / FILES_FILE).write_text("".join(json.dumps(m.files_record()) + "\n" for m in context.members))
    (directory / DECLS_FILE).write_text("".join(json.dumps(m.decls_record()) + "\n" for m in context.members))
    (directory / COLLISIONS_FILE).write_text("".join(json.dumps(r) + "\n" for r in context.records()))


def compute(
    clone: Path,
    chain: list[ChainCommit],
    target: str,
    worktree: Path,
    *,
    jobs: int | None = None,
    declaration_names: dict[int, dict] | None = None,
    edited_prs: set[int] | None = None,
) -> RepairContext:
    """The entry point: the members' files and declarations, their collisions and rename ripples
    against the chain tip and ``target`` (the commit the result must sit on), written to
    ``.tauceti-repair/`` in ``worktree``. Never raises; a failure is recorded as ``error``."""
    started = time.monotonic()
    jobs = jobs or min(32, (os.cpu_count() or 4) * 2)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            members = member_contexts(
                clone, chain, pool, jobs, declaration_names=declaration_names, edited_prs=edited_prs
            )
            found_collisions = collisions(members)
            found_ripples = ripples(clone, members, chain[-1].sha, target, pool) if chain else []
        context = RepairContext(members, found_collisions, found_ripples, time.monotonic() - started)
        write_files(context, worktree)
    except Exception as error:  # the repair runs without this context as it did before
        return RepairContext([], [], [], time.monotonic() - started, error=f"{type(error).__name__}: {error}")
    context.seconds = time.monotonic() - started
    return context

"""The `reconcile` phase: write back what an operator's target list's own pull requests settled.

Runs only for a worker whose `only` names `reconcile` and that has a target list, at most once per
TAUCETI_RECONCILE_GAP. What it changes in the file, and why, is documented in docs/target-lists.md
("Reconciliation"). It starts no model and makes no judgement of content: every edit follows from
the state of a pull request that carries the item's marker or that the item's `in flight:` clause
names.

With TAUCETI_ROADMAP_TARGETS_INFER on it also ticks an item from the base branch's source, on a host
agent's verdict reduced to declarations the worker finds there, and takes such a tick back when a
dependent's round reports the item wanting or the roadmap's coverage header disagrees ("Inferred
completion" in the same document). The pieces:

    due(counters)                  whether a look is due, and the reason either way
    settle(targets, seen)          pure: the edits that pull request states imply
    audit(targets, roadmap)        pure but for reading STATUS.md: inferred ticks a coverage header refutes
    candidates(live)               the open items an inference look may ask about, most blocking first
    confirmed(checkout, verdict)   a model verdict reduced to declarations the checkout has
    reopen(slugs, allowed, by)     take back inferred ticks a dependent's round found wanting
    publish(path, base, ours, ..)  merge an edit into the file; commit and push a tracked one
    run(w, c, profile)             one reconciliation, under the list's lock

Only `ask` starts a model. Its answer reaches the list as file paths and declaration names that
`confirmed` found in the checkout, and in no other form.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tauceti_tools.forge import GITHUB, RepositoryRef

from .config import Die, SourceUnavailable, log, one_line, roadmap_targets
from .constants import ROADMAP, ROADMAP_DIR, TAUCETI
from .github import BORS_MERGED_PREFIX, GitHubError
from .paths import HERE
from .targets import (
    MERGED_SEARCH,
    GitHubMarkerReads,
    Key,
    TargetItem,
    Targets,
    _is_partial,
    content_base,
    eligible_items,
    github_history_enabled,
    inference_enabled,
    inferred,
    inflight_ref,
    landed,
    layer_of,
    marker_keys,
    overlay_live,
    parse_targets,
    partial_prs,
    ref_key,
    render_item,
    reopened,
    rewrite_item,
    verified_landings,
)

GAP = int(os.environ.get("TAUCETI_RECONCILE_GAP", "21600"))
INFER_MAX = int(os.environ.get("TAUCETI_ROADMAP_TARGETS_INFER_MAX", "12"))
RECHECK = 7 * 86400  # a not-landed verdict on unchanged evidence is asked again after this long
MERGED_LIMIT = 1000
MAX_HITS = 5
MAX_EVIDENCE = 5
REOPEN_WAIT = 120
ATTEMPT_KEY = "reconcile-attempt-ts"

_IDENTIFIER = re.compile(r"`([A-Za-z][A-Za-z0-9_'.]{3,})(?=[`\s(])")
# POSIX ERE, since `git grep -E` has no \s, \b or \w on every platform.
_DECLARATION = (
    r"^[[:space:]]*(@\[[^]]*\][[:space:]]*)?((protected|private|noncomputable|scoped|unsafe|partial)[[:space:]]+)*"
    r"(theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque)[[:space:]]+"
    r"([^[:space:]]+\.)?(NAME)([^A-Za-z0-9_'.]|$)"
)
_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.lean")
_NAME = re.compile(r"[^\W\d][\w.']*")
_COVERAGE_RE = re.compile(r"<!--\s*tauceti-coverage:v1\s*(\{.*?\})\s*-->", re.S)
_STATUS_RE = re.compile(r"<!--\s*tauceti-status:v1\s*(\{.*?\})\s*-->", re.S)
_LAYER_STATES = ("done", "partial", "untouched", "unassessed")


@dataclass(frozen=True)
class Change:
    item: TargetItem
    status: str
    note: str  # what happened, for the log and the commit message
    drop: tuple[str, ...] = ()  # clause keys to remove from the item's line
    add: tuple[str, ...] = ()  # clauses to append to it


@dataclass(frozen=True)
class Source:
    """One forge's pull requests as reconciliation reads them. The list names a GitHub pull request
    `#N` and any other by its URL, as the roadmap round does."""

    reads: object  # the GitHub client or the Forgejo worker source
    repository: RepositoryRef
    native: bool = False
    shared: bool = False  # GitHub read from inside another source's round

    def ref(self, number: int) -> str:
        return self.repository.pull_url(number) if self.native else f"#{number}"

    def number(self, ref: str) -> int | None:
        found = re.fullmatch(r"https?://\S*?(\d+)/?" if self.native else r"#(\d+)", ref, re.I)
        if found is None or (self.native and ref_key(self.repository.pull_url(int(found.group(1)))) != ref_key(ref)):
            return None
        return int(found.group(1))

    def _read(self, method: str, *args, **kwargs):
        if not self.shared:
            return getattr(self.reads, method)(*args, **kwargs)
        from .worker_sources import shared_github_environment

        with shared_github_environment():
            return getattr(self.reads, method)(*args, **kwargs)

    def open_prs(self) -> list[dict]:
        rows = self._read("open_prs")
        if rows is None:
            raise SourceUnavailable(f"open pull requests of {self.repository.name} are unavailable")
        return rows

    def merged_prs(self) -> list[dict]:
        return self._read("merged_prs", f"{MERGED_SEARCH} sort:updated-desc", limit=MERGED_LIMIT)

    def target_queue_landed(self, number: int, branch: str) -> bool | None:
        """The source's proof that the merge queue landed pull request `number` on `branch`; None
        when the source has no such proof or could not read it."""
        if getattr(self.reads, "target_queue_landed", None) is None:
            return None
        found = self._read("target_queue_landed", number, branch)
        return found if isinstance(found, bool) else None

    def view(self, number: int) -> dict:
        """The pull request's state, title, body and base. One closed under the queue's title is
        MERGED only with the source's landing proof for its base, CLOSED when the proof is refused,
        and unknown (`{}`) when there is none."""
        row = self._read("pr_view", number, ["state", "title", "body", "baseRefName"]) or {}
        if row.get("state") == "CLOSED" and str(row.get("title") or "").startswith(BORS_MERGED_PREFIX):
            # Proof against the pull request's own base: one landed elsewhere is merged, not closed,
            # and `_merged_into_base` then leaves its item alone.
            base = row.get("baseRefName")
            proof = self.target_queue_landed(number, base) if base else None
            return {} if proof is None else {**row, "state": "MERGED"} if proof else row
        return row


class _GitHubHistory(GitHubMarkerReads):
    """`GitHubMarkerReads` with the view of one pull request that reconciliation needs, from the
    same listing. A pull request outside it reads as unknown, and its item is left as it is."""

    def pr_view(self, number: int, fields: list[str]) -> dict | None:
        row = next((d for d in self._listing() if d.get("number") == number), None)
        return {f: row[f] for f in fields if f in row} if row else None


def sources(w) -> list[Source]:
    """The worker's own source and the others a multi-source round hands it (`target_sources`). A
    round with no GitHub source adds the GitHub repository's marked pull requests (`_GitHubHistory`)
    unless TAUCETI_ROADMAP_TARGETS_GITHUB is 0, so a list carried over from GitHub keeps its `#N`."""
    from .worker_sources import current

    found = []
    for gh in (w.gh, *getattr(w, "target_sources", ())):
        if getattr(gh, "direct_source", False):
            found.append(Source(gh, gh.repository, native=True))
        else:
            found.append(Source(gh, RepositoryRef(GITHUB, getattr(gh, "repo", TAUCETI)), shared=current() is not None))
    if all(source.native for source in found) and github_history_enabled():
        found.append(Source(_GitHubHistory(), RepositoryRef(GITHUB, TAUCETI)))
    return found


@dataclass(frozen=True)
class Seen:
    """What one reconciliation read from the forges. A pull request is named as the list names it
    (`Source.ref`)."""

    merged: list[dict]  # merged pull requests that carry a target marker; a Forgejo one has a `url`
    pulls: dict[str, dict]  # the pull request each `in flight:` clause names
    open_keys: set[Key] = field(default_factory=set)  # items an open pull request's marker names


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def due(counters) -> tuple[bool, str]:
    """Whether the target list is due a look, and why or why not. Two local reads: this worker's
    last attempt and the lock file's modification time, which every completed look moves."""
    path = roadmap_targets()
    if path is None:
        return False, "no target list is set (--roadmap-targets)"
    last = counters.read(ATTEMPT_KEY)
    with contextlib.suppress(OSError):
        last = max(last, int(_lock_path(path.resolve()).stat().st_mtime))
    waited = time.time() - last
    if waited < GAP:
        return False, f"the target list was looked at {waited / 3600:.1f}h ago; it is looked at every {GAP / 3600:g}h"
    return True, "the target list is due a look"


def _refs(refs: Iterable[str]) -> str:
    """GitHub `#N` references in number order, then URLs; one spelling of each, the first given."""
    unique = list({ref_key(ref): ref for ref in reversed(list(refs))}.values())
    return ", ".join(
        sorted(unique, key=lambda ref: (0, int(ref[1:]), "") if ref.startswith("#") else (1, 0, ref.lower()))
    )


def _partial_refs(item: TargetItem) -> set[str]:
    """The references an item's `partial:` clause names, read as `targets.landed` reads them."""
    clauses = " ".join(c.partition(":")[2] for c in item.meta if _is_partial(c))
    return {f"#{n}" for n in partial_prs(item)} | set(re.findall(r"https?://[^\s,;]+", clauses, re.I))


def _closed_refs(item: TargetItem) -> list[str]:
    """The references an item's `closed:` clause names."""
    clauses = " ".join(c.partition(":")[2] for c in item.meta if c.partition(":")[0].strip().lower() == "closed")
    return re.findall(r"https?://[^\s,;]+|#\d+", clauses, re.I)


def _merged_into_base(pull: dict) -> bool:
    """A merge settles an item only into the content base, as `targets.landed` counts one."""
    return pull.get("state") == "MERGED" and pull.get("baseRefName") == content_base()


def settle(targets: Targets, seen: Seen) -> list[Change]:
    """The edits that `seen` implies for the items not yet done.

    An item is done when a merged pull request completes it by marker (`targets.landed`), or when
    the pull request its `in flight:` clause names merged into the content base. A named pull
    request that merged as partial opens the item again and joins its `partial:` clause; one that
    closed unmerged opens it again and joins its `closed:` clause. One merged into another branch,
    still open, or not read leaves the item as it is."""
    pending = [it for it in targets.items() if it.status != "done"]
    excused = {(it.area, it.slug): {ref_key(ref) for ref in _partial_refs(it)} for it in pending}
    by_marker: dict[Key, str] = {}
    for row in seen.merged:
        for key, number in landed(pending, [row]).items():
            ref = row.get("url") or f"#{number}"
            if ref_key(ref) not in excused[key]:
                by_marker.setdefault(key, ref)
    changes: list[Change] = []
    for it in pending:
        key = (it.area, it.slug)
        named = inflight_ref(it) if it.status == "inflight" else None
        pull = seen.pulls.get(named) or {}
        partial = key in marker_keys(pull.get("body"))[1] or (named is not None and ref_key(named) in excused[key])
        merged = _merged_into_base(pull)
        if key in by_marker:
            ref = by_marker[key]
            changes.append(Change(it, "done", f"{ref} merged", ("in flight",), (f"landed: {ref}",)))
        elif merged and partial:
            clause = "partial: " + _refs([*_partial_refs(it), named])
            note = f"{named} merged without completing it; open again"
            changes.append(Change(it, "open", note, ("in flight", "partial"), (clause,)))
        elif merged:
            changes.append(Change(it, "done", f"{named} merged", ("in flight",), (f"landed: {named}",)))
        elif pull.get("state") == "CLOSED":
            clause = "closed: " + _refs([*_closed_refs(it), named])
            changes.append(
                Change(it, "open", f"{named} closed unmerged; open again", ("in flight", "closed"), (clause,))
            )
    return changes


def apply(text: str, changes: Iterable[Change]) -> str:
    for change in changes:
        text = rewrite_item(text, change.item, change.status, drop=change.drop, add=change.add)
    return text


def observe(targets: Targets, read: list[Source], *, open_listing: bool = False) -> Seen:
    """Read what `settle` needs from each source: its merged pull requests that carry a marker, and
    one view per `in flight:` clause that names one of its pull requests; with `open_listing`, also
    its open ones, which keep an item from inference. A pull request closed under the queue's title
    counts as merged only with its source's landing proof (`verified_landings`, `Source.view`). A
    clause naming a pull request of no source in `read` is not looked at. Raises GitHubError or
    SourceUnavailable when a listing fails."""
    merged: list[dict] = []
    open_keys: set[Key] = set()
    pending = [it for it in targets.items() if it.status != "done"]
    for source in read:
        for row in source.merged_prs():
            if row.get("state") != "CLOSED":
                merged.append(row)
            elif verified_landings(pending, [row], source):
                merged.append({**row, "state": "MERGED"})
        for d in source.open_prs() if open_listing else ():
            open_keys |= marker_keys(d.get("body"))[0]
    named = {inflight_ref(it) for it in targets.items() if it.status == "inflight"} - {None}
    pulls = {}
    for ref in sorted(named):
        source = next((s for s in read if s.number(ref) is not None), None)
        if source is not None:
            pulls[ref] = source.view(source.number(ref))
    return Seen(merged, pulls, open_keys)


def _evidence(found: Iterable[str]) -> str:
    return ", ".join(sorted(found))


def _date(ts: str) -> str | None:
    """The UTC date of an ISO timestamp, the calendar inference dates are written in; a timestamp
    without an offset is taken as UTC."""
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (when.astimezone(datetime.UTC) if when.tzinfo else when).date().isoformat()


def coverage(status: str, area: str, readme: str) -> tuple[dict[str, str], str] | None:
    """The layer states a STATUS.md's coverage header gives for roadmap `area` itself, and the date
    of the status header beside it. None unless there is exactly one such header, it and the status
    header parse, every layer is an object with a string id and a known state, and its `readme_sha`
    is the SHA-256 of `readme`, the area's README as it now is: an assessment of another version of
    the README says nothing about the current requirements under the same layer id."""
    headers = []
    for raw in _COVERAGE_RE.findall(status):
        with contextlib.suppress(ValueError):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("roadmap") == area:
                headers.append(data)
    stamps = []
    for raw in _STATUS_RE.findall(status):
        with contextlib.suppress(ValueError):
            data = json.loads(raw)
            if isinstance(data, dict):
                stamps.append(_date(data.get("ts")))
    if len(headers) != 1 or len(stamps) != 1 or stamps[0] is None or not isinstance(headers[0].get("layers"), list):
        return None
    if headers[0].get("readme_sha") != hashlib.sha256(readme.encode("utf-8")).hexdigest():
        return None
    states: dict[str, str] = {}
    for layer in headers[0]["layers"]:
        if not (isinstance(layer, dict) and isinstance(layer.get("id"), str) and layer.get("state") in _LAYER_STATES):
            return None
        states[layer["id"]] = layer["state"]
    return states, stamps[0]


def audit(targets: Targets, roadmap: Path | None) -> list[Change]:
    """The inferred ticks a roadmap's coverage header refutes: when every item of an area that begins
    `Layer N,` is done, and the area's STATUS.md (under `roadmap`) says layer `Layer N` is not done,
    as of a status date later than the day an inferred tick in that layer was made and for the README
    the area has now (`coverage`), that tick is opened again. Ticks earned by a merged pull request, and inferred ones without a date, stay."""
    changes: list[Change] = []
    for area, items in targets.areas.items() if roadmap is not None else ():
        try:
            found = coverage(
                (roadmap / area / "STATUS.md").read_text(),
                area,
                (roadmap / area / "README.md").read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeDecodeError):
            continue
        if found is None:
            continue
        states, as_of = found
        layers: dict[str, list[TargetItem]] = {}
        for it in items:
            if (layer := layer_of(it)) is not None:
                layers.setdefault(layer, []).append(it)
        for layer, members in layers.items():
            state = states.get(layer)
            if state in (None, "done") or any(it.status != "done" for it in members):
                continue
            for it in members:
                tick = inferred(it)
                if tick is None or tick[1] is None or tick[1] >= as_of:
                    continue
                why = f"coverage {layer} {state} on {as_of}"
                clause = f"reopened: {_evidence(tick[0])} by {why}"
                changes.append(Change(it, "open", f"reopened, {why}", ("inferred",), (clause,)))
    return changes


def identifiers(text: str) -> list[str]:
    """The Lean names a description quotes in backticks: the leading token of each code span when it
    is dotted, has an underscore or has a capital. Plain words and notation name no declaration."""
    found = (name for name in _IDENTIFIER.findall(text or "") if not name.endswith("."))
    return list(dict.fromkeys(n for n in found if "." in n or "_" in n or any(c.isupper() for c in n)))


def declared(checkout: Path, pattern: str, *, pathspec: str = "*.lean", ignore_case: bool = False) -> list[str]:
    """The `path:line:text` lines of the checkout's Lean files that declare a name matching the ERE
    `pattern` as their last component."""
    try:
        found = subprocess.run(
            ["git", "-C", str(checkout), "grep", "-nE" + ("i" if ignore_case else ""), "-e"]
            + [_DECLARATION.replace("NAME", pattern), "--", pathspec],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip()[:240] for line in found.stdout.splitlines() if line.strip()]


def leads(checkout: Path, item: TargetItem) -> dict[str, list[str]]:
    """Where the checkout declares what `item` names, by name: each Lean name its description quotes
    that is declared, and the slug when a declaration carries its words in any case, with or without
    underscores between them. Leads for the agent only; an item with none is asked about too."""
    found = {}
    for name in identifiers(item.text):
        if hits := declared(checkout, re.escape(name.rsplit(".", 1)[-1])):
            found[name] = hits
    words = "_?".join(re.escape(word) for word in item.slug.split("-") if word)
    if words and (by_slug := declared(checkout, words, ignore_case=True)):
        found[f"slug {item.slug}"] = by_slug
    return found


def candidates(live: Targets) -> list[TargetItem]:
    """The open items an inference look may ask about: those whose needs have all landed, and those
    an item not yet done needs, ordered by how many items not yet done wait on them, directly or
    through others, then in file order. An item in flight or done is never one."""
    waiting: dict[str, set[str]] = {}
    for it in live.items():
        if it.status != "done":
            for slug in it.needs:
                waiting.setdefault(slug, set()).add(it.slug)

    def blocked(slug: str) -> int:
        seen, todo = set(), [slug]
        while todo:
            for dependent in waiting.get(todo.pop(), ()):
                if dependent not in seen:
                    seen.add(dependent)
                    todo.append(dependent)
        return len(seen - {slug})

    eligible = {it.slug for area in live.areas for it in eligible_items(live, area)}
    pool = [it for it in live.items() if it.status == "open" and (it.slug in eligible or it.slug in waiting)]
    return [it for _, it in sorted(enumerate(pool), key=lambda pair: (-blocked(pair[1].slug), pair[0]))]


def _fingerprint(checkout: Path, item: TargetItem, hits: dict[str, list[str]], around: tuple[str, ...]) -> str:
    """Identifies what a verdict on `item` was given on: its description and `around` (the goal, its
    line, its README), the names looked up, and the whole of every checkout file a hit is in."""
    digest = hashlib.sha256(json.dumps([item.text, list(around), sorted(hits)]).encode())
    for file in sorted({hit.split(":", 1)[0] for found in hits.values() for hit in found}):
        digest.update(b"\0" + file.encode() + b"\0")
        with contextlib.suppress(OSError):
            digest.update(hashlib.sha256((checkout / file).read_bytes()).digest())
    return digest.hexdigest()


_LEAN_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:protected|private|noncomputable|scoped|unsafe|partial|nonrec)\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque)\s+([^\s:({\[]+)"
)
_LEAN_SCOPE = re.compile(r"^\s*(?:(namespace)\s+(\S+)|(?:noncomputable\s+)?(section)\b|(end)\b)")


def _code(text: str) -> str:
    """`text` with Lean comments (line comments and nested block comments, doc comments included)
    and string literals blanked to spaces, keeping every newline, so only commands remain."""
    out, depth, i, quoted = [], 0, 0, False
    while i < len(text):
        pair = text[i : i + 2]
        if depth:
            depth += 1 if pair == "/-" else -1 if pair == "-/" else 0
            step = 2 if pair in ("/-", "-/") else 1
        elif quoted:
            quoted = not (text[i] == '"')
            step = 2 if text[i] == "\\" else 1
        elif pair == "/-":
            depth, step = 1, 2
        elif pair == "--":
            step = (text.find("\n", i) if "\n" in text[i:] else len(text)) - i
        elif text[i] == '"':
            quoted, step = True, 1
        else:
            out.append(text[i])
            i += 1
            continue
        out.append("".join(c if c == "\n" else " " for c in text[i : i + step]))
        i += step
    return "".join(out)


def declarations(text: str) -> set[str]:
    """The fully qualified names a Lean file declares, from its `namespace`/`section`/`end` nesting
    and `_root_.` prefixes. A reading of the source's lines, not an elaboration."""
    scopes: list[list[str]] = []
    found = set()
    for line in _code(text).splitlines():
        scope = _LEAN_SCOPE.match(line)
        if scope and scope.group(1):
            scopes.append(scope.group(2).split("."))
        elif scope and scope.group(3):
            scopes.append([])
        elif scope and scope.group(4):
            if scopes:
                scopes.pop()
        elif declared_here := _LEAN_DECLARATION.match(line):
            name = declared_here.group(1)
            if name.startswith("_root_."):
                found.add(name[len("_root_.") :])
            else:
                found.add(".".join([*(part for parts in scopes for part in parts), name]))
    return found


def canonical(checkout: Path, file: str, name: str) -> str | None:
    """`path#Full.Name` for a declaration the checkout's file declares, with the path normalized and
    the name resolved to the one fully qualified declaration it is a dot-suffix of; None when the
    path leaves the checkout or the name matches no declaration, or more than one."""
    path = posixpath.normpath(file)
    if not _FILE.fullmatch(path) or path.startswith("/") or ".." in path.split("/") or not _NAME.fullmatch(name):
        return None
    try:
        text = (checkout / path).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    matches = {full for full in declarations(text) if full == name or full.endswith("." + name)}
    return f"{path}#{matches.pop()}" if len(matches) == 1 else None


def confirmed(checkout: Path, verdict, refused: Iterable[frozenset[str]] = ()) -> frozenset[str] | None:
    """The canonical `path#Full.Name` evidence (`canonical`) of `verdict` when it is `{"landed":
    true, "evidence": [{"file": ..., "declaration": ...}, ...]}` with one to MAX_EVIDENCE entries,
    each resolving to a declaration of the checkout, and the evidence is not within what a
    `reopened:` clause (`refused`, compared canonically too) already found wanting; None otherwise."""
    if not (isinstance(verdict, dict) and verdict.get("landed") is True):
        return None
    entries = verdict.get("evidence")
    if not (isinstance(entries, list) and 1 <= len(entries) <= MAX_EVIDENCE):
        return None
    found = set()
    for entry in entries:
        file, name = (entry.get("file"), entry.get("declaration")) if isinstance(entry, dict) else (None, None)
        resolved = canonical(checkout, file, name) if isinstance(file, str) and isinstance(name, str) else None
        if resolved is None:
            return None
        found.add(resolved)
    found = frozenset(found)

    def known(reference: str) -> str:
        path, _, name = reference.partition("#")
        return canonical(checkout, path, name) or reference

    return None if any(found <= {known(r) for r in wanting} for wanting in refused) else found


def ask(w, profile, checkout: Path, targets: Targets, rows: list[dict]) -> dict:
    """Put the list's goal and the candidates to one host agent and return the `verdicts.json` it
    wrote, `{}` when it wrote none that parses."""
    from .agents import fill_prompt, run_agent_host
    from .claude_api import blocked_failure

    work = w.cfg.state / "reconcile" / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    listing, verdicts = work / "candidates.json", work / "verdicts.json"
    listing.write_text(
        json.dumps(
            {"checkout": str(checkout), "goal": targets.preamble, "candidates": rows}, indent=1, ensure_ascii=False
        )
    )
    prompt = fill_prompt(
        HERE / "prompts" / "infer.md", CANDIDATES=str(listing), CHECKOUT=str(checkout), VERDICTS=str(verdicts)
    )
    rc = run_agent_host(work, prompt, profile, w.cfg.logdir, quota_cfg=w.cfg)
    if rc != 0:
        if failure := blocked_failure():
            raise failure
        log(f"reconcile: the agent exited {rc}; no verdict is used")
        return {}
    try:
        answer = json.loads(verdicts.read_text())
    except (OSError, ValueError):
        log("reconcile: the agent wrote no verdicts.json that parses; no verdict is used")
        return {}
    return answer if isinstance(answer, dict) else {}


def _base_checkout(w) -> Path | None:
    from .agents import fetch_ref

    checkout = w.cfg.state / "reconcile" / "base"
    return checkout if fetch_ref(TAUCETI, checkout, ref=content_base()) else None


def _roadmap(w) -> Path | None:
    """The roadmap repository's directory of roadmaps, attached as the authoring round attaches it."""
    from .agents import fetch_ref

    refs = w.cfg.state / "refs" / "roadmap"
    if not fetch_ref(ROADMAP, refs):
        log(f"reconcile: {ROADMAP} could not be attached; no README is shown and no layer is audited")
        return None
    return refs / ROADMAP_DIR


def _infer(w, profile, live: Targets, roadmap: Path | None) -> list[Change]:
    """The candidates (`candidates`) a model verdict, checked against a fresh checkout of the content
    base, shows landed. At most INFER_MAX are put to the model; one it found not landed is not asked
    about again for RECHECK unless its line, the goal, its README or a file its leads are in changes."""
    pool = candidates(live)
    checkout = _base_checkout(w) if pool else None
    if checkout is None:
        if pool:
            log(f"reconcile: no checkout of {content_base()}; no item is inferred")
        return []
    memo_file = w.cfg.state / "reconcile" / "not-landed.json"
    try:
        memo = json.loads(memo_file.read_text())
    except (OSError, ValueError):
        memo = {}
    memo = {it.slug: memo[it.slug] for it in pool if isinstance(memo, dict) and isinstance(memo.get(it.slug), dict)}
    now = time.time()
    asked: list[tuple[TargetItem, str, dict]] = []
    for it in pool:
        readme = roadmap / it.area / "README.md" if roadmap is not None else None
        try:
            statement = readme.read_text() if readme is not None else ""
        except (OSError, UnicodeDecodeError):
            readme, statement = None, ""
        hits = leads(checkout, it)
        line = render_item(live, it)
        fingerprint = _fingerprint(checkout, it, hits, (live.preamble, line, statement))
        known = memo.get(it.slug) or {}
        if known.get("fingerprint") == fingerprint and now - float(known.get("at") or 0) < RECHECK:
            continue
        row = {
            "slug": it.slug,
            "area": it.area,
            "milestone": it.text,
            "line": line,
            "readme": str(readme) if readme is not None and statement else None,
            "layer": layer_of(it),
            "leads": {name: found[:MAX_HITS] for name, found in hits.items()},
        }
        asked.append((it, fingerprint, row))
        if len(asked) >= INFER_MAX:
            break
    if not asked:
        return []
    log(f"reconcile: asking whether {len(asked)} open item(s) already landed on {content_base()}")
    verdicts = ask(w, profile, checkout, live, [row for _, _, row in asked])
    on = datetime.datetime.now(datetime.UTC).date().isoformat()
    changes = []
    for it, fingerprint, _ in asked:
        verdict = verdicts.get(it.slug)
        found = confirmed(checkout, verdict, reopened(it))
        if found:
            log(f"reconcile: `{it.slug}` inferred landed: {one_line(str(verdict.get('why') or ''))[:200]}")
            clause = f"inferred: {_evidence(found)} on {on}"
            changes.append(Change(it, "done", f"inferred from {_evidence(found)}", ("in flight",), (clause,)))
        elif isinstance(verdict, dict) and verdict.get("landed") in (True, False):
            memo[it.slug] = {"fingerprint": fingerprint, "at": now}
            log(f"reconcile: `{it.slug}` not inferred: {one_line(str(verdict.get('missing') or ''))[:160]}")
        else:
            log(f"reconcile: `{it.slug}`: no usable verdict; it stays open")
    with contextlib.suppress(OSError):
        memo_file.parent.mkdir(parents=True, exist_ok=True)
        memo_file.write_text(json.dumps(memo, indent=1, sort_keys=True))
    return changes


def _git(path: Path, *args: str, timeout: int = 120, env: dict | None = None) -> subprocess.CompletedProcess:
    """Git in the list's clone, with the operator's identity, credentials and hooks even inside a
    source's activation."""
    from .worker_sources import operator_environment

    argv = ["git", "-C", str(path.parent), *args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**operator_environment(), "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 124, "", str(error))


def _spec(path: Path) -> str:
    """The list as a pathspec that matches its own name only, whatever glob characters it holds."""
    return f":(literal){path.name}"


def _owed(path: Path) -> Path | None:
    """Where the clone records an inference it owes a withdrawal (`_withdraw`), inside `.git`."""
    found = _git(path, "rev-parse", "--git-path", f"tauceti-reconcile/{path.name}.owed")
    return path.parent / found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def _withdraw(path: Path, base: str, ours: str) -> bool:
    """Commit, on whatever HEAD now is, the list as committed there with this look's edits undone
    where they still stand (`_taken_back`), through a scratch index so nothing else the clone has
    staged is touched; then undo them in the working file too. History is never rewritten, so a
    commit another actor made meanwhile, to this list or another, stays."""
    shown = _git(path, "show", f"HEAD:./{path.name}")
    fixed = _taken_back(base, ours, "", shown.stdout) if shown.returncode == 0 else None
    if fixed is None:
        return False
    if fixed != shown.stdout:
        listed = _git(path, "ls-files", "-s", "-z", "--full-name", "--", _spec(path)).stdout.split("\0")[0]
        meta, _, name = listed.partition("\t")
        if not name or len(meta.split()) != 3:
            return False
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "list").write_text(fixed)
            scratch = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            blob = _git(path, "hash-object", "-w", "--", str(Path(tmp) / "list")).stdout.strip()
            head = _git(path, "rev-parse", "HEAD").stdout.strip()
            steps = (("read-tree", head), ("update-index", "--cacheinfo", f"{meta.split()[0]},{blob},{name}"))
            if not blob or any(_git(path, *step, env=scratch).returncode for step in steps):
                return False
            tree = _git(path, "write-tree", env=scratch).stdout.strip()
            message = f"Withdraw unpublished inference in {path.name}"
            commit = _git(path, "commit-tree", tree, "-p", head, "-m", message).stdout.strip()
            if not (tree and commit) or _git(path, "update-ref", "HEAD", commit, head).returncode:
                return False
    undone = _taken_back(base, ours, "", path.read_text())
    if undone is None:
        return False
    _git(path, "reset", "-q", "HEAD", "--", _spec(path))
    return _write(path, undone)


def _owe(path: Path, base: str, ours: str) -> None:
    """Record that this look's inference must be withdrawn before the list is pushed again, then try."""
    marker = _owed(path)
    if marker is not None:
        with contextlib.suppress(OSError):
            marker.parent.mkdir(parents=True, exist_ok=True)
            _replace_json(marker, {"base": base, "ours": ours})
    _settle_owed(path)


def _settle_owed(path: Path) -> bool:
    """Make the withdrawal `_owe` recorded, if any; whether none is owed afterwards."""
    marker = _owed(path)
    if marker is None or not marker.exists():
        return True
    try:
        owed = json.loads(marker.read_text())
        done = _withdraw(path, owed["base"], owed["ours"])
    except (OSError, ValueError, KeyError, TypeError):
        done = False
    if done:
        marker.unlink(missing_ok=True)
        log(f"reconcile: the unpublished inference in {path.name} was withdrawn; the next look asks again")
    return done


def _tracked(path: Path) -> bool:
    return _git(path, "ls-files", "--error-unmatch", "--", _spec(path)).returncode == 0


def _upstream(path: Path) -> tuple[str, str] | None:
    """(remote, ref) that the branch checked out around the list tracks on a remote."""
    head = _git(path, "symbolic-ref", "-q", "HEAD").stdout.strip()
    if not head:
        return None
    found = _git(path, "for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref)", head).stdout.split()
    return (found[0], found[1]) if len(found) == 2 and found[0] != "." else None


def _sync(path: Path) -> None:
    """Fetch the branch the list's branch tracks and rebase local commits onto it. A clone with
    uncommitted changes to tracked files is left where it is; so is one whose rebase stops on a
    conflict, after aborting it."""
    upstream = _upstream(path)
    if upstream is None:
        return
    fetched = _git(path, "fetch", "-q", upstream[0], timeout=300)
    if fetched.returncode != 0:
        log(f"reconcile: fetch from {upstream[0]} failed ({one_line(fetched.stderr)}); working from the local list")
    elif _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        log(f"reconcile: {path.parent} has uncommitted changes; not rebasing it onto {upstream[0]}")
    elif _git(path, "rebase", "-q", "@{upstream}").returncode != 0:
        _git(path, "rebase", "--abort")
        log(f"reconcile: local commits in {path.parent} conflict with {upstream[0]}; rebase there by hand")


def _push(path: Path, still=None) -> bool:
    """Push the list's branch to the branch it tracks when it is ahead, once more after a sync if
    the remote has moved; whether nothing is left unpushed. A commit that cannot be pushed stays
    local and goes with a later call. Nothing is pushed while a withdrawal is owed (`_owed`) and
    cannot be made. With `still`, a list whose committed text, after any hook or sync, fails it is
    not pushed."""
    upstream = _upstream(path)
    if upstream is None:
        return True
    if not _settle_owed(path):
        log(f"reconcile: an inference in {path.name} is still owed a withdrawal; nothing pushed")
        return False
    for attempt in (1, 2):
        # A failed comparison (the tracked branch not on the remote yet, say) is left to the push.
        ahead = _git(path, "rev-list", "--count", "@{upstream}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip() in ("", "0"):
            return True
        committed = _git(path, "show", f"HEAD:./{path.name}") if still is not None else None
        if committed is not None and (committed.returncode != 0 or not still(committed.stdout)):
            log("reconcile: the committed list changed what this look's verdicts were given; not pushed")
            return False
        pushed = _git(path, "push", "-q", upstream[0], f"HEAD:{upstream[1]}", timeout=300)
        if pushed.returncode == 0:
            log(f"reconcile: pushed the target list to {upstream[0]}")
            return True
        if attempt == 1:
            _sync(path)
    log(f"reconcile: push to {upstream[0]} failed ({one_line(pushed.stderr)}); the commit stays local")
    return False


def _taken_back(base: str, ours: str, parent: str, current: str) -> str | None:
    """`current` with this look's edit of each item (`ours` against `base`) undone where it still
    stands, found by area and slug so a line a hook reformatted is still found: the status goes back
    only while it is the one `ours` wrote, a clause `ours` added goes only while it is there as
    written, and a clause `ours` removed returns only with the status. Whatever else changed the line
    meanwhile stays. An item `parent` already holds as `ours` has it stays. None when an item cannot
    be found or put back."""

    def listed(text: str) -> Targets | None:
        try:
            return parse_targets(text)
        except Die:
            return None

    def key(clause: str) -> str:
        return clause.partition(":")[0].strip().lower()

    before, after, held = listed(base), listed(ours), listed(parent)
    if before is None or after is None:
        return None
    text = current
    for item in after.items():
        old = before.find(item.slug)
        if old is None or (old.status, old.meta) == (item.status, item.meta):
            continue
        kept = held.find(item.slug) if held else None
        if kept is not None and (kept.status, kept.meta) == (item.status, item.meta):
            continue
        now = listed(text)
        found = now.find(item.slug) if now else None
        if found is None:
            return None
        reverted = found.status == item.status
        status = old.status if reverted else found.status
        added = [m for m in item.meta if m not in old.meta and m in found.meta]
        keys = {key(m) for m in added}
        restored = [m for m in old.meta if m not in item.meta and key(m) not in {key(c) for c in found.meta}]
        others = [m for m in found.meta if key(m) in keys and m not in added]
        if not reverted and not added:
            continue
        text = rewrite_item(text, found, status, drop=keys, add=[*others, *(restored if reverted else ())])
        again = listed(text)
        if again is None or again.find(item.slug) is None or again.find(item.slug).status != status:
            return None
    return text


def merge3(base: str, ours: str, theirs: str) -> str | None:
    """Two edits of `base` merged (`git merge-file`), or None when they touch the same lines."""
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(tmp) / name).write_text(text)
            files.append(str(Path(tmp) / name))
        merged = subprocess.run(["git", "merge-file", "-p", "-q", *files], capture_output=True, text=True)
    return merged.stdout if merged.returncode == 0 else None


def _write(path: Path, text: str) -> bool:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except OSError as error:
        log(f"reconcile: {path} cannot be written ({error.strerror}); nothing written")
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def publish(path: Path, base: str, ours: str, notes: list[str], *, still=None) -> str | None:
    """Put `ours`, the list as this look edited it from `base`, into the file and return what the
    file then holds, or None when it could not be merged or written. Call with the lock held.

    A tracked list is first brought up to the branch it tracks. The edit is then merged three ways
    with the file as it is now, so an edit made meanwhile by hand or on another host is kept; when
    both touch the same lines nothing is written and a later look starts from the new file. A
    tracked list is committed, that path only, and pushed; when the commit fails this look's edit
    is taken back out of the file, so a later look makes it again. With `still`, nothing is written
    unless `still(the file as it now is)` holds, and a commit that is not pushed in this call, or
    whose committed list fails `still`, is withdrawn by a commit on top (`_owe`); until that
    withdrawal is made nothing is pushed."""
    tracked = _tracked(path)
    if tracked:
        _sync(path)
    theirs = path.read_text()
    if still is not None and not still(theirs):
        log(f"reconcile: {path.name} changed what this look's verdicts were given; nothing written")
        return None
    merged = ours if theirs == base else merge3(base, ours, theirs)
    if merged is None:
        log(f"reconcile: {path.name} changed on the same lines during this look; nothing written")
        return None
    if merged != theirs and not _write(path, merged):
        return None
    if tracked and merged != theirs:
        message = f"Reconcile {path.name}: {len(notes)} item(s)\n\n" + "\n".join(f"- {note}" for note in notes)
        committed = _git(path, "commit", "-q", "-m", message, "--", _spec(path))
        if committed.returncode != 0:
            log(
                f"reconcile: commit failed ({one_line(committed.stderr or committed.stdout)}); the edit waits for a later look"
            )
            # Take back this look's edit alone, keeping one a hook or a hand made meanwhile. When the
            # hook touched the same lines, put each edited item back by its slug instead: a status
            # left behind uncommitted would read as settled and never be published.
            now = path.read_text()
            undone = merge3(merged, now, theirs) or _taken_back(theirs, merged, "", now)
            if undone is None:
                log(f"reconcile: {path.name} changed on the reconciled lines during the commit; left as it is")
            else:
                _write(path, undone)
            return None
    own = still if tracked and merged != theirs else None
    if tracked and not _push(path, own):
        if own is not None:
            # An inferred tick is published in the look that made it or not at all: a later push
            # could carry it onto a goal or item line it was never given.
            _owe(path, base, ours)
            return None
    return merged


def _parsed(path: Path, text: str) -> Targets:
    try:
        return parse_targets(text)
    except Die as error:
        raise Die(f"roadmap targets: {path}: {error}") from None


def _unchanged(path: Path, targets: Targets, items: list[TargetItem]):
    """Whether a later text of the list keeps `targets`' preamble and each of `items` as it was."""

    def basis(listed: Targets, item: TargetItem):
        # The edit an inference makes (status, `inferred:`, no `in flight:`) is not part of it.
        found = listed.find(item.slug)
        own = ("in flight", "inferred")
        meta = found and tuple(m for m in found.meta if m.partition(":")[0].strip().lower() not in own)
        return listed.preamble, found and (found.area, found.text, meta)

    before = [basis(targets, item) for item in items]

    def still(text: str) -> bool:
        try:
            listed = _parsed(path, text)
        except Die:
            return False
        return [basis(listed, item) for item in items] == before

    return still


def _reconcile(w, c, path: Path, profile) -> int | None:
    if _tracked(path):
        _sync(path)
        _push(path)
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as error:
        raise Die(f"roadmap targets: {path}: {getattr(error, 'strerror', None) or error}") from None
    targets = _parsed(path, text)
    infer = inference_enabled()
    try:
        seen = observe(targets, sources(w), open_listing=infer)
    except (GitHubError, SourceUnavailable) as error:
        c.reason = f"pull requests could not be read ({one_line(str(error))})"
        log(f"reconcile: {c.reason}; the target list is left as it is")
        return None
    roadmap = _roadmap(w) if infer else None
    written = 0
    for tier in ("settle", "audit", "infer") if infer else ("settle",):
        if tier == "settle":
            changes = settle(targets, seen)
        elif tier == "audit":
            changes = audit(targets, roadmap)
        else:
            changes = _infer(w, profile, overlay_live(targets, seen.open_keys, set()), roadmap)
        if not changes:
            continue
        notes = [f"`{change.item.slug}`: {change.note}" for change in changes]
        # A model verdict holds for the goal and item line it was given, not for a later edit of them.
        still = _unchanged(path, targets, [change.item for change in changes]) if tier == "infer" else None
        published = publish(path, text, apply(text, changes), notes, still=still)
        if published is None:
            c.reason = f"{len(changes)} edit(s) to the target list could not be written"
            return 0 if written else None
        text, targets = published, _parsed(path, published)
        written += len(changes)
        for note in notes:
            log(f"reconcile: {note}")
    if written:
        return 0
    c.reason = "the target list is current"
    log(f"reconcile: {c.reason}")
    return None


def _open_lock(lock: Path) -> int:
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o664)
        # A new lock records no look yet; its modification time is when one last completed.
        with contextlib.suppress(OSError):
            os.utime(fd, (0, 0))
    except FileExistsError:
        fd = os.open(lock, os.O_RDWR)
    return fd


def reported(file: Path, allowed: Iterable[str]) -> list[str]:
    """The slugs a round wrote to `file`, one per line, that are among `allowed`; every other line,
    and a file over 4 KiB, is ignored."""
    allowed = set(allowed)
    try:
        if file.stat().st_size > 4096:
            log(f"reconcile: {file} is over 4 KiB; ignored")
            return []
        lines = file.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    slugs = [line.strip().strip("`").strip() for line in lines]
    ignored = [slug for slug in slugs if slug and slug not in allowed]
    if ignored:
        log(f"reconcile: ignoring {len(ignored)} line(s) of {file.name} that name no prerequisite the round was shown")
    return list(dict.fromkeys(slug for slug in slugs if slug in allowed))


def reopen(slugs: Iterable[str], seen: dict[str, frozenset[str]], by: str) -> int | None:
    """Open again each of `slugs` that is in `seen` and is still ticked by inference on exactly the
    evidence `seen` gives for it (what the reporting round was shown), dropping its `inferred:`
    clause and recording that evidence and `by` in a `reopened:` clause, so the same evidence does
    not tick it again. Waits up to REOPEN_WAIT seconds for the list's lock and does not move the
    look gap. Returns how many items were opened, or None when the list could not be locked, read
    or written, so that the report can be tried again (`keep_pending`)."""
    listed = roadmap_targets()
    slugs = [slug for slug in dict.fromkeys(slugs) if slug in seen]
    if listed is None or not slugs:
        return 0
    path = listed.resolve()
    by = re.sub(r"[;()]", "", by)
    try:
        fd = _open_lock(_lock_path(path))
    except OSError as error:
        log(f"reconcile: the lock of {path.name} cannot be opened ({error.strerror}); nothing reopened")
        return None
    try:
        deadline = time.time() + REOPEN_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    log(f"reconcile: {path.name} stayed locked; {', '.join(slugs)} not reopened yet")
                    return None
                time.sleep(2)
        try:
            text = path.read_text()
            targets = _parsed(path, text)
        except (OSError, UnicodeDecodeError, Die) as error:
            log(f"reconcile: {path.name} cannot be read ({one_line(str(error))}); nothing reopened yet")
            return None
        changes = []
        for slug in slugs:
            item = targets.find(slug)
            tick = inferred(item) if item is not None else None
            if tick is None or tick[0] != seen[slug]:
                log(f"reconcile: `{slug}` is no longer the inferred tick the round saw; not reopened")
                continue
            clause = f"reopened: {_evidence(tick[0])} by {by}"
            changes.append(Change(item, "open", f"reopened by {by}", ("inferred",), (clause,)))
        notes = [f"`{change.item.slug}`: {change.note}" for change in changes]
        if not changes:
            return 0
        if publish(path, text, apply(text, changes), notes) is None:
            return None
        for note in notes:
            log(f"reconcile: {note}")
        return len(changes)
    finally:
        os.close(fd)


def keep_pending(file: Path, slugs: list[str], seen: dict[str, frozenset[str]], by: str) -> None:
    """Record a report `reopen` could not apply, for `retry_pending`."""
    try:
        rows = json.loads(file.read_text())
    except (OSError, ValueError):
        rows = []
    rows = rows if isinstance(rows, list) else []
    rows.append({"slugs": slugs, "seen": {slug: sorted(found) for slug, found in seen.items()}, "by": by})
    with contextlib.suppress(OSError):
        file.parent.mkdir(parents=True, exist_ok=True)
        _replace_json(file, rows)


def retry_pending(file: Path) -> None:
    """Apply again each report `keep_pending` recorded, keeping those that still cannot be."""
    try:
        rows = json.loads(file.read_text())
    except (OSError, ValueError):
        return
    left = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not isinstance(row.get("seen"), dict):
            continue
        seen = {slug: frozenset(found) for slug, found in row["seen"].items() if isinstance(found, list)}
        if reopen([s for s in row.get("slugs") or () if isinstance(s, str)], seen, str(row.get("by") or "")) is None:
            left.append(row)
    with contextlib.suppress(OSError):
        if left:
            _replace_json(file, left)
        else:
            file.unlink(missing_ok=True)


def _replace_json(file: Path, value) -> None:
    temporary = file.with_name(f".{file.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True))
    os.replace(temporary, file)


def run(w, c, profile=None) -> int | None:
    """One reconciliation of the worker's target list. 0 when the file changed; None, with the
    reason in `c.reason`, when it did not, so that the round goes on to its other work. `profile`
    is the authoring profile an inference look's agent runs with."""
    listed = roadmap_targets()
    if listed is None:
        c.reason = "no target list is set (--roadmap-targets)"
        return None
    path = listed.resolve()
    w.counters.write(ATTEMPT_KEY, int(time.time()))
    lock = _lock_path(path)
    try:
        fd = _open_lock(lock)
    except OSError as error:
        c.reason = f"the lock {lock} cannot be opened ({error.strerror})"
        log(f"reconcile: {c.reason}; the target list is left as it is")
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            c.reason = "another worker is reconciling the target list"
            log(f"reconcile: {c.reason}")
            return None
        waited = time.time() - os.fstat(fd).st_mtime
        if waited < GAP:
            c.reason = f"another worker looked at the target list {waited / 3600:.1f}h ago"
            log(f"reconcile: {c.reason}")
            return None
        rc = _reconcile(w, c, path, profile)
        # Only a look that ran to its end moves the shared gap; one that raised is due again.
        with contextlib.suppress(OSError):
            os.utime(fd)
        return rc
    finally:
        os.close(fd)

"""The `curate` phase: keep an operator's target list true to GitHub and to main.

Runs only for a worker whose `only` names `curate` and that has a target list, at most once per
TAUCETI_CURATE_GAP. What it changes in the file, and why, is documented in docs/target-lists.md
("Curation"). The pieces:

    due(counters)                  whether a look is due, and the reason either way
    settle(targets, ...)           pure: the edits that pull request states and records imply
    evidence(checkout, item)       where a checkout of main declares what an item names
    confirmed(checkout, verdict)   a model verdict reduced to a declaration the checkout has
    publish(path, base, ours, ..)  merge an edit into the file; commit and push a tracked one
    run(w, c, profile)             one curation, under the list's lock

Only `ask` starts a model. Its answer reaches the list as a file path and a declaration name that
`confirmed` found in the checkout, and in no other form.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tauceti_tools.forge import GITHUB, RepositoryRef

from . import incidents
from .config import Die, SourceUnavailable, log, one_line, roadmap_targets
from .constants import TAUCETI
from .github import GitHubError, normalize_merged
from .paths import HERE
from .targets import (
    MERGED_SEARCH,
    GitHubMarkerReads,
    Key,
    TargetItem,
    Targets,
    eligible_items,
    github_history_enabled,
    inflight_ref,
    landed,
    marker_keys,
    overlay_live,
    parse_targets,
    partial_prs,
    ref_key,
    render_item,
    rewrite_item,
)

GAP = int(os.environ.get("TAUCETI_CURATE_GAP", "21600"))
MAX_CANDIDATES = int(os.environ.get("TAUCETI_CURATE_MAX_CANDIDATES", "12"))
MERGED_LIMIT = 1000
MAX_HITS = 5
ATTEMPT_KEY = "curate-attempt-ts"

_IDENTIFIER = re.compile(r"`([A-Za-z][A-Za-z0-9_'.]{3,})(?=[`\s(])")
# POSIX ERE, since `git grep -E` has no \s, \b or \w on every platform.
_DECLARATION = (
    r"^[[:space:]]*(@\[[^]]*\][[:space:]]*)?((protected|private|noncomputable|scoped|unsafe|partial)[[:space:]]+)*"
    r"(theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque)[[:space:]]+"
    r"([^[:space:]]+\.)?(NAME)([^A-Za-z0-9_'.]|$)"
)
_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.lean")
_NAME = re.compile(r"[^\W\d][\w.']*")


@dataclass(frozen=True)
class Change:
    item: TargetItem
    status: str
    note: str  # what happened, for the log and the commit message
    drop: tuple[str, ...] = ()  # clause keys to remove from the item's line
    add: tuple[str, ...] = ()  # clauses to append to it


@dataclass(frozen=True)
class Source:
    """One forge's pull requests as curation reads them. The list names a GitHub pull request `#N`
    and any other by its URL, as the roadmap round does."""

    reads: object  # the GitHub client or the Forgejo worker source
    repository: RepositoryRef
    native: bool = False
    shared: bool = False  # GitHub read from inside another source's round

    def ref(self, number: int) -> str:
        return self.repository.pull_url(number) if self.native else f"#{number}"

    def number(self, ref: str) -> int | None:
        found = re.fullmatch(r"https?://\S*?(\d+)" if self.native else r"#(\d+)", ref)
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

    def view(self, number: int) -> dict:
        row = self._read("pr_view", number, ["state", "title", "body", "headRefOid"]) or {}
        return row if self.native else normalize_merged(row)


class _GitHubHistory(GitHubMarkerReads):
    """`GitHubMarkerReads` with the view of one pull request that curation needs, from the same
    listing. A pull request outside it reads as unknown, and its item is left as it is."""

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
    """What one curation read from the forges and the incident records. A pull request is named as
    the list names it (`Source.ref`)."""

    merged: list[dict]  # merged pull requests that carry a target marker; a Forgejo one has a `url`
    pulls: dict[str, dict]  # the pull request each `in flight:` clause names
    subsumed: dict[str, list[str]]  # a closed one of those -> the merged pull requests replacing it
    open_keys: set[Key]  # the items an open pull request's marker names
    where: dict[str, tuple[RepositoryRef, int]] = field(default_factory=dict)  # a viewed one's repository and number


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
    clauses = " ".join(c.partition(":")[2] for c in item.meta if c.partition(":")[0].strip().lower() == "partial")
    return {f"#{n}" for n in partial_prs(item)} | set(re.findall(r"https?://[^\s,;]+", clauses))


def settle(targets: Targets, seen: Seen) -> tuple[list[Change], list[tuple[TargetItem, str]]]:
    """The edits that `seen` implies for the items not yet done, and the in-flight items it leaves
    to the operator, each with the closed pull request it names.

    An item is done when a merged pull request completes it by marker (`targets.landed`), when the
    pull request its `in flight:` clause names merged, or when that pull request closed and
    `seen.subsumed` names what replaced it. A named pull request that merged as partial reopens
    the item and joins its `partial:` clause; one that was partial and closed reopens it with its
    replacements in that clause instead."""
    pending = [it for it in targets.items() if it.status != "done"]
    excused = {(it.area, it.slug): {ref_key(ref) for ref in _partial_refs(it)} for it in pending}
    by_marker: dict[Key, str] = {}
    for row in seen.merged:
        for key, number in landed(pending, [row]).items():
            ref = row.get("url") or f"#{number}"
            if ref_key(ref) not in excused[key]:
                by_marker.setdefault(key, ref)
    changes: list[Change] = []
    stranded: list[tuple[TargetItem, str]] = []
    for it in pending:
        key = (it.area, it.slug)
        named = inflight_ref(it) if it.status == "inflight" else None
        pull = seen.pulls.get(named) or {}
        partial = key in marker_keys(pull.get("body"))[1] or (named is not None and ref_key(named) in excused[key])
        if key in by_marker:
            ref = by_marker[key]
            changes.append(Change(it, "done", f"{ref} merged", ("in flight",), (f"landed: {ref}",)))
        elif pull.get("state") == "MERGED" and partial:
            clause = "partial: " + _refs([*_partial_refs(it), named])
            note = f"{named} merged without completing it; open again"
            changes.append(Change(it, "open", note, ("in flight", "partial"), (clause,)))
        elif pull.get("state") == "MERGED":
            changes.append(Change(it, "done", f"{named} merged", ("in flight",), (f"landed: {named}",)))
        elif pull.get("state") == "CLOSED" and named in seen.subsumed and partial:
            replaced = seen.subsumed[named]
            clauses = ("partial: " + _refs([*_partial_refs(it), *replaced]), f"closed: {named}")
            note = f"{named} closed without completing it, subsumed by {_refs(replaced)}; open again"
            changes.append(Change(it, "open", note, ("in flight", "partial"), clauses))
        elif pull.get("state") == "CLOSED" and named in seen.subsumed:
            by = _refs(seen.subsumed[named])
            clauses = (f"subsumed by: {by}", f"closed: {named}")
            changes.append(Change(it, "done", f"{named} closed, subsumed by {by}", ("in flight",), clauses))
        elif pull.get("state") == "CLOSED":
            stranded.append((it, named))
    return changes, stranded


def apply(text: str, changes: Iterable[Change]) -> str:
    for change in changes:
        text = rewrite_item(text, change.item, change.status, drop=change.drop, add=change.add)
    return text


def observe(targets: Targets, read: list[Source]) -> Seen:
    """Read what `settle` needs from each source: its merged pull requests that carry a marker, its
    open ones, one view per `in flight:` clause that names one of its pull requests, and one per
    pull request a `declined` record about a closed one at its last head names as replacing it. A
    replacement counts only when every pull request the record names has merged. A clause naming a
    pull request of no source in `read` is not looked at. Raises GitHubError or SourceUnavailable
    when a listing fails."""
    merged: list[dict] = []
    open_keys: set[Key] = set()
    for source in read:
        merged += source.merged_prs()
        for d in source.open_prs():
            open_keys |= marker_keys(d.get("body"))[0]
    named = {inflight_ref(it) for it in targets.items() if it.status == "inflight"} - {None}
    pulls, subsumed, where = {}, {}, {}
    for ref in sorted(named):
        source = next((s for s in read if s.number(ref) is not None), None)
        if source is None:
            continue
        number = source.number(ref)
        pulls[ref], where[ref] = source.view(number), (source.repository, number)
        head = pulls[ref].get("headRefOid") or ""
        by = incidents.subsumed_by(number, head, source.repository) if pulls[ref].get("state") == "CLOSED" else []
        if by and all(source.view(m).get("state") == "MERGED" for m in by):
            subsumed[ref] = [source.ref(m) for m in by]
    return Seen(merged, pulls, subsumed, open_keys, where)


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


def evidence(checkout: Path, item: TargetItem) -> dict[str, list[str]]:
    """Where the checkout declares what `item` names, by name. The names its description quotes
    count when every one of them is declared; the slug counts when a declaration carries its words
    in any case, with or without underscores between them. Empty when neither holds."""
    names = identifiers(item.text)
    hits = {name: declared(checkout, re.escape(name.rsplit(".", 1)[-1])) for name in names}
    found = hits if names and all(hits.values()) else {}
    words = "_?".join(re.escape(word) for word in item.slug.split("-") if word)
    by_slug = declared(checkout, words, ignore_case=True) if words else []
    return {**found, f"slug {item.slug}": by_slug} if by_slug else found


def context(targets: Targets, item: TargetItem) -> tuple[str, str]:
    """The list's goal (its preamble) and the item's line as the authoring prompt shows them."""
    return targets.preamble, render_item(targets, item)


def _fingerprint(checkout: Path, item: TargetItem, hits: dict[str, list[str]], around: tuple[str, ...] = ()) -> str:
    """Identifies what a verdict on `item` was given on: its description and `around` (its
    `context`), the names looked up, and the whole of every checkout file a hit is in, since a
    declaration's statement runs on past the line that names it."""
    digest = hashlib.sha256(json.dumps([item.text, list(around), sorted(hits)]).encode())
    for file in sorted({hit.split(":", 1)[0] for found in hits.values() for hit in found}):
        digest.update(b"\0" + file.encode() + b"\0")
        with contextlib.suppress(OSError):
            digest.update(hashlib.sha256((checkout / file).read_bytes()).digest())
    return digest.hexdigest()


def confirmed(checkout: Path, verdict) -> str | None:
    """`<file> <declaration>` when `verdict` is `{"landed": true, "file": ..., "declaration": ...}`
    and that file of the checkout declares that name; None for anything else."""
    if not (isinstance(verdict, dict) and verdict.get("landed") is True):
        return None
    file, name = verdict.get("file"), verdict.get("declaration")
    if not (isinstance(file, str) and isinstance(name, str) and _FILE.fullmatch(file) and _NAME.fullmatch(name)):
        return None
    if ".." in Path(file).parts or not (checkout / file).is_file():
        return None
    return f"{file} {name}" if declared(checkout, re.escape(name.rsplit(".", 1)[-1]), pathspec=file) else None


def ask(w, profile, checkout: Path, targets: Targets, candidates: list[tuple[TargetItem, dict]]) -> dict:
    """Put the list's goal, the candidates and their hits to one host agent and return the
    `verdicts.json` it wrote, `{}` when it wrote none that parses."""
    from .agents import fill_prompt, run_agent_host
    from .claude_api import blocked_failure

    work = w.cfg.state / "curate" / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    listing, verdicts = work / "candidates.json", work / "verdicts.json"
    rows = [
        {
            "slug": it.slug,
            "area": it.area,
            "milestone": it.text,
            "line": context(targets, it)[1],
            "hits": {name: found[:MAX_HITS] for name, found in hits.items()},
        }
        for it, hits in candidates
    ]
    listing.write_text(
        json.dumps(
            {"checkout": str(checkout), "goal": targets.preamble, "candidates": rows}, indent=1, ensure_ascii=False
        )
    )
    prompt = fill_prompt(
        HERE / "prompts" / "curate.md", CANDIDATES=str(listing), CHECKOUT=str(checkout), VERDICTS=str(verdicts)
    )
    rc = run_agent_host(work, prompt, profile, w.cfg.logdir, quota_cfg=w.cfg)
    if rc != 0:
        if failure := blocked_failure():
            raise failure
        log(f"curate: the agent exited {rc}; no verdict is used")
        return {}
    try:
        answer = json.loads(verdicts.read_text())
    except (OSError, ValueError):
        log("curate: the agent wrote no verdicts.json that parses; no verdict is used")
        return {}
    return answer if isinstance(answer, dict) else {}


def _git(path: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
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
            env={**operator_environment(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 124, "", str(error))


def _tracked(path: Path) -> bool:
    return _git(path, "ls-files", "--error-unmatch", "--", path.name).returncode == 0


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
        log(f"curate: fetch from {upstream[0]} failed ({one_line(fetched.stderr)}); working from the local list")
    elif _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        log(f"curate: {path.parent} has uncommitted changes; not rebasing it onto {upstream[0]}")
    elif _git(path, "rebase", "-q", "@{upstream}").returncode != 0:
        _git(path, "rebase", "--abort")
        log(f"curate: local commits in {path.parent} conflict with {upstream[0]}; rebase there by hand")


def _push(path: Path, still=None) -> bool:
    """Push the list's branch to the branch it tracks when it is ahead, once more after a sync if
    the remote has moved; whether nothing is left unpushed. A commit that cannot be pushed stays
    local and goes with a later call. With `still`, a sync after which `still(the file)` fails
    stops the push."""
    upstream = _upstream(path)
    if upstream is None:
        return True
    for attempt in (1, 2):
        ahead = _git(path, "rev-list", "--count", "@{upstream}..HEAD")
        if ahead.returncode != 0 or ahead.stdout.strip() in ("", "0"):
            return True
        pushed = _git(path, "push", "-q", upstream[0], f"HEAD:{upstream[1]}", timeout=300)
        if pushed.returncode == 0:
            log(f"curate: pushed {ahead.stdout.strip()} commit(s) of the target list to {upstream[0]}")
            return True
        if attempt == 1:
            _sync(path)
            if still is not None and not still(path.read_text()):
                log(f"curate: {upstream[0]} changed what this curation's verdicts were given; not pushed")
                return False
    log(f"curate: push to {upstream[0]} failed ({one_line(pushed.stderr)}); the commit stays local")
    return False


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
        log(f"curate: {path} cannot be written ({error.strerror}); nothing written")
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def publish(path: Path, base: str, ours: str, notes: list[str], *, still=None) -> str | None:
    """Put `ours`, the list as this curation edited it from `base`, into the file and return what
    the file then holds, or None when it could not be merged or written. Call with the lock held.

    A tracked list is first brought up to the branch it tracks. The edit is then merged three ways
    with the file as it is now, so an edit made meanwhile by hand or on another host is kept; when
    both touch the same lines nothing is written and a later curation starts from the new file. A
    tracked list is committed, that path only, and pushed; when the commit fails this curation's
    edit is taken back out of the file, so a later curation makes it again. With `still`, nothing
    is written unless `still(the file as it now is)` holds, and a commit that is not pushed in this
    call, or that a sync puts onto a file where `still` fails, is taken back."""
    tracked = _tracked(path)
    if tracked:
        _sync(path)
    theirs = path.read_text()
    if still is not None and not still(theirs):
        log(f"curate: {path.name} changed what this curation's verdicts were given; nothing written")
        return None
    merged = ours if theirs == base else merge3(base, ours, theirs)
    if merged is None:
        log(f"curate: {path.name} changed on the same lines during this curation; nothing written")
        return None
    if merged != theirs and not _write(path, merged):
        return None
    if tracked and merged != theirs:
        message = f"Curate {path.name}: {len(notes)} item(s)\n\n" + "\n".join(f"- {note}" for note in notes)
        committed = _git(path, "commit", "-q", "-m", message, "--", path.name)
        if committed.returncode != 0:
            log(
                f"curate: commit failed ({one_line(committed.stderr or committed.stdout)}); the edit waits for a later look"
            )
            # Take back this curation's edit alone, keeping one a hook or a hand made meanwhile.
            undone = merge3(merged, path.read_text(), theirs)
            if undone is None:
                log(f"curate: {path.name} changed on the curated lines during the commit; left as it is")
            else:
                _write(path, undone)
            return None
    if tracked and not _push(path, still) and still is not None and merged != theirs:
        # A verdict is published in the look that made it or not at all: a later push could carry
        # it onto a goal or item line it was never given.
        if _git(path, "reset", "-q", "--keep", "HEAD~1").returncode != 0:
            log(f"curate: the unpushed verdict commit in {path.parent} could not be taken back; remove it by hand")
        else:
            log("curate: the verdicts were taken back; the next look asks again")
        return None
    return merged


def _main_checkout(w) -> Path | None:
    from .agents import fetch_ref

    checkout = w.cfg.state / "curate" / "main"
    return checkout if fetch_ref(TAUCETI, checkout) else None


def _landed_elsewhere(w, profile, live: Targets) -> list[Change]:
    """The eligible items of `live` that a model verdict, checked against a fresh checkout of main,
    shows landed. At most MAX_CANDIDATES items with evidence are put to the model, in file order;
    one it found not landed is not asked about again until its description or a file declaring
    what it names changes."""
    eligible = [it for area in live.areas for it in eligible_items(live, area)]
    checkout = _main_checkout(w) if eligible else None
    if checkout is None:
        if eligible:
            log("curate: no checkout of main; eligible items are not looked for there")
        return []
    memo_file = w.cfg.state / "curate" / "not-landed.json"
    try:
        memo = json.loads(memo_file.read_text())
    except (OSError, ValueError):
        memo = {}
    memo = {it.slug: memo[it.slug] for it in eligible if isinstance(memo, dict) and it.slug in memo}
    candidates = []
    for it in eligible:
        hits = evidence(checkout, it)
        if hits and memo.get(it.slug) != _fingerprint(checkout, it, hits, context(live, it)):
            candidates.append((it, hits))
        if len(candidates) >= MAX_CANDIDATES:
            break
    changes = []
    if candidates:
        log(f"curate: asking about {len(candidates)} eligible item(s) that main declares by name")
        verdicts = ask(w, profile, checkout, live, candidates)
        for it, hits in candidates:
            verdict = verdicts.get(it.slug)
            where = confirmed(checkout, verdict)
            if where:
                clause = f"landed elsewhere: {where}"
                changes.append(Change(it, "done", f"landed elsewhere, {where}", ("in flight",), (clause,)))
            elif isinstance(verdict, dict) and verdict.get("landed") is False:
                memo[it.slug] = _fingerprint(checkout, it, hits, context(live, it))
                log(f"curate: `{it.slug}` has not landed: {one_line(str(verdict.get('missing') or ''))[:160]}")
            else:
                log(f"curate: `{it.slug}`: no usable verdict; it stays open")
    with contextlib.suppress(OSError):
        memo_file.parent.mkdir(parents=True, exist_ok=True)
        memo_file.write_text(json.dumps(memo, indent=1, sort_keys=True))
    return changes


def _parsed(path: Path, text: str) -> Targets:
    try:
        return parse_targets(text)
    except Die as error:
        raise Die(f"roadmap targets: {path}: {error}") from None


def _unchanged(path: Path, targets: Targets, items: list[TargetItem]):
    """Whether a later text of the list keeps `targets`' preamble and each of `items` as it was."""

    def basis(listed: Targets, item: TargetItem):
        # The edit a verdict makes (status, `landed elsewhere:`, no `in flight:`) is not part of it.
        found = listed.find(item.slug)
        own = ("in flight", "landed elsewhere")
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


def _curate(w, c, path: Path, profile) -> int | None:
    if _tracked(path):
        _sync(path)
        _push(path)
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as error:
        raise Die(f"roadmap targets: {path}: {getattr(error, 'strerror', None) or error}") from None
    targets = _parsed(path, text)
    try:
        seen = observe(targets, sources(w))
    except (GitHubError, SourceUnavailable) as error:
        c.reason = f"pull requests could not be read ({one_line(str(error))})"
        log(f"curate: {c.reason}; the target list is left as it is")
        return None
    changes, stranded = settle(targets, seen)
    for it, ref in stranded:
        log(f"curate: `{it.slug}` is in flight on {ref}, which closed unmerged; left for the operator")
        repository, number = seen.where[ref]
        incidents.note_target_closed(path, it.area, it.slug, number, repository)
    written = 0
    for tier in (1, 2):
        if tier == 2:
            changes = _landed_elsewhere(w, profile, overlay_live(targets, seen.open_keys, set()))
        if not changes:
            continue
        notes = [f"`{change.item.slug}`: {change.note}" for change in changes]
        # A model verdict holds for the goal and item line it was given, not for a later edit of them.
        still = _unchanged(path, targets, [change.item for change in changes]) if tier == 2 else None
        published = publish(path, text, apply(text, changes), notes, still=still)
        if published is None:
            c.reason = f"{len(changes)} edit(s) to the target list could not be written"
            return 0 if written else None
        text, targets = published, _parsed(path, published)
        written += len(changes)
        for note in notes:
            log(f"curate: {note}")
    if written:
        return 0
    c.reason = "the target list is current"
    if stranded:
        c.reason += f"; {len(stranded)} in-flight item(s) name a closed pull request and wait for the operator"
    log(f"curate: {c.reason}")
    return None


def run(w, c, profile) -> int | None:
    """One curation of the worker's target list. 0 when the file changed; None, with the reason in
    `c.reason`, when it did not, so that the round goes on to its other work."""
    listed = roadmap_targets()
    if listed is None:
        c.reason = "no target list is set (--roadmap-targets)"
        return None
    path = listed.resolve()
    w.counters.write(ATTEMPT_KEY, int(time.time()))
    lock = _lock_path(path)
    try:
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o664)
            # A new lock records no look yet; its modification time is when one last completed.
            with contextlib.suppress(OSError):
                os.utime(fd, (0, 0))
        except FileExistsError:
            fd = os.open(lock, os.O_RDWR)
    except OSError as error:
        c.reason = f"the lock {lock} cannot be opened ({error.strerror})"
        log(f"curate: {c.reason}; the target list is left as it is")
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            c.reason = "another worker is curating the target list"
            log(f"curate: {c.reason}")
            return None
        waited = time.time() - os.fstat(fd).st_mtime
        if waited < GAP:
            c.reason = f"another worker looked at the target list {waited / 3600:.1f}h ago"
            log(f"curate: {c.reason}")
            return None
        rc = _curate(w, c, path, profile)
        # Only a look that ran to its end moves the shared gap; one that raised is due again.
        with contextlib.suppress(OSError):
            os.utime(fd)
        return rc
    finally:
        os.close(fd)

"""The `reconcile` phase: write back what an operator's target list's own pull requests settled.

Runs only for a worker whose `only` names `reconcile` and that has a target list, at most once per
TAUCETI_RECONCILE_GAP. What it changes in the file, and why, is documented in docs/target-lists.md
("Reconciliation"). It starts no model and makes no judgement of content: every edit follows from
the state of a pull request that carries the item's marker or that the item's `in flight:` clause
names. The pieces:

    due(counters)                  whether a look is due, and the reason either way
    settle(targets, seen)          pure: the edits that pull request states imply
    publish(path, base, ours, ..)  merge an edit into the file; commit and push a tracked one
    run(w, c)                      one reconciliation, under the list's lock
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tauceti_tools.forge import GITHUB, RepositoryRef

from .config import Die, SourceUnavailable, log, one_line, roadmap_targets
from .constants import TAUCETI
from .github import GitHubError, normalize_merged
from .targets import (
    MERGED_SEARCH,
    GitHubMarkerReads,
    Key,
    TargetItem,
    Targets,
    _is_partial,
    content_base,
    github_history_enabled,
    inflight_ref,
    landed,
    marker_keys,
    parse_targets,
    partial_prs,
    ref_key,
    rewrite_item,
)

GAP = int(os.environ.get("TAUCETI_RECONCILE_GAP", "21600"))
MERGED_LIMIT = 1000
ATTEMPT_KEY = "reconcile-attempt-ts"


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
        row = self._read("pr_view", number, ["state", "title", "body", "baseRefName"]) or {}
        return normalize_merged(row)


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


def observe(targets: Targets, read: list[Source]) -> Seen:
    """Read what `settle` needs from each source: its merged pull requests that carry a marker, and
    one view per `in flight:` clause that names one of its pull requests. A clause naming a pull
    request of no source in `read` is not looked at. Raises GitHubError or SourceUnavailable when a
    listing fails."""
    merged: list[dict] = []
    for source in read:
        merged += source.merged_prs()
    named = {inflight_ref(it) for it in targets.items() if it.status == "inflight"} - {None}
    pulls = {}
    for ref in sorted(named):
        source = next((s for s in read if s.number(ref) is not None), None)
        if source is not None:
            pulls[ref] = source.view(source.number(ref))
    return Seen(merged, pulls)


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
        log(f"reconcile: fetch from {upstream[0]} failed ({one_line(fetched.stderr)}); working from the local list")
    elif _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        log(f"reconcile: {path.parent} has uncommitted changes; not rebasing it onto {upstream[0]}")
    elif _git(path, "rebase", "-q", "@{upstream}").returncode != 0:
        _git(path, "rebase", "--abort")
        log(f"reconcile: local commits in {path.parent} conflict with {upstream[0]}; rebase there by hand")


def _push(path: Path) -> bool:
    """Push the list's branch to the branch it tracks when it is ahead, once more after a sync if
    the remote has moved; whether nothing is left unpushed. A commit that cannot be pushed stays
    local and goes with a later call."""
    upstream = _upstream(path)
    if upstream is None:
        return True
    for attempt in (1, 2):
        # A failed comparison (the tracked branch not on the remote yet, say) is left to the push.
        ahead = _git(path, "rev-list", "--count", "@{upstream}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip() in ("", "0"):
            return True
        pushed = _git(path, "push", "-q", upstream[0], f"HEAD:{upstream[1]}", timeout=300)
        if pushed.returncode == 0:
            log(f"reconcile: pushed the target list to {upstream[0]}")
            return True
        if attempt == 1:
            _sync(path)
    log(f"reconcile: push to {upstream[0]} failed ({one_line(pushed.stderr)}); the commit stays local")
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
        log(f"reconcile: {path} cannot be written ({error.strerror}); nothing written")
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def publish(path: Path, base: str, ours: str, notes: list[str]) -> str | None:
    """Put `ours`, the list as this look edited it from `base`, into the file and return what the
    file then holds, or None when it could not be merged or written. Call with the lock held.

    A tracked list is first brought up to the branch it tracks. The edit is then merged three ways
    with the file as it is now, so an edit made meanwhile by hand or on another host is kept; when
    both touch the same lines nothing is written and a later look starts from the new file. A
    tracked list is committed, that path only, and pushed; when the commit fails this look's edit
    is taken back out of the file, so a later look makes it again."""
    tracked = _tracked(path)
    if tracked:
        _sync(path)
    theirs = path.read_text()
    merged = ours if theirs == base else merge3(base, ours, theirs)
    if merged is None:
        log(f"reconcile: {path.name} changed on the same lines during this look; nothing written")
        return None
    if merged != theirs and not _write(path, merged):
        return None
    if tracked and merged != theirs:
        message = f"Reconcile {path.name}: {len(notes)} item(s)\n\n" + "\n".join(f"- {note}" for note in notes)
        committed = _git(path, "commit", "-q", "-m", message, "--", path.name)
        if committed.returncode != 0:
            log(
                f"reconcile: commit failed ({one_line(committed.stderr or committed.stdout)}); the edit waits for a later look"
            )
            # Take back this look's edit alone, keeping one a hook or a hand made meanwhile.
            undone = merge3(merged, path.read_text(), theirs)
            if undone is None:
                log(f"reconcile: {path.name} changed on the reconciled lines during the commit; left as it is")
            else:
                _write(path, undone)
            return None
    if tracked:
        _push(path)
    return merged


def _parsed(path: Path, text: str) -> Targets:
    try:
        return parse_targets(text)
    except Die as error:
        raise Die(f"roadmap targets: {path}: {error}") from None


def _reconcile(w, c, path: Path) -> int | None:
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
        log(f"reconcile: {c.reason}; the target list is left as it is")
        return None
    changes = settle(targets, seen)
    if not changes:
        c.reason = "the target list is current"
        log(f"reconcile: {c.reason}")
        return None
    notes = [f"`{change.item.slug}`: {change.note}" for change in changes]
    if publish(path, text, apply(text, changes), notes) is None:
        c.reason = f"{len(changes)} edit(s) to the target list could not be written"
        return None
    for note in notes:
        log(f"reconcile: {note}")
    return 0


def run(w, c) -> int | None:
    """One reconciliation of the worker's target list. 0 when the file changed; None, with the
    reason in `c.reason`, when it did not, so that the round goes on to its other work."""
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
        rc = _reconcile(w, c, path)
        # Only a look that ran to its end moves the shared gap; one that raised is due again.
        with contextlib.suppress(OSError):
            os.utime(fd)
        return rc
    finally:
        os.close(fd)

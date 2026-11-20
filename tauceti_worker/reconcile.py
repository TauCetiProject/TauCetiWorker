"""The `reconcile` phase: write back what an operator's target list's own pull requests settled.

Runs only for a worker whose `only` names `reconcile` and that has a target list, at most once per
TAUCETI_RECONCILE_GAP. What it changes in the file, and why, is documented in docs/target-lists.md
("Reconciliation"). It starts no model and makes no judgement of content: every edit follows from
the state of a pull request that carries the item's marker or that the item's `in flight:` clause
names. The pieces:

    due(counters)                  whether a look is due, and the reason either way
    settle(targets, seen)          pure: the edits that pull request states imply
    publish(path, plan)            plan the edits on the published list and publish them
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
from .github import BORS_MERGED_PREFIX, GitHubError
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
    verified_landings,
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
    one view per `in flight:` clause that names one of its pull requests. A pull request closed under
    the queue's title counts as merged only with its source's landing proof (`verified_landings`,
    `Source.view`). A clause naming a pull request of no source in `read` is not looked at. Raises GitHubError or SourceUnavailable when a
    listing fails."""
    merged: list[dict] = []
    pending = [it for it in targets.items() if it.status != "done"]
    for source in read:
        for row in source.merged_prs():
            if row.get("state") != "CLOSED":
                merged.append(row)
            elif verified_landings(pending, [row], source):
                merged.append({**row, "state": "MERGED"})
    named = {inflight_ref(it) for it in targets.items() if it.status == "inflight"} - {None}
    pulls = {}
    for ref in sorted(named):
        source = next((s for s in read if s.number(ref) is not None), None)
        if source is not None:
            pulls[ref] = source.view(source.number(ref))
    return Seen(merged, pulls)


def _git(path: Path, *args: str, timeout: int = 120, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Git in the list's clone (or in `cwd`, a worktree of it), with the operator's identity,
    credentials and hooks even inside a source's activation."""
    from .worker_sources import operator_environment

    argv = ["git", "-C", str(cwd or path.parent), *args]
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


def _spec(path: Path) -> str:
    """The list as a pathspec that matches its own name only, whatever glob characters it holds."""
    return f":(literal){path.name}"


def _tracked(path: Path) -> bool:
    return _git(path, "ls-files", "--error-unmatch", "--", _spec(path)).returncode == 0


def _upstream(path: Path) -> tuple[str, str, str] | None:
    """(remote, its ref, the local ref tracking it) for the branch checked out around the list."""
    head = _git(path, "symbolic-ref", "-q", "HEAD").stdout.strip()
    if not head:
        return None
    found = _git(
        path, "for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref) %(upstream)", head
    ).stdout.split()
    return (found[0], found[1], found[2]) if len(found) == 3 and found[0] != "." else None


def _sync(path: Path) -> None:
    """Fast-forward the clone around the list to the branch it tracks, as far as the last fetch saw.
    A clone with uncommitted changes to tracked files, or with commits of its own, is left as it
    is."""
    if _upstream(path) is None:
        return
    if _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        log(f"reconcile: {path.parent} has uncommitted changes; not bringing it up to its upstream")
    elif _git(path, "merge", "-q", "--ff-only", "@{upstream}").returncode != 0:
        log(f"reconcile: {path.parent} has commits its upstream lacks; bring it up to date by hand")


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


def _edits(text: str, plan) -> tuple[str, list[Change]]:
    changes = plan(parse_targets(text))
    return apply(text, changes), changes


def _notes(changes: list[Change]) -> list[str]:
    return [f"`{change.item.slug}`: {change.note}" for change in changes]


def _carries(text: str, changes: list[Change]) -> bool:
    """Whether `text` holds each change as planned: the item under its area, with its status, the
    clauses it added, and none it dropped and did not add back."""
    try:
        listed = parse_targets(text)
    except Die:
        return False
    for change in changes:
        it = listed.find(change.item.slug)
        dropped = {key.strip().lower() for key in change.drop}
        if it is None or (it.area, it.status) != (change.item.area, change.status):
            return False
        if not set(change.add) <= set(it.meta) or any(
            m.partition(":")[0].strip().lower() in dropped and m not in change.add for m in it.meta
        ):
            return False
    return True


def _in_place(path: Path, plan) -> list[str] | None:
    """`publish` for a list that is not tracked on a remote: planned on the file as it is and
    written by a rename, planned again if the file changed meanwhile."""
    for _ in (1, 2):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            return None
        ours, changes = _edits(text, plan)
        if ours == text:
            return []
        if path.read_text() == text:
            return _notes(changes) if _write(path, ours) else None
    log(f"reconcile: {path.name} kept changing during this look; nothing written")
    return None


def _snapshot(path: Path) -> str:
    """The list as `publish` would start from: the remote branch its clone tracks, freshly fetched,
    when it is tracked on one; the file otherwise."""
    upstream = _upstream(path) if _tracked(path) else None
    if upstream is not None:
        _git(path, "fetch", "-q", upstream[0], timeout=300)
        _sync(path)
        name = _git(path, "ls-files", "-z", "--full-name", "--", _spec(path)).stdout.split("\0")[0]
        for base in (upstream[2], "HEAD"):
            shown = _git(path, "show", f"{base}:{name}")
            if shown.returncode == 0:
                return shown.stdout
    return path.read_text()


def publish(path: Path, plan, *, check=None) -> list[str] | None:
    """Apply `plan` to the list where it is published and return one note per edit, `[]` when it
    changes nothing, or None when nothing could be published. Call with the lock held.

    `plan(listed)` gives the edits for the parsed list it is handed; it is run on the text it will
    replace, so every edit is made against the list as it is there, never merged into it. A list
    tracked on a remote is published from a throwaway worktree of the branch it tracks, as fetched
    now, committed through the clone's own hooks and pushed only if the commit changes the list
    alone and the list as committed still carries every planned edit (`_carries`) and, when given,
    satisfies `check`; the worktree is then removed. A rejected commit, check or push leaves nothing
    behind, and a later look plans again. The clone around the list is then fast-forwarded when it
    is clean (`_sync`). Any other list is edited in place."""
    upstream = _upstream(path) if _tracked(path) else None
    if upstream is None:
        return _in_place(path, plan)
    remote, ref, tracking = upstream
    fetched = _git(path, "fetch", "-q", remote, timeout=300)
    if fetched.returncode != 0:
        log(f"reconcile: fetch from {remote} failed ({one_line(fetched.stderr)}); nothing published")
        return None
    name = _git(path, "ls-files", "-z", "--full-name", "--", _spec(path)).stdout.split("\0")[0]
    top = Path(_git(path, "rev-parse", "--show-toplevel").stdout.strip())
    # The clone's hooks, wherever `core.hooksPath` puts them; a relative one would otherwise be
    # looked for inside the temporary worktree.
    hooks = top / _git(path, "rev-parse", "--git-path", "hooks", cwd=top).stdout.strip()
    # A tracked branch the remote does not have yet starts from the clone's own commit.
    base = tracking if _git(path, "rev-parse", "-q", "--verify", tracking).returncode == 0 else "HEAD"
    with tempfile.TemporaryDirectory(prefix="reconcile-") as tmp:
        tree = Path(tmp) / "tree"
        if _git(path, "worktree", "add", "-q", "--detach", str(tree), base).returncode != 0:
            log(f"reconcile: no worktree of {base} could be made; nothing published")
            return None
        try:
            return _publish_from(path, tree, name, hooks, plan, check, remote, ref)
        finally:
            _git(path, "worktree", "remove", "--force", str(tree))
            _git(path, "worktree", "prune")
            _sync(path)


def _publish_from(path, tree: Path, name: str, hooks: Path, plan, check, remote: str, ref: str) -> list[str] | None:
    try:
        text = (tree / name).read_text()
    except (OSError, UnicodeDecodeError):
        log(f"reconcile: {remote} has no readable {name}; nothing published")
        return None
    ours, changes = _edits(text, plan)
    if ours == text:
        return []
    (tree / name).write_text(ours)
    notes = _notes(changes)
    message = f"Reconcile {path.name}: {len(notes)} item(s)\n\n" + "\n".join(f"- {note}" for note in notes)
    committed = _git(
        path, "-c", f"core.hooksPath={hooks}", "commit", "-q", "-m", message, "--", f":(literal){name}", cwd=tree
    )
    if committed.returncode != 0:
        log(f"reconcile: commit failed ({one_line(committed.stderr or committed.stdout)}); a later look tries again")
        return None
    touched = _git(path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD", cwd=tree)
    if [p for p in touched.stdout.split("\0") if p] != [name]:
        log(f"reconcile: the commit changes more than {name}; not pushed")
        return None
    shown = _git(path, "show", f"HEAD:{name}", cwd=tree)
    if shown.returncode != 0 or not _carries(shown.stdout, changes) or (check is not None and not check(shown.stdout)):
        log("reconcile: the list as committed is not what this look's edits were made for; not pushed")
        return None
    pushed = _git(path, "push", "-q", remote, f"HEAD:{ref}", timeout=300, cwd=tree)
    if pushed.returncode != 0:
        log(f"reconcile: push to {remote} failed ({one_line(pushed.stderr)}); a later look tries again")
        return None
    log(f"reconcile: pushed the target list to {remote}")
    _git(path, "fetch", "-q", remote, timeout=300)
    return notes


def _parsed(path: Path, text: str) -> Targets:
    try:
        return parse_targets(text)
    except Die as error:
        raise Die(f"roadmap targets: {path}: {error}") from None


def _reconcile(w, c, path: Path) -> int | None:
    try:
        text = _snapshot(path)
    except (OSError, UnicodeDecodeError) as error:
        raise Die(f"roadmap targets: {path}: {getattr(error, 'strerror', None) or error}") from None
    observed = {"targets": _parsed(path, text)}
    read = sources(w)
    try:
        observed["seen"] = observe(observed["targets"], read)
    except (GitHubError, SourceUnavailable) as error:
        c.reason = f"pull requests could not be read ({one_line(str(error))})"
        log(f"reconcile: {c.reason}; the target list is left as it is")
        return None
    if not settle(observed["targets"], observed["seen"]):
        c.reason = "the target list is current"
        log(f"reconcile: {c.reason}")
        return None

    def plan(listed: Targets) -> list[Change]:
        # The list published may have moved since it was observed; its references are read afresh.
        if listed != observed["targets"]:
            observed.update(targets=listed, seen=observe(listed, read))
        return settle(listed, observed["seen"])

    try:
        notes = publish(path, plan)
    except (GitHubError, SourceUnavailable) as error:
        notes = None
        log(f"reconcile: pull requests could not be read again ({one_line(str(error))})")
    if not notes:
        c.reason = (
            "the edits to the target list could not be published" if notes is None else "the target list is current"
        )
        log(f"reconcile: {c.reason}")
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

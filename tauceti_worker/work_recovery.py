"""tauceti_worker.work_recovery — preserve unfinished source before destructive checkout preparation.

Checkout preparation force-checks-out main, cleans the tree and later replaces PR branches. A round
that timed out, crashed, or ended while one of its tools was still running leaves source that exists
nowhere else. Before any of those operations this module inspects the checkout and, when anything
unpublished is present, writes an atomic, worker-private recovery record:

    state/<worker>/recovery/<round-id>/
        manifest.json    schema tauceti-work-recovery/v1, written last
        commits.bundle   unpublished commits (absent when there are none)
        staged.patch     binary index-vs-HEAD diff
        unstaged.patch   binary worktree-vs-index diff (conflicted paths are archived instead)
        untracked.tar    bounded archive of untracked source and conflicted paths; symlinks as links
        status.json      mutable disposition: preserved, checkout-reset, retained, superseded

A reset happens only after the record is durable (every file fsynced, the directory renamed into
place) and a final inspection shows the checkout still matches it. Preparation never stops for a
human: OOMs and SIGKILLs are routine, so their leftovers are saved and the checkout is reset. When a
process still uses the checkout, saving fails, or the tree will not hold still, the whole checkout
is moved aside unmodified and preparation continues on a fresh clone; bounded background activity housekeeping saves and
removes the retained copy once ordinary ownership and live-reference checks permit it. A clean checkout with nothing unpublished needs no
record at all.

"Published" is decided locally, never from GitHub: a commit is published when it is reachable from a
remote-tracking ref or from refs/tauceti/published/*, which git-safe-push writes after a successful
push and the worker writes for every remote head it checks out. The current source fingerprint looks up a completed descriptor in a disposable index,
so warm preparation need not rehash historical bundles. Preserved refs remain for compatibility.

This module saves work and prevents destruction. It never pushes, opens a PR, or infers that work
was published from a transcript; resuming retained work is a separate consumer of this format.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import tarfile
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from . import recovery_index
from .config import log
from .constants import ROADMAP, TAUCETI
from .paths import HERE
from .runtime_status import atomic_json, read_json

SCHEMA = "tauceti-work-recovery/v1"
PUBLISHED = "refs/tauceti/published"
PRESERVED = "refs/tauceti/preserved"
PENDING = "refs/tauceti-pending"  # outside refs/tauceti/, so a pending tip never counts as preserved
PSEUDO_HEADS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD")

MAX_UNTRACKED_BYTES = 128 * 1024 * 1024
MAX_UNTRACKED_FILES = 20000

# Untracked or ignored paths that are dependency/build caches, never source. Ignored is not by itself
# proof of a cache: `git clean -x` would delete an ignored draft, so everything else is archived.
CACHE_DIRS = frozenset(
    {".lake", "lake-packages", ".cache", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".venv"}
)
# Reserved names used by crash-resumable retention deletion contain only generated artifacts.
CACHE_DIRS |= frozenset(name + ".retention-deleting" for name in CACHE_DIRS)
CACHE_TOP = frozenset({"build"})  # Lake's legacy top-level build directory
CACHE_TOP |= frozenset(name + ".retention-deleting" for name in CACHE_TOP)
CACHE_SUFFIXES = (".olean", ".ilean", ".pyc", ".pyo")
# Untracked paths that may carry credentials or runtime secrets; recorded by name only.
SECRET_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        ".credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".token")

# The reason for the most recent refusal, for callers that report a failure after a False return.
LAST_REFUSAL = ""


class RecordError(Exception):
    """Saving a recovery record failed; the checkout must be left intact."""


class HistoryLimit(RecordError):
    """Source remains intact when history/metadata exceeds the descriptor's bounded format."""


MAX_HISTORY_TIPS = 4096
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class BudgetExhausted(RecordError):
    """Housekeeping yielded; source and durable cleanup jobs remain retryable."""


@dataclass
class Work:
    seconds: float = float("inf")
    entries: int = 2**63
    bytes: int = 2**63
    started: float = field(default_factory=time.monotonic)
    metadata_entries: int = 0
    read_bytes: int = 0
    hash_bytes: int = 0
    removed_bytes: int = 0
    removal_bytes: int | None = None
    deadline: float = float("inf")
    adaptive: bool = False
    promoted: bool = False

    @property
    def read_remaining(self):
        return self.bytes - self.read_bytes - (self.removed_bytes if self.removal_bytes is None else 0)

    @property
    def removal_remaining(self):
        return (self.bytes - self.read_bytes if self.removal_bytes is None else self.removal_bytes) - self.removed_bytes

    def charge(self, *, entries=0, read=0, hashed=0, removed=0):
        if (
            time.monotonic() >= min(self.started + self.seconds, self.deadline)
            or self.metadata_entries + entries > self.entries
            or read > self.read_remaining
            or removed > self.removal_remaining
        ):
            raise BudgetExhausted("housekeeping budget; resume next preparation")
        self.metadata_entries += entries
        self.read_bytes += read
        self.hash_bytes += hashed
        self.removed_bytes += removed

    def report(self):
        return dict(
            seconds=time.monotonic() - self.started,
            metadata_entries=self.metadata_entries,
            read_bytes=self.read_bytes,
            hash_bytes=self.hash_bytes,
            removed_bytes=self.removed_bytes,
            limits=dict(
                seconds=None if self.seconds == float("inf") else self.seconds,
                entries=self.entries,
                read_bytes=self.bytes,
                removal_bytes=self.removal_bytes,
                promoted=self.promoted,
            ),
        )


_WORK = ContextVar("recovery_work", default=None)


def _charge(**fields):
    if work := _WORK.get():
        work.charge(**fields)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    _charge(entries=1)
    with path.open("rb") as handle:
        while True:
            _charge()
            # Charge before the read, including in bounded background work.
            size = min(65536, max(0, path.stat().st_size - handle.tell()))
            if not size:
                break
            _charge(read=size, hashed=size)
            data = handle.read(size)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


@dataclass
class Snapshot:
    """What a destructive preparation would lose, inspected without changing the checkout."""

    head: str | None
    branch: str | None
    base: str | None
    tips: dict[str, str]  # ref or pseudo-head name -> oid, for every local tip
    unpublished: list[str]  # commits not reachable from published refs
    unpreserved: list[str]  # of those, commits no durable record captured yet
    staged: bytes
    unstaged: bytes
    conflicted: list[str]
    flagged: list[str]  # assume-unchanged / skip-worktree paths, whose edits a diff would hide
    absent: list[str]  # conflicted or flagged paths deleted from the worktree
    untracked: list[tuple[str, str]]  # (relative path, kind) for files and links to archive
    excluded: list[tuple[str, str]]  # (relative path, reason)
    operations: list[str]  # merge/rebase/cherry-pick in progress
    fingerprint: str = ""
    archive_bytes: int = 0

    @property
    def dirty(self) -> bool:
        """Working-tree source a reset or clean would destroy."""
        return bool(
            self.staged or self.unstaged or self.conflicted or self.flagged or self.untracked or self.operations
        )

    @property
    def needs_record(self) -> bool:
        return self.dirty or bool(self.unpreserved)

    @property
    def published(self) -> bool:
        """Nothing a reset could lose: a clean tree and every local commit published."""
        return not self.dirty and not self.unpublished


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    record: Path | None = None
    state: str = "clean"
    details: dict = field(default_factory=dict)


def _git_environment(repo):
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "GIT_CEILING_DIRECTORIES": str(repo.parent.resolve()),
    }
    # Inspect this checkout even if the caller inherited an unrelated Git context.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(repo: Path, *args: str, check: bool = True, text: bool = False, timeout: int = 600):
    work = _WORK.get()
    if work is not None:
        work.charge(entries=1)
        timeout = min(timeout, max(0.001, work.seconds - (time.monotonic() - work.started)))
    command = ["git", "-C", str(repo), "-c", "core.quotepath=off", *args]
    env = _git_environment(repo)
    if work is not None and work.seconds != float("inf"):
        # Bound bytes at the pipe, rather than accounting for an arbitrarily large captured
        # rev-list/diff/ls-files output after it has already been read into memory.
        returncode, output, stderr = _bounded_git(command, env, work)
    else:
        p = subprocess.run(command, capture_output=True, timeout=timeout, env=env)
        output, stderr, returncode = p.stdout, p.stderr, p.returncode
        _charge(read=len(output) + len(stderr))
    if check and returncode:
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise RecordError(f"git {' '.join(args[:2])} failed: {detail[-1] if detail else f'exit {returncode}'}")
    return output.decode(errors="replace") if text else output


def _bounded_git(command, env, work):
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        output = {process.stdout: bytearray(), process.stderr: bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for stream in output:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    work.charge()
                    remaining = min(work.started + work.seconds, work.deadline) - time.monotonic()
                    for event, _ in selector.select(max(0, remaining)):
                        capacity = min(65536, work.read_remaining)
                        if capacity <= 0:
                            raise BudgetExhausted("git output byte budget; retry preservation")
                        data = os.read(event.fileobj.fileno(), capacity)
                        work.charge(read=len(data))
                        if data:
                            output[event.fileobj].extend(data)
                        else:
                            selector.unregister(event.fileobj)
            remaining = max(0.001, min(work.started + work.seconds, work.deadline) - time.monotonic())
            rc = process.wait(timeout=remaining)
            return rc, bytes(output[process.stdout]), bytes(output[process.stderr])
        except BaseException:
            # Only the Git subprocess started by this housekeeping operation, never a session.
            process.kill()
            process.wait()
            raise


def _rev(repo: Path, name: str) -> str | None:
    return _git(repo, "rev-parse", "-q", "--verify", f"{name}^{{commit}}", check=False, text=True).strip() or None


def _excluded(rel: str) -> str | None:
    parts = rel.rstrip("/").split("/")
    name = parts[-1]
    if any(part in CACHE_DIRS for part in parts) or parts[0] in CACHE_TOP or name.endswith(CACHE_SUFFIXES):
        return "dependency or build cache"
    if name in SECRET_NAMES or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES):
        return "possible credential or runtime secret"
    return None


def _split(raw: bytes) -> list[str]:
    return sorted(filter(None, raw.decode("utf-8", "surrogateescape").split("\0")))


def _untracked(repo: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Untracked entries, ignored ones included, never following a symlink. A nested repository or
    an ignored directory is walked, skipping caches."""
    keep: list[tuple[str, str]] = []
    excluded: list[tuple[str, str]] = []
    listed = _split(_git(repo, "ls-files", "-z", "--others", "--exclude-standard")) + _split(
        _git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    )
    for rel in sorted(set(listed)):
        _charge(entries=1)
        why = _excluded(rel)
        if why:
            excluded.append((rel.rstrip("/"), why))
            continue
        path = repo / rel
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue  # vanished since listing; nothing to lose
        if stat.S_ISDIR(st.st_mode):
            # `ls-files` reports a nested repository as `dir/`. Walk it without following links.
            for root, dirs, files in os.walk(path, followlinks=False):
                _charge(entries=1)
                dirs[:] = sorted(d for d in dirs if d not in CACHE_DIRS)
                for name in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                    _charge(entries=1)
                    sub = os.path.relpath(os.path.join(root, name), repo)
                    why = _excluded(sub)
                    if why:
                        excluded.append((sub, why))
                    else:
                        keep.append((sub, "link" if os.path.islink(os.path.join(root, name)) else "file"))
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        elif stat.S_ISLNK(st.st_mode):
            keep.append((rel, "link"))
        elif stat.S_ISREG(st.st_mode):
            keep.append((rel, "file"))
    return keep, excluded


def inspect(repo: Path, preserved: list[str] | None = None, *, history: bool = False) -> Snapshot:
    """Read-only inspection. Raises RecordError when the checkout cannot be read reliably.
    `preserved` names the complete records whose preserved refs may exempt commits from re-recording."""
    head = _rev(repo, "HEAD")
    branch = _git(repo, "symbolic-ref", "-q", "HEAD", check=False, text=True).strip() or None
    tips: dict[str, str] = {}
    if head:
        tips["HEAD"] = head
    for line in _git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", text=True).splitlines():
        oid, _, ref = line.partition(" ")
        tips[ref] = oid
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir", text=True).strip())
    operations = []
    for name in PSEUDO_HEADS:
        oid = _rev(repo, name) if (gitdir / name).exists() else None
        if oid:
            tips[name] = oid
            operations.append(name)
    operations += [d for d in ("rebase-merge", "rebase-apply") if (gitdir / d).exists()]
    if history:
        # Archive only history outside the source tips already covered by ordinary v1 records.
        # Deduplicate by oid so our own preservation-ref writes cannot change the snapshot.
        extra = set(
            _git(
                repo,
                "rev-list",
                "--all",
                "--reflog",
                "--not",
                "--remotes",
                f"--glob={PUBLISHED}",
                *sorted(set(tips.values())),
                text=True,
            ).split()
        )
        if extra:
            heads = set(_git(repo, "rev-list", "--no-walk", "--all", "--reflog", text=True).split())
            retained = extra & heads
            if len(retained) > MAX_HISTORY_TIPS:
                raise HistoryLimit("retained history exceeds bounded descriptor tip count")
            for oid in retained:
                tips[f"history/{oid}"] = oid
    base = _rev(repo, "refs/remotes/origin/" + os.environ.get("TAUCETI_BASE_BRANCH", "main")) or _rev(
        repo, "refs/remotes/origin/HEAD"
    )

    def outside(*exclusions: str) -> list[str]:
        if not tips:
            return []
        args = ["rev-list", *sorted(set(tips.values())), "--not", "--remotes", *exclusions]
        return _git(repo, *args, text=True).split()

    unpublished = outside(f"--glob={PUBLISHED}")
    globs = [f"--glob={PRESERVED}/{record_id}" for record_id in preserved or []]
    unpreserved = outside(f"--glob={PUBLISHED}", *globs) if unpublished else []

    conflicted = _split(_git(repo, "diff", "--name-only", "-z", "--diff-filter=U"))
    # `ls-files -v` tags assume-unchanged entries in lowercase and skip-worktree entries with S.
    flagged = sorted(
        entry[2:] for entry in _split(_git(repo, "ls-files", "-v", "-z")) if entry[:1].islower() or entry[:1] == "S"
    )
    diff = ["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    staged = b"".join(
        line
        for line in _git(repo, *diff, "--cached").splitlines(keepends=True)
        if not line.startswith(b"* Unmerged path ")
    )
    excludes = [f":(exclude,literal){p}" for p in conflicted]
    unstaged = _git(repo, *diff, "--", ".", *excludes)
    untracked, excluded = _untracked(repo)
    # Conflicted and flagged paths are saved as bytes; deletion of one is recorded explicitly.
    absent = []
    for p in sorted(set(conflicted) | set(flagged)):
        if (repo / p).is_symlink():
            untracked.append((p, "link"))
        elif (repo / p).is_file():
            untracked.append((p, "file"))
        elif not (repo / p).exists():
            absent.append(p)

    digest = hashlib.sha256()
    archive_bytes = 0
    for rel, kind in untracked:
        _charge(entries=1)
        path = repo / rel
        digest.update(f"{rel}\0{kind}\0".encode("utf-8", "surrogateescape"))
        try:
            if kind == "link":
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
            else:
                st = os.lstat(path)
                archive_bytes += st.st_size
                digest.update(f"{st.st_mode & 0o777}\0".encode())
                if archive_bytes <= MAX_UNTRACKED_BYTES:
                    digest.update(bytes.fromhex(_digest(path)))
        except OSError as error:
            raise RecordError(f"could not read untracked {rel}: {error}") from None
    state = dict(
        head=head,
        branch=branch,
        tips=tips,
        unpublished=unpublished,
        operations=operations,
        staged=hashlib.sha256(staged).hexdigest(),
        unstaged=hashlib.sha256(unstaged).hexdigest(),
        conflicted=conflicted,
        flagged=flagged,
        absent=absent,
        untracked=digest.hexdigest(),
    )
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return Snapshot(
        head=head,
        branch=branch,
        base=base,
        tips=tips,
        unpublished=unpublished,
        unpreserved=unpreserved,
        staged=staged,
        unstaged=unstaged,
        conflicted=conflicted,
        flagged=flagged,
        absent=absent,
        untracked=untracked,
        excluded=excluded,
        operations=operations,
        fingerprint=fingerprint,
        archive_bytes=archive_bytes,
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _archive(repo: Path, entries: list[tuple[str, str]], dest: Path) -> None:
    """Tar the entries with their original relative paths. Links are stored as links and never
    read through; a regular file is opened O_NOFOLLOW so a swapped-in link fails the save."""
    if len(entries) > MAX_UNTRACKED_FILES:
        raise RecordError(f"{len(entries)} untracked files exceed the {MAX_UNTRACKED_FILES}-file archive bound")
    total = 0
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel, kind in entries:
                path = repo / rel
                st = os.lstat(path)
                info = tarfile.TarInfo(rel)
                info.mtime = int(st.st_mtime)
                info.mode = st.st_mode & 0o7777
                if kind == "link":
                    if not stat.S_ISLNK(st.st_mode):
                        raise RecordError(f"untracked {rel} changed type during the save")
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(path)
                    tar.addfile(info)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise RecordError(f"untracked {rel} changed type during the save")
                total += st.st_size
                if total > MAX_UNTRACKED_BYTES:
                    raise RecordError(
                        f"untracked source exceeds the {MAX_UNTRACKED_BYTES // (1024 * 1024)} MiB archive bound "
                        f"(at {rel}); move large non-source files out of the checkout"
                    )
                src = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(src, "rb") as handle:
                    _charge(entries=1, read=st.st_size)
                    data = handle.read(st.st_size + 1)
                if len(data) != st.st_size:
                    raise RecordError(f"untracked {rel} changed size during the save")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        out.flush()
        os.fsync(out.fileno())


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".-")[:80] or "unknown"


REQUIRED = ("id", "checkout", "fingerprint", "files", "bundle_refs", "local_head")
PAYLOADS = ("staged.patch", "unstaged.patch", "untracked.tar")


def _read_manifest(record: Path) -> dict | None:
    try:
        size = (record / "manifest.json").stat().st_size
        if size > MAX_MANIFEST_BYTES:
            return None
        _charge(entries=1, read=size)
    except OSError:
        return None
    try:
        value = read_json(record / "manifest.json")
    except RecursionError:
        return None
    if value.get("schema") != SCHEMA or any(key not in value for key in REQUIRED):
        return None
    if (
        not isinstance(value["checkout"], str)
        or not isinstance(value["fingerprint"], str)
        or not isinstance(value["bundle_refs"], dict)
        or (value["local_head"] is not None and not isinstance(value["local_head"], str))
    ):
        return None
    files = value["files"]
    needed = PAYLOADS + (("commits.bundle",) if value["bundle_refs"] else ())
    if (
        not isinstance(files, dict)
        or any(name not in files for name in needed)
        or not set(files) <= set(PAYLOADS) | {"commits.bundle"}
        or any(not isinstance(meta, dict) for meta in files.values())
    ):
        return None
    return value


def _complete(record: Path, *, digests: bool = True) -> dict | None:
    """The manifest, if every payload it names is present with the recorded size (and digest)."""
    manifest = _read_manifest(record)
    if manifest is None:
        return None
    for name, meta in manifest["files"].items():
        path = record / name
        try:
            st = path.lstat()
            _charge(entries=1)
            if not stat.S_ISREG(st.st_mode) or st.st_size != meta.get("bytes"):
                return None
            if digests and _digest(path) != meta.get("sha256"):
                return None
        except OSError:
            return None
    return manifest


def _sync_record(record: Path) -> None:
    """Re-establish durability of a reused record: its files, its directory, and the directory chain."""
    manifest = _read_manifest(record)
    if manifest is None:
        raise OSError("invalid recovery descriptor")
    for name in ("manifest.json", *manifest["files"]):
        _charge(entries=1)
        with open(record / name, "rb") as handle:
            os.fsync(handle.fileno())
    _fsync_dir(record)
    _fsync_dir(record.parent)
    _fsync_dir(record.parent.parent)


def owner_path(cfg, label: str = "TauCeti") -> Path:
    return cfg.state / "checkout-owners" / f"{label}.json"


def process_owner():
    """Process birth attribution without requiring PID namespace support."""
    import sys

    from .usage_lifecycle import identity

    if sys.platform != "linux":
        return identity()
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    owner = dict(pid=os.getpid(), start=fields[19], boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    if os.getsid(0) == os.getpid():
        owner["session"] = os.getpid()  # ordinary dedicated round session, when one exists
    return owner


def owner_stopped(owner):
    """Unknown/unreadable owners stay queued; only this candidate is withheld."""
    import sys
    import uuid

    if not isinstance(owner, dict) or not {"pid", "start", "boot"} <= owner.keys():
        return False
    if sys.platform != "linux":
        from .usage_lifecycle import alive

        return not alive(owner)
    if not isinstance(owner["start"], str) or not owner["start"].isdigit() or not isinstance(owner["boot"], str):
        return False
    try:
        uuid.UUID(owner["boot"])
        pid = int(owner["pid"])
        if pid <= 0:
            return False
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot != owner["boot"]:
            return True
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] != owner["start"] or fields[0] in {"Z", "X"}
    except (ProcessLookupError, FileNotFoundError):
        # A missing boot-id file is unavailable ownership evidence, rather than a dead PID.
        return Path("/proc/sys/kernel/random/boot_id").is_file() and not Path(f"/proc/{pid}").exists()
    except (OSError, ValueError, TypeError, IndexError):
        return False


def note_owner(cfg, label: str = "TauCeti", **fields) -> bool:
    """Record which round now owns the checkout, so a later record names the round that left the source.
    Attribution only: a failure is logged and never stops a round."""
    from .activity import current_round_id

    path = owner_path(cfg, label)
    fields["round_id"] = fields.get("round_id") or current_round_id()
    previous = read_json(path)
    # A later note from the same round (the PR head it checked out) extends the first one.
    same = fields["round_id"] is not None and previous.get("round_id") == fields["round_id"]
    value = previous if same else {}
    value.update({k: v for k, v in fields.items() if v is not None})
    if not same:
        value["round_id"] = fields["round_id"]  # None replaces an earlier round's identity
        value["claimed_at"] = time.time()
    with contextlib.suppress(OSError, ValueError, IndexError):
        value["process"] = process_owner()
    value["worker"] = getattr(cfg, "wid", None)
    try:
        atomic_json(path, value)
        _fsync_dir(path.parent)
    except OSError as error:
        log(f"recovery: could not record checkout owner ({error})")
        return False
    return True


def mark_published(repo: Path, name: str, oid: str) -> None:
    """Record a remote head this worker observed, so its commits are known published without GitHub."""
    subprocess.run(["git", "-C", str(repo), "update-ref", f"{PUBLISHED}/{name}", oid], capture_output=True, check=False)


def _commit_key(snap):
    # A clean checkout may move HEAD while the same local-only branch tips remain. Their
    # completed bundle is reusable independently of the whole working-tree fingerprint.
    return "commits:" + hashlib.sha256("\0".join(sorted(snap.unpublished)).encode()).hexdigest()


def _indexed_record(state, repository, fingerprint, *, source=True, snapshot=None):
    cached = recovery_index.lookup(state, repository, fingerprint)
    if not cached or (not source and cached.get("verified") is not True):
        return None
    record = state / "recovery" / cached["record"]
    try:
        manifest = _read_manifest(record)
        if manifest is None or cached.get("manifest_checksum") != recovery_index.checksum(manifest):
            return None
        if snapshot is not None and (
            any(
                manifest.get(key) != getattr(snapshot, attribute)
                for key, attribute in (
                    ("local_head", "head"),
                    ("branch", "branch"),
                    ("tips", "tips"),
                    ("operations", "operations"),
                    ("conflicted", "conflicted"),
                    ("flagged", "flagged"),
                    ("absent", "absent"),
                )
            )
            or manifest.get("untracked") != [path for path, _ in snapshot.untracked]
            or manifest["files"]["staged.patch"].get("sha256") != hashlib.sha256(snapshot.staged).hexdigest()
            or manifest["files"]["unstaged.patch"].get("sha256") != hashlib.sha256(snapshot.unstaged).hexdigest()
        ):
            return None
        before = recovery_index.stamps(record, manifest)
        _charge(entries=len(before))
        trusted = cached.get("verified") is True and cached.get("evidence") == before
        if (
            manifest
            and manifest["fingerprint"] == (fingerprint if source else cached.get("source_fingerprint"))
            and manifest.get("repository", "") == repository
            and (trusted or _complete(record) is not None)
        ):
            after = recovery_index.stamps(record, manifest)
            if before == after:
                if not trusted:
                    _sync_record(record)
                    with contextlib.suppress(OSError):
                        recovery_index.remember(
                            state, record, manifest, verified=True, evidence=after, fingerprint=fingerprint
                        )
                return record
    except (OSError, ValueError, TypeError, RecursionError):
        pass  # invalidated evidence requires exact validation or a fresh source save
    return None


def _remember_commits(state, record, manifest, snap):
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
        if not snap.unpublished:
            return
        fingerprint = _commit_key(snap)
        previous = recovery_index.lookup(state, manifest["repository"], fingerprint)
        evidence = recovery_index.stamps(record, manifest)
        if (
            previous.get("verified") is True
            and previous.get("record") == record.name
            and previous.get("evidence") == evidence
        ):
            return
        with contextlib.suppress(OSError, ValueError):
            recovery_index.remember(
                state,
                record,
                manifest,
                verified=True,
                evidence=evidence,
                fingerprint=fingerprint,
            )


def ref_object(repo: Path, ref: str) -> str | None:
    """Read an intent's object identity without peeling its annotated tag to source."""
    return _git(repo, "rev-parse", "--verify", ref, check=False, text=True).strip() or None


def pending_push_ref(marker: dict) -> str:
    branch = marker["ref"].removeprefix("refs/tauceti/published/push/")
    return f"refs/tauceti/pending-push/{marker.get('scope') or 'unscoped'}/{branch}"


def _record(cfg, repo: Path, snap: Snapshot, label: str, repository: str, *, owner: dict | None = None) -> Path:
    """Write or reuse a complete record for this exact snapshot. Raises RecordError on any failure."""
    from .activity import current_round_id

    root = cfg.state / "recovery"
    owner = read_json(owner_path(cfg, label)) if owner is None else owner
    marker = owner.get("publication_marker") or {}
    outcome = owner.get("round_outcome")
    if marker.get("ref"):
        published = _rev(repo, marker["ref"])
        if published and published != marker.get("before"):
            outcome = {**(outcome or {}), "published_head": published}
        pending_ref = pending_push_ref(marker)
        pending = ref_object(repo, pending_ref)
        if pending:
            message = _git(repo, "cat-file", "-p", pending, text=True).split("\n\n", 1)[-1].splitlines()
            auth = owner.get("authorization") or {}
            outcome = {
                **(outcome or {}),
                "pending_push": dict(
                    oid=_rev(repo, pending_ref),
                    expected=message[1] if len(message) > 1 else None,
                    repository=auth.get("repository"),
                    ref=auth.get("ref"),
                ),
            }
    source_round = owner.get("round_id") or current_round_id() or f"unattributed-{int(time.time())}"
    base_id = _safe_id(str(source_round))
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _fsync_dir(root)
        _fsync_dir(root.parent)
    except OSError as error:
        raise RecordError(f"could not create {root}: {error}") from None
    indexed = os.environ.get("TAUCETI_WORK_RECOVERY", "").lower() != "off"
    if indexed and (record := _indexed_record(cfg.state, repository, snap.fingerprint, snapshot=snap)):
        if (outcome or {}).get("pending_push"):
            journal = read_json(record / "recovery.json")
            atomic_json(record / "recovery.json", {**journal, "normal_publication": outcome["pending_push"]})
            _fsync_dir(record)
        if snap.unpublished:
            _remember_commits(cfg.state, record, _read_manifest(record), snap)
        manifest = _read_manifest(record)
        if manifest.get("source_round"):
            with contextlib.suppress(OSError, ValueError):
                recovery_index.remember(
                    cfg.state,
                    record,
                    manifest,
                    verified=True,
                    evidence=recovery_index.stamps(record, manifest),
                    fingerprint=f"round:{manifest['source_round']}",
                )
        return record
    record_id = base_id
    if (root / record_id).exists():
        record_id = f"{base_id}.{secrets.token_hex(8)}"
    tmp = root / f".tmp-{record_id}-{os.getpid()}-{secrets.token_hex(4)}"
    pending = f"{PENDING}/{tmp.name.lstrip('.')}"
    bundle_refs: dict[str, str] = {}
    try:
        tmp.mkdir(mode=0o700)
        unpublished = set(snap.unpublished)
        bundle_refs = {name: oid for name, oid in sorted(snap.tips.items()) if oid in unpublished}
        _update_refs(repo, [f"update {pending}/{name} {oid}" for name, oid in bundle_refs.items()])
        if bundle_refs:
            _git(
                repo,
                "bundle",
                "create",
                "-q",
                str(tmp / "commits.bundle"),
                f"--glob={pending}",
                "--not",
                "--remotes",
                f"--glob={PUBLISHED}",
            )
            with open(tmp / "commits.bundle", "rb+") as handle:
                os.fsync(handle.fileno())
            _git(repo, "bundle", "verify", "-q", str(tmp / "commits.bundle"))
        _write(tmp / "staged.patch", snap.staged)
        _write(tmp / "unstaged.patch", snap.unstaged)
        _archive(repo, snap.untracked, tmp / "untracked.tar")
        files = {}
        for path in sorted(tmp.iterdir()):
            files[path.name] = dict(bytes=path.stat().st_size, sha256=_digest(path))
        manifest = dict(
            schema=SCHEMA,
            id=record_id,
            worker=getattr(cfg, "wid", None),
            repository=repository,
            checkout=str(repo),
            created_at=time.time(),
            recorded_by_round=current_round_id(),
            source_round=owner.get("round_id"),
            work=owner.get("work"),
            roadmap=owner.get("roadmap"),
            pr=owner.get("pr"),
            expected_remote_head=owner.get("expected_head"),
            local_head=snap.head,
            branch=snap.branch,
            base=snap.base,
            tips=snap.tips,
            bundle_refs={name: f"{pending}/{name}" for name in bundle_refs},
            operations=snap.operations,
            fingerprint=snap.fingerprint,
            publication=dict(
                state="unpublished" if snap.unpublished or snap.dirty else "published",
                unpublished_commits=len(snap.unpublished),
                dirty=snap.dirty,
                basis=f"remote-tracking refs and {PUBLISHED}/*; no GitHub read",
            ),
            conflicted=snap.conflicted,
            flagged=snap.flagged,
            absent=snap.absent,
            untracked=[p for p, _ in snap.untracked],
            excluded=[dict(path=p, reason=r) for p, r in snap.excluded],
            files=files,
            authorization=owner.get("authorization"),
            round_outcome=outcome,
            conversation=read_json(Path(owner["conversation_path"])) if owner.get("conversation_path") else None,
        )
        encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise HistoryLimit("preservation metadata exceeds bounded descriptor size")
        _write(tmp / "manifest.json", encoded)
        _fsync_dir(tmp)
        final = root / record_id
        os.rename(tmp, final)
        _fsync_dir(root)
    except (OSError, subprocess.SubprocessError, tarfile.TarError, RecordError) as error:
        # Only our own partial copy is removed; the checkout itself is untouched.
        shutil.rmtree(tmp, ignore_errors=True)
        _update_refs(repo, [f"delete {pending}/{name}" for name in bundle_refs], check=False)
        if isinstance(error, RecordError):
            raise
        raise RecordError(f"could not save recovery record: {error}") from error
    # The descriptor is durable before the disposable index is published. Failure/crash here is
    # safe: a later lookup misses and saves again, or housekeeping reconstructs an unverified entry.
    if indexed:
        with contextlib.suppress(OSError, ValueError):
            recovery_index.remember(
                cfg.state, final, manifest, verified=True, evidence=recovery_index.stamps(final, manifest)
            )
        _remember_commits(cfg.state, final, manifest, snap)
    # Durable: the bundle now preserves these commits, so later inspections need not re-record them.
    _update_refs(
        repo,
        [f"update {PRESERVED}/{record_id}/{name} {oid}" for name, oid in bundle_refs.items()]
        + [f"delete {pending}/{name}" for name in bundle_refs],
        check=False,
    )
    return final


def _update_refs(repo: Path, commands: list[str], *, check: bool = True) -> None:
    if not commands:
        return
    p = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "--stdin"],
        input="".join(c + "\n" for c in commands).encode(),
        capture_output=True,
        env=_git_environment(repo),
    )
    if check and p.returncode:
        raise RecordError(f"git update-ref failed: {p.stderr.decode(errors='replace').strip()[-200:]}")


def _set_status(record: Path, state: str, reason: str = "") -> bool:
    try:
        atomic_json(record / "status.json", dict(state=state, reason=reason, updated_at=time.time()))
        _fsync_dir(record)
    except OSError as error:
        log(f"recovery: could not update {record.name} status ({error})")
        return False
    return True


def live_users(repo: Path, *, session=None) -> list[str]:
    """Processes whose cwd, root, or open files lie inside the checkout (Linux /proc; read-only).
    Detached descendants without a live reference are outside this best-effort observation."""
    proc = Path("/proc")
    if not (proc / "self").exists():
        return []
    target = os.path.realpath(repo)
    prefix = target.rstrip("/") + "/"
    skip = set()
    pid = os.getpid()
    while pid > 1:  # never report ourselves or the supervisors that started this round
        skip.add(pid)
        try:
            pid = int((proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    found = []
    for entry in proc.iterdir():
        _charge()  # deadline also bounds ownership observation during housekeeping
        if not entry.name.isdigit():
            continue
        if session is not None:
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[3]) == session and fields[0] not in {"Z", "X"}:
                    found.append(f"{entry.name} (owner session)")
                    continue
            except (OSError, ValueError, IndexError):
                pass
        if int(entry.name) in skip:
            continue
        links = [entry / "cwd", entry / "root"]
        with contextlib.suppress(OSError):
            links += list((entry / "fd").iterdir())
        for link in links:
            _charge()
            try:
                where = os.readlink(link)
            except OSError:
                continue
            if where == target or where.startswith(prefix):
                try:
                    found.append(f"{entry.name} ({(entry / 'comm').read_text().strip()})")
                except OSError:
                    found.append(entry.name)
                break
    return found


# Next to the checkout, so moving aside is a same-volume rename. Not `retained`: progress parks
# publication bundles in state/<worker>/progress/retained, beside the roadmap clone.
RETAINED_DIR = "retained-checkouts"


def _retain(
    cfg, repo: Path, reason: str, record: Path | None, label: str, *, repository: str | None = None
) -> Decision:
    """Move the whole checkout aside, unmodified, so preparation can continue on a fresh one.

    A same-directory rename needs no free space and leaves every byte (and any process still
    working there) exactly where it was. The retained copy is saved and removed automatically by
    background activity housekeeping after ordinary ownership and live-reference checks permit it."""
    global LAST_REFUSAL
    parent = repo.parent / RETAINED_DIR
    dest = parent / f"{repo.name}-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    from .recovery_maintenance import register

    repository = repository or (ROADMAP if label == "progress-roadmap" else TAUCETI)
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A same-volume rename must still work when the state volume is full/unwritable.
        # Lost registration is rediscovered from known roots; lost attribution withholds cleanup.
        with contextlib.suppress(OSError):
            register(cfg.state, parent, label, repository=repository)
        owner = read_json(owner_path(cfg, label))
        owner.update(repository=repository, label=label)
        with contextlib.suppress(OSError):
            atomic_json(dest.with_name(dest.name + ".owner.json"), owner)
            _fsync_dir(parent)
        os.rename(repo, dest)
    except OSError as error:
        if repo.exists() and not dest.exists():
            with contextlib.suppress(OSError):
                dest.with_name(dest.name + ".owner.json").unlink(missing_ok=True)
        # Neither saved nor movable: leave everything untouched; the next round tries again.
        LAST_REFUSAL = f"{reason}, and moving {repo} aside failed ({error}); the checkout is left intact"
        return Decision(False, LAST_REFUSAL, record, "refused")
    with contextlib.suppress(OSError):  # durability and attribution are best effort here
        _fsync_dir(parent)
        _fsync_dir(repo.parent)
    if record is not None:
        _set_status(record, "retained", f"{reason}; checkout moved to {dest}")
    log(f"recovery: {reason}; moved the checkout to {dest} and continuing with a fresh one")
    return Decision(True, reason, record, "retained", dict(retained=str(dest)))


def retain(cfg, repo: Path, reason: str, *, label: str = "TauCeti") -> Decision:
    """Move an unusable checkout (a stale lock, damaged metadata, an interrupted clone) aside whole."""
    return _retain(cfg, repo, reason, None, label)


def save_for_cleanup(cfg, repo: Path, *, owner=None, dry_run=False) -> Decision:
    """Save source without moving/resetting a tree; a collector can still evict caches on failure."""
    record = None
    try:
        if not (repo / ".git").is_dir() or (repo / ".git").is_symlink():
            return Decision(False, "not a standalone Git checkout; retaining source", state="protected")
        snap = inspect(repo, history=True)
        if len(snap.untracked) > MAX_UNTRACKED_FILES or snap.archive_bytes > MAX_UNTRACKED_BYTES:
            return Decision(
                False, "source exceeds the existing recovery archive bounds; retaining checkout", state="protected"
            )
        if snap.needs_record and not dry_run:
            record = _record(cfg, repo, snap, "TauCeti", TAUCETI, owner=owner)
        if any("credential" in reason for _, reason in snap.excluded):
            return Decision(False, "credential-like files require retaining the checkout", record, "protected")
        if _git(
            repo, "for-each-ref", "--format=%(refname)", "refs/stash", "refs/tags", "refs/notes", text=True
        ).strip():
            return Decision(False, "additional Git refs require retaining the checkout", record, "protected")
        preserved = [f"--glob={PRESERVED}/{record.name}"] if record else []
        if _git(
            repo,
            "rev-list",
            "--reflog",
            "--all",
            "--not",
            "--branches",
            "--remotes",
            f"--glob={PUBLISHED}",
            *preserved,
            text=True,
        ).strip():
            return Decision(False, "reflog-only commits require retaining the checkout", record, "protected")
        if live_users(repo) or inspect(repo, history=True).fingerprint != snap.fingerprint:
            return Decision(False, "checkout changed or acquired a live user during preservation", record)
        return Decision(True, record=record, state="preserved")
    except (OSError, subprocess.SubprocessError, RecordError) as error:
        return Decision(False, f"could not preserve source: {error}", record)


def _bundle_prerequisites(bundle):
    """Read only the bounded text header, after exact validation of the bundle payload."""
    prerequisites = []
    with bundle.open("rb") as handle:
        while True:
            line = handle.readline(4097)
            _charge(read=len(line))
            if len(line) > 4096 or not line:
                raise RecordError("invalid bundle header")
            if line == b"\n":
                break
            if line.startswith(b"-"):
                oid = line[1:].split(b" ", 1)[0].decode("ascii")
                if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
                    raise RecordError("invalid bundle prerequisite")
                prerequisites.append(oid)
            if handle.tell() > MAX_MANIFEST_BYTES:
                raise RecordError("oversized bundle header")
    return prerequisites


def _rollback_commits(cfg, repo, snap, repository):
    """Index-free fallback: validate one bundle named by this checkout's own preserved refs.

    Only current Git-ref metadata is enumerated, never historic descriptor directories/trees.
    A corrupt/missing candidate causes a fresh save; no other historical payload is hashed.
    """
    unpublished = set(snap.unpublished)
    required = {oid for oid in snap.tips.values() if oid in unpublished}
    if not required:
        return None
    groups = {}
    prefix = PRESERVED + "/"
    for line in _git(repo, "for-each-ref", "--format=%(objectname) %(refname)", PRESERVED, text=True).splitlines():
        oid, _, ref = line.partition(" ")
        if not ref.startswith(prefix):
            continue
        record_id, separator, _ = ref[len(prefix) :].partition("/")
        if separator and record_id and not record_id.startswith("."):
            groups.setdefault(record_id, set()).add(oid)
    selected, newest = None, -1
    for record_id, oids in groups.items():
        if not required <= oids:
            continue
        record = cfg.state / "recovery" / record_id
        try:
            _charge(entries=1)
            modified = record.stat().st_mtime_ns
            if modified > newest:
                selected, newest = record, modified
        except OSError:
            continue
    if selected is None:
        return None
    try:
        manifest = _complete(selected)
        if manifest is None or manifest.get("repository") != repository or not manifest["bundle_refs"]:
            return None
        prerequisites = _bundle_prerequisites(selected / "commits.bundle")
        if (
            prerequisites
            and _git(repo, "rev-list", "--no-walk", *prerequisites, "--not", "--remotes", f"--glob={PUBLISHED}").strip()
        ):
            return None  # a former published base must now be included in a fresh bundle
        _sync_record(selected)
        heads = {
            line.split()[0]
            for line in _git(repo, "bundle", "list-heads", str(selected / "commits.bundle"), text=True).splitlines()
        }
        return selected if required <= heads else None
    except (OSError, RecordError, ValueError, TypeError, UnicodeError):
        return None


def _guard(cfg, repo: Path, *, label: str = "TauCeti", repository: str = TAUCETI) -> Decision:
    """Save anything unpublished in `repo`, then let preparation continue. Never waits for a human.

    The normal outcome is a durable record followed by a reset in place, including after an OOM or
    SIGKILL. Every reset is checked against a final inspection, so a write that lands after the
    snapshot is saved too. When a process still uses the checkout, the save fails, or the checkout
    will not hold still, the whole checkout is moved aside instead (`state == "retained"`) and the
    caller creates a fresh one. Refusal is left only for a checkout that can be neither saved nor
    moved, and the next round simply retries."""
    global LAST_REFUSAL
    LAST_REFUSAL = ""
    indexed = os.environ.get("TAUCETI_WORK_RECOVERY", "").lower() != "off"
    record = None
    for _ in range(3):
        try:
            snap = inspect(repo)
        except (OSError, subprocess.SubprocessError, RecordError) as error:
            return _retain(cfg, repo, f"could not inspect {repo} ({error})", record, label, repository=repository)
        if snap.published:
            users = live_users(repo)
            if users:  # nothing to lose now, but a live writer could still create some
                return _retain(
                    cfg,
                    repo,
                    "processes still use the checkout: " + ", ".join(users[:8]),
                    None,
                    label,
                    repository=repository,
                )
            return Decision(True, state="clean")
        committed = None
        if snap.unpublished and not snap.dirty:
            committed = (
                _indexed_record(cfg.state, repository, _commit_key(snap), source=False)
                if indexed
                else _rollback_commits(cfg, repo, snap, repository)
            )
        if committed is not None:
            record = committed  # still run the ordinary live-user and final-source rechecks below
        elif snap.needs_record:
            try:
                record = _record(cfg, repo, snap, label, repository)
            except RecordError as error:
                return _retain(cfg, repo, f"could not save its source ({error})", record, label, repository=repository)
        users = live_users(repo)
        if users:
            return _retain(
                cfg,
                repo,
                "processes still use the checkout: " + ", ".join(users[:8]),
                record,
                label,
                repository=repository,
            )
        try:
            again = inspect(repo)
        except (OSError, subprocess.SubprocessError, RecordError) as error:
            return _retain(cfg, repo, f"could not re-inspect {repo} ({error})", record, label, repository=repository)
        if again.fingerprint != snap.fingerprint:
            continue  # a late write: save the newer state before resetting
        state = "checkout-reset" if snap.dirty else "preserved"
        if record is not None:
            _set_status(record, state, "saved before the checkout was reset")
            what = "unfinished source" if snap.dirty else f"{len(snap.unpublished)} unpublished commit(s)"
            log(f"recovery: saved {what} in {record}")
        return Decision(True, record=record, state=state)
    return _retain(
        cfg, repo, "the checkout kept changing while it was being saved", record, label, repository=repository
    )


def guard(cfg, repo: Path, *, label: str = "TauCeti", repository: str = TAUCETI) -> Decision:
    """Foreground preparation measures only the current candidate, never retained history."""
    work = Work()
    token = _WORK.set(work)
    try:
        decision = _guard(cfg, repo, label=label, repository=repository)
        decision.details["preparation"] = work.report()
        with contextlib.suppress(OSError):
            atomic_json(cfg.state / "recovery-preparation.json", dict(at=time.time(), **work.report()))
        return decision
    finally:
        _WORK.reset(token)


def restore(record: Path, repo: Path) -> None:
    """Reconstruct a record's source into `repo`, a clone with the published history available."""
    manifest = _complete(record)
    if manifest is None:
        raise RecordError(f"{record} is not a complete {SCHEMA} record")
    if (record / "commits.bundle").exists():
        _git(repo, "fetch", "-q", str(record / "commits.bundle"), f"{PENDING}/*:{PENDING}/*")
    if manifest.get("local_head"):
        _git(repo, "checkout", "-q", "--detach", manifest["local_head"])
    for name, option in (("staged.patch", "--index"), ("unstaged.patch", None)):
        if (record / name).stat().st_size:
            _git(repo, "apply", "--binary", *([option] if option else []), str(record / name))
    with tarfile.open(record / "untracked.tar") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or any(p in ("..", ".git") for p in Path(member.name).parts):
                raise RecordError(f"unsafe archive member {member.name}")
            dest = repo / member.name
            if any(parent.is_symlink() for parent in dest.parents if parent != repo and repo in parent.parents):
                raise RecordError(f"archive member traverses a symlink: {member.name}")
            if not (member.isfile() or member.issym()):
                raise RecordError(f"unsupported archive member {member.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            if member.issym():
                os.symlink(member.linkname, dest)
            else:
                dest.write_bytes(tar.extractfile(member).read())
                os.chmod(dest, member.mode & 0o777)
    for rel in manifest.get("absent", []):
        with contextlib.suppress(FileNotFoundError):
            (repo / rel).unlink()


def records(state_root: Path | None = None, *, workers=None):
    """Yield (worker, record dir or None for an incomplete save, manifest or None, status) locally."""
    base = state_root or HERE / "state"
    with contextlib.suppress(OSError):
        for worker in sorted(p for p in base.iterdir() if p.is_dir()):
            if workers is not None and worker.name not in workers:
                continue
            root = worker / "recovery"
            if not root.is_dir():
                continue
            for record in sorted(root.iterdir()):
                if not record.is_dir():
                    continue
                if record.name.startswith(".tmp-"):
                    yield worker.name, None, None, {"state": "incomplete"}
                    continue
                yield (
                    worker.name,
                    record,
                    _complete(record, digests=False),
                    (read_json(record / "recovery.json") or read_json(record / "status.json")),
                )


def describe_reason(value):
    if isinstance(value, dict):
        return str(value.get("detail") or value.get("code") or "") + (
            f"; next action: {value['next_action']}" if value.get("next_action") else ""
        )
    return str(value)


def housekeep(state_root: Path | None = None, **budgets) -> dict:
    from .recovery_maintenance import prepare

    return prepare(state_root or _state_root(), **budgets)


def summary(state_root: Path | None = None, *, workers=None) -> dict:
    """Only prepared local receipts; no inventory traversal or payload reads."""
    from .recovery_maintenance import cached_summary

    return cached_summary(state_root or _state_root(), workers=workers)


def _state_root() -> Path:
    from .config import legacy_project, worker_state_root

    return HERE / "state" if legacy_project() else worker_state_root()

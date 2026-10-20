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
is moved aside unmodified and preparation continues on a fresh clone; a later preparation saves and
removes the retained copy once nothing uses it. A clean checkout with nothing unpublished needs no
record at all.

"Published" is decided locally, never from GitHub: a commit is published when it is reachable from a
remote-tracking ref or from refs/tauceti/published/*, which git-safe-push writes after a successful
push and the worker writes for every remote head it checks out. Commits already captured by a durable
record are excluded through refs/tauceti/preserved/*, so the same commits are bundled once.

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
import shutil
import stat
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import log
from .constants import TAUCETI
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
CACHE_TOP = frozenset({"build"})  # Lake's legacy top-level build directory
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


def _git(repo: Path, *args: str, check: bool = True, text: bool = False, timeout: int = 600):
    p = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
    )
    if check and p.returncode:
        detail = p.stderr.decode(errors="replace").strip().splitlines()
        raise RecordError(f"git {' '.join(args[:2])} failed: {detail[-1] if detail else f'exit {p.returncode}'}")
    return p.stdout.decode(errors="replace") if text else p.stdout


def _rev(repo: Path, name: str) -> str | None:
    p = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"{name}^{{commit}}"], capture_output=True, text=True
    )
    return p.stdout.strip() or None if p.returncode == 0 else None


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
                dirs[:] = sorted(d for d in dirs if d not in CACHE_DIRS)
                for name in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
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


def inspect(repo: Path, preserved: list[str] | None = None) -> Snapshot:
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
    base = _rev(repo, "refs/remotes/origin/main") or _rev(repo, "refs/remotes/origin/HEAD")

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
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
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
                    data = handle.read()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        out.flush()
        os.fsync(out.fileno())


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".-")[:80] or "unknown"


REQUIRED = ("id", "checkout", "fingerprint", "files", "bundle_refs", "local_head")
PAYLOADS = ("staged.patch", "unstaged.patch", "untracked.tar")


def _read_manifest(record: Path) -> dict | None:
    value = read_json(record / "manifest.json")
    if value.get("schema") != SCHEMA or any(key not in value for key in REQUIRED):
        return None
    files = value["files"]
    needed = PAYLOADS + (("commits.bundle",) if value["bundle_refs"] else ())
    if not isinstance(files, dict) or any(name not in files for name in needed):
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
            if path.stat().st_size != meta.get("bytes"):
                return None
            if digests and hashlib.sha256(path.read_bytes()).hexdigest() != meta.get("sha256"):
                return None
        except OSError:
            return None
    return manifest


def _sync_record(record: Path) -> None:
    """Re-establish durability of a reused record: its files, its directory, and the directory chain."""
    for path in record.iterdir():
        if path.is_file():
            with open(path, "rb") as handle:
                os.fsync(handle.fileno())
    _fsync_dir(record)
    _fsync_dir(record.parent)
    _fsync_dir(record.parent.parent)


def owner_path(cfg, label: str = "TauCeti") -> Path:
    return cfg.state / "checkout-owners" / f"{label}.json"


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


def _record(cfg, repo: Path, snap: Snapshot, label: str, repository: str, *, owner: dict | None = None) -> Path:
    """Write or reuse a complete record for this exact snapshot. Raises RecordError on any failure."""
    from .activity import current_round_id

    root = cfg.state / "recovery"
    owner = owner or read_json(owner_path(cfg, label))
    source_round = owner.get("round_id") or current_round_id() or f"unattributed-{int(time.time())}"
    base_id = _safe_id(str(source_round))
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _fsync_dir(root)
        _fsync_dir(root.parent)
        existing = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as error:
        raise RecordError(f"could not create {root}: {error}") from None
    for record in existing:
        manifest = _read_manifest(record)
        # The fingerprint covers every tip and every saved byte, so a match is the same source even
        # when the checkout has since been moved aside.
        if manifest and manifest["fingerprint"] == snap.fingerprint:
            if _complete(record) is None:
                continue  # damaged: never trusted, never deleted; write a fresh copy instead
            try:
                _sync_record(record)
            except OSError as error:
                raise RecordError(f"could not re-sync {record}: {error}") from None
            return record  # this exact source is already durable; never write it twice
    record_id, n = base_id, 1
    while (root / record_id).exists():
        n += 1
        record_id = f"{base_id}.{n}"
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
            files[path.name] = dict(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
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
        )
        _write(tmp / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
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
    )
    if check and p.returncode:
        raise RecordError(f"git update-ref failed: {p.stderr.decode(errors='replace').strip()[-200:]}")


def _supersede(cfg, repo: Path, current: Path | None) -> None:
    """Records left `recovery-blocked` by the earlier blocking guard no longer describe the checkout.
    Their data is kept; only the disposition changes."""
    for _, record, manifest, status in records(cfg.state.parent, workers={cfg.state.name}):
        if (
            record is not None
            and record != current
            and manifest is not None
            and manifest.get("checkout") == str(repo)
            and status.get("state") == "recovery-blocked"
        ):
            _set_status(
                record, "superseded", f"checkout changed; see {current.name if current else 'a clean checkout'}"
            )


def _set_status(record: Path, state: str, reason: str = "") -> bool:
    try:
        atomic_json(record / "status.json", dict(state=state, reason=reason, updated_at=time.time()))
        _fsync_dir(record)
    except OSError as error:
        log(f"recovery: could not update {record.name} status ({error})")
        return False
    return True


def _preserved(cfg) -> list[str]:
    """Record ids whose preserved refs may exempt commits: only complete records count."""
    return [
        record.name
        for _, record, _, _ in records(cfg.state.parent, workers={cfg.state.name})
        if record is not None and _complete(record) is not None
    ]


def live_users(repo: Path) -> list[str]:
    """Processes whose cwd, root, or open files lie inside the checkout (Linux /proc; read-only).
    An empty result is NOT proof of quiescence: a detached process may hold nothing open there."""
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
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        links = [entry / "cwd", entry / "root"]
        with contextlib.suppress(OSError):
            links += list((entry / "fd").iterdir())
        for link in links:
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


MAX_PRUNE_PER_PREPARATION = 3
# Next to the checkout, so moving aside is a same-volume rename. Not `retained`: progress parks
# publication bundles in state/<worker>/progress/retained, beside the roadmap clone.
RETAINED_DIR = "retained-checkouts"


def _retain(cfg, repo: Path, reason: str, record: Path | None, label: str) -> Decision:
    """Move the whole checkout aside, unmodified, so preparation can continue on a fresh one.

    A same-directory rename needs no free space and leaves every byte (and any process still
    working there) exactly where it was. The retained copy is saved and removed automatically by a
    later preparation once nothing uses it, so no operator is ever needed."""
    global LAST_REFUSAL
    parent = repo.parent / RETAINED_DIR
    dest = parent / f"{repo.name}-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(repo, dest)
    except OSError as error:
        # Neither saved nor movable: leave everything untouched; the next round tries again.
        LAST_REFUSAL = f"{reason}, and moving {repo} aside failed ({error}); the checkout is left intact"
        return Decision(False, LAST_REFUSAL, record, "refused")
    with contextlib.suppress(OSError):  # durability and attribution are best effort here
        _fsync_dir(parent)
        _fsync_dir(repo.parent)
    owner = read_json(owner_path(cfg, label))
    if owner:
        with contextlib.suppress(OSError):
            atomic_json(dest.with_name(dest.name + ".owner.json"), owner)
    if record is not None:
        _set_status(record, "retained", f"{reason}; checkout moved to {dest}")
    log(f"recovery: {reason}; moved the checkout to {dest} and continuing with a fresh one")
    return Decision(True, reason, record, "retained", dict(retained=str(dest)))


def retain(cfg, repo: Path, reason: str, *, label: str = "TauCeti") -> Decision:
    """Move an unusable checkout (a stale lock, damaged metadata, an interrupted clone) aside whole."""
    return _retain(cfg, repo, reason, None, label)


def _evict_caches(tree: Path) -> None:
    """Reclaim a retained checkout's dependency and build caches; never its source."""
    for root, dirs, _ in os.walk(tree, followlinks=False):
        for name in [d for d in dirs if d in CACHE_DIRS or (root == str(tree) and d in CACHE_TOP)]:
            path = Path(root) / name
            if not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
        dirs[:] = [d for d in dirs if d not in CACHE_DIRS and d != ".git"]


def _prune_retained(cfg, repo: Path, label: str, repository: str) -> None:
    """Save and remove retained checkouts that nothing uses any more, a few per preparation.

    A copy is removed only after a record of it is durable and a second inspection shows it did not
    change meanwhile. A copy that cannot be saved keeps its source but loses its caches, so retained
    checkouts cannot fill the disk with build output. Removal renames to `*.deleting` first, so an
    interrupted delete is finished later without needing git."""
    parent = repo.parent / RETAINED_DIR
    if not parent.is_dir():
        return
    for doomed in parent.glob("*.deleting"):
        shutil.rmtree(doomed, ignore_errors=True)
    budget = MAX_PRUNE_PER_PREPARATION
    for old in sorted(p for p in parent.glob(f"{repo.name}-*") if p.is_dir() and not p.name.endswith(".deleting")):
        if budget <= 0:
            return
        budget -= 1
        if live_users(old):
            continue
        sidecar = old.with_name(old.name + ".owner.json")
        try:
            snap = inspect(old, _preserved(cfg))
            record = _record(cfg, old, snap, label, repository, owner=read_json(sidecar)) if snap.needs_record else None
            if live_users(old) or inspect(old, _preserved(cfg)).fingerprint != snap.fingerprint:
                continue  # something touched it while saving; try again later
            doomed = old.with_name(old.name + ".deleting")
            os.rename(old, doomed)
            if record is not None:
                _set_status(record, "checkout-reset", f"retained checkout {old.name} saved and removed")
            shutil.rmtree(doomed, ignore_errors=True)
            sidecar.unlink(missing_ok=True)
            log(f"recovery: removed retained checkout {old}" + (f"; its source is in {record}" if record else ""))
        except (OSError, subprocess.SubprocessError, RecordError) as error:
            log(f"recovery: keeping retained checkout {old} for now ({error}); evicting its caches")
            _evict_caches(old)


def _dirty_without_inspection(repo: Path) -> bool:
    """Rollback's only check: does git report anything a reset would discard? Unknown counts as yes."""
    p = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--ignored=no", "--untracked-files=all"],
        capture_output=True,
    )
    return p.returncode != 0 or bool(p.stdout.strip())


def guard(cfg, repo: Path, *, label: str = "TauCeti", repository: str = TAUCETI) -> Decision:
    """Save anything unpublished in `repo`, then let preparation continue. Never waits for a human.

    The normal outcome is a durable record followed by a reset in place, including after an OOM or
    SIGKILL. Every reset is checked against a final inspection, so a write that lands after the
    snapshot is saved too. When a process still uses the checkout, the save fails, or the checkout
    will not hold still, the whole checkout is moved aside instead (`state == "retained"`) and the
    caller creates a fresh one. Refusal is left only for a checkout that can be neither saved nor
    moved, and the next round simply retries."""
    global LAST_REFUSAL
    LAST_REFUSAL = ""
    _prune_retained(cfg, repo, label, repository)
    if os.environ.get("TAUCETI_WORK_RECOVERY", "").lower() == "off":
        # Rollback: no inspection or records. A dirty tree is still never reset; it is moved aside.
        if _dirty_without_inspection(repo):
            return _retain(cfg, repo, "TAUCETI_WORK_RECOVERY=off and the checkout is dirty", None, label)
        return Decision(True, state="disabled")
    record = None
    for _ in range(3):
        try:
            snap = inspect(repo, _preserved(cfg))
        except (OSError, subprocess.SubprocessError, RecordError) as error:
            return _retain(cfg, repo, f"could not inspect {repo} ({error})", record, label)
        if snap.published:
            users = live_users(repo)
            if users:  # nothing to lose now, but a live writer could still create some
                return _retain(cfg, repo, "processes still use the checkout: " + ", ".join(users[:8]), None, label)
            _supersede(cfg, repo, None)
            return Decision(True, state="clean")
        if snap.needs_record:
            try:
                record = _record(cfg, repo, snap, label, repository)
            except RecordError as error:
                return _retain(cfg, repo, f"could not save its source ({error})", record, label)
            _supersede(cfg, repo, record)
        users = live_users(repo)
        if users:
            return _retain(cfg, repo, "processes still use the checkout: " + ", ".join(users[:8]), record, label)
        try:
            again = inspect(repo, _preserved(cfg))
        except (OSError, subprocess.SubprocessError, RecordError) as error:
            return _retain(cfg, repo, f"could not re-inspect {repo} ({error})", record, label)
        if again.fingerprint != snap.fingerprint:
            continue  # a late write: save the newer state before resetting
        state = "checkout-reset" if snap.dirty else "preserved"
        if record is not None:
            _set_status(record, state, "saved before the checkout was reset")
            what = "unfinished source" if snap.dirty else f"{len(snap.unpublished)} unpublished commit(s)"
            log(f"recovery: saved {what} in {record}")
        return Decision(True, record=record, state=state)
    return _retain(cfg, repo, "the checkout kept changing while it was being saved", record, label)


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
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise RecordError(f"unsafe archive member {member.name}")
            dest = repo / member.name
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
                yield worker.name, record, _complete(record, digests=False), read_json(record / "status.json")


def summary(state_root: Path | None = None, *, workers=None) -> dict:
    """Background preparation only; status reads the resulting JSON and never GitHub."""
    counts = dict(records=0, retained=0, blocked=0, preserved=0, reset=0, superseded=0, incomplete=0, corrupt=0)
    reasons = set()
    for worker, record, manifest, status in records(state_root, workers=workers):
        if record is None:
            counts["incomplete"] += 1
            continue
        if manifest is None:
            counts["corrupt"] += 1
            reasons.add(f"{worker}/{record.name}: manifest or payload missing or invalid")
            continue
        counts["records"] += 1
        state = status.get("state")
        if state == "retained":
            counts["retained"] += 1
            reasons.add(f"{worker}: {status.get('reason') or 'checkout moved aside'}")
        elif state == "recovery-blocked":
            counts["blocked"] += 1  # written only by the earlier blocking guard; superseded on next use
        elif state == "checkout-reset":
            counts["reset"] += 1
        elif state == "superseded":
            counts["superseded"] += 1
        else:
            counts["preserved"] += 1
    return dict(counts, reasons=sorted(reasons))

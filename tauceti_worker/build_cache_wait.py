"""Wait for a TauCeti build cache whose dependencies match the round, instead of rebuilding TauCeti.

`lake cache get --max-revs=N` walks back from HEAD and installs the first revision with a published
map. After a Mathlib bump lands, that revision can predate the bump: none of its outputs apply, and
the round would compile every TauCeti module, which cannot finish inside the round cap. Preparation
therefore checks which revision Lake installed and compares its `lake-manifest.json` package
revisions and `lean-toolchain` with the checkout's. When they differ the round ends as a wait
(`waiting-build-cache`) before the agent starts.

A small marker per blocked commit in the host's build-cache pool lets the other workers on the host
skip that commit for a few minutes. Expiry and an anonymous HTTP check of the revision map are the
only releases besides a successful preparation. Every error reading, writing or probing fails open:
this mechanism may never block work by itself breaking.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import log

RETRY_SECONDS = 300  # how long a recorded wait keeps the host's other workers off the commit
WARN_SECONDS = 3600  # a wait this old logs one warning per host and commit
PR_GRACE_SECONDS = 7200  # a PR whose own head changes dependencies stops waiting after this
PROBE_SECONDS = 30  # at most one published-map probe per marker and host in this interval
FORGET_SECONDS = 7 * 86400  # stale markers are removed by readers
SCOPE = "TauCeti"  # Lake's local revision-map scope: <cache>/revisions/TauCeti/<rev>.jsonl
ENABLE = "TAUCETI_BUILD_CACHE_WAIT"  # set by worker agent rounds; other cache users never wait
_SHA = re.compile(r"[0-9a-f]{40}")


def directory(env=None) -> Path:
    from . import expanded_cache

    return expanded_cache.root(env) / "cache-waits"


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def fingerprint(manifest: str | None, toolchain: str | None):
    """The parts of a revision that decide whether its outputs can apply at all; None if unknown."""
    if manifest is None or toolchain is None:
        return None
    try:
        packages = json.loads(manifest).get("packages")
        if not isinstance(packages, list):
            return None
        pins = tuple(sorted((str(p.get("name")), str(p.get("rev"))) for p in packages if isinstance(p, dict)))
    except (ValueError, AttributeError):
        return None
    return toolchain.strip(), pins


def committed(cwd: Path, rev: str):
    return fingerprint(_git(cwd, "show", f"{rev}:lake-manifest.json"), _git(cwd, "show", f"{rev}:lean-toolchain"))


def working(cwd: Path):
    """The checkout's dependency files as preparation sees them.

    A rebase round (and a repair whose pin is refused) prepares with origin/main's dependency files
    in place (shared_mathlib.rebase_target): main's cache is the one its agent will build against.
    """
    try:
        return fingerprint((cwd / "lake-manifest.json").read_text(), (cwd / "lean-toolchain").read_text())
    except OSError:
        return None


def installed_revision(cwd: Path, cache_dir: str | None, max_revs: str) -> str | None:
    """The revision whose map `lake cache get` (no `--rev`) just used, or None if it cannot be told.

    Lake walks `git rev-list HEAD -n <max-revs>` and takes the first revision whose map is already in
    the cache directory or downloads. Maps seeded from the pool are CI's own maps for those exact
    revisions, so the first present map in the same walk is exactly the one Lake used, seeded or not.
    This must run before any `--rev` fetch adds other maps.
    """
    if not cache_dir:
        return None
    listed = _git(cwd, "rev-list", *([] if max_revs == "0" else ["-n", max_revs]), "HEAD")
    if not listed:
        return None
    maps = Path(cache_dir) / "revisions" / SCOPE
    for rev in listed.split():
        if _SHA.fullmatch(rev) and (maps / f"{rev}.jsonl").is_file():
            return rev
    return None


def revision_url(rev: str, toolchain: str) -> str:
    from .agents import TAUCETI_CACHE_REVISION_URL
    from .constants import TAUCETI

    name = toolchain.strip()
    if ":" not in name and "/" not in name:
        name = "leanprover/lean4:" + name
    # Lake's toolchain2Dir: '/' -> '--', ':' -> '---'.
    mangled = name.replace("/", "--").replace(":", "---")
    return f"{TAUCETI_CACHE_REVISION_URL.rstrip('/')}/{TAUCETI}/tc/{urllib.parse.quote(mangled)}/{rev}.jsonl"


def published(rev: str, toolchain: str, *, timeout: float = 5) -> bool:
    """Whether CI has published this revision's map; any error answers False."""
    if not toolchain:
        return False
    request = urllib.request.Request(revision_url(rev, toolchain), method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


@contextlib.contextmanager
def _locked(env=None):
    folder = directory(env)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield folder


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def record(commit: str, *, scope: str, toolchain: str, installed: str | None, env=None, now=None) -> dict:
    """Record (or extend) a wait on `commit`; returns the marker, with `first_wait_at` preserved."""
    now = time.time() if now is None else now
    try:
        with _locked(env) as folder:
            path = folder / f"{commit}.json"
            marker = _read(path)
            if scope == "main":
                # Main has one current commit: an older main commit's wait is superseded by this one.
                for other in folder.glob("*.json"):
                    if other != path and _read(other).get("scope") == "main":
                        other.unlink(missing_ok=True)
            marker.setdefault("first_wait_at", now)
            marker.update(
                commit=commit, scope=scope, toolchain=toolchain, installed=installed, retry_at=now + RETRY_SECONDS
            )
            marker.pop("released", None)
            if now - marker["first_wait_at"] >= WARN_SECONDS and not marker.get("warned_at"):
                marker["warned_at"] = now
                hours = (now - marker["first_wait_at"]) / 3600
                log(
                    f"warning: {scope} commit {commit[:12]} has waited {hours:.1f}h for a TauCeti build cache "
                    f"matching its dependencies; check that CI published {revision_url(commit, toolchain)}"
                )
            _write(path, marker)
            return marker
    except OSError as error:
        log(f"build-cache wait marker for {commit[:12]} could not be written: {error}")
        return dict(commit=commit, scope=scope, first_wait_at=now, retry_at=now + RETRY_SECONDS)


def release(commit: str, env=None) -> None:
    """Stop the host waiting on this commit while keeping its first-wait time (the PR grace is spent)."""
    with contextlib.suppress(OSError), _locked(env) as folder:
        path = folder / f"{commit}.json"
        marker = _read(path)
        if marker:
            marker["released"] = True
            _write(path, marker)


def clear(commit: str, *, scope: str, env=None) -> None:
    """A usable cache was found: drop this commit's wait, and for main every main wait."""
    with contextlib.suppress(OSError), _locked(env) as folder:
        (folder / f"{commit}.json").unlink(missing_ok=True)
        if scope == "main":
            for other in folder.glob("*.json"):
                if _read(other).get("scope") == "main":
                    other.unlink(missing_ok=True)


def deferral(commit: str | None = None, *, scope: str = "pr", env=None, now=None, probe=None) -> dict | None:
    """The fresh marker that should keep this worker off `commit` (or off main), else None.

    An expired marker lets exactly one worker through for a real check: that worker re-arms it
    for everyone else. A fresh marker is cleared as soon as CI's map for its commit is published.
    """
    now = time.time() if now is None else now
    probe = probe or published
    try:
        folder = directory(env)
        if scope == "main":
            paths = [p for p in folder.glob("*.json") if _read(p).get("scope") == "main"] if folder.is_dir() else []
        else:
            paths = [folder / f"{commit}.json"] if commit and (folder / f"{commit}.json").is_file() else []
        for path in paths:
            marker = _read(path)
            if not marker or marker.get("released"):
                continue
            probe_due = False
            with _locked(env):
                marker = _read(path)
                if not marker or marker.get("released"):
                    continue
                if now - float(marker.get("first_wait_at", now)) > FORGET_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
                if now >= float(marker.get("retry_at", 0)):
                    marker["retry_at"] = now + RETRY_SECONDS  # this worker takes the real check
                    _write(path, marker)
                    continue
                if now - float(marker.get("probed_at", 0)) >= PROBE_SECONDS:
                    marker["probed_at"] = now
                    _write(path, marker)
                    probe_due = True
            if probe_due and probe(str(marker.get("commit", "")), str(marker.get("toolchain") or "")):
                log(f"build cache for {marker.get('commit', '')[:12]} is now published; its wait is over")
                with contextlib.suppress(OSError), _locked(env):
                    path.unlink(missing_ok=True)
                continue
            return marker
    except Exception as error:  # noqa: BLE001 - a broken marker must never block work
        log(f"build-cache wait marker unreadable ({type(error).__name__}: {error}); not waiting")
    return None


def check(cwd: Path, env: dict, *, head: str, main: str, installed: str | None, main_hit: str | None) -> None:
    """Raise BuildCacheWait when no installed cache has the checkout's dependencies.

    `installed` is the revision the HEAD-history fetch used; `main_hit` the revision the main fetch
    used in a PR round. Either matching the checkout is enough. Unknown facts proceed.
    """
    from .agents import BuildCacheWait

    if env.get(ENABLE) != "1" or not head:
        return
    scope = "pr" if main and main != head else "main"
    try:
        target = working(cwd)
        if target is None:
            return
        candidates = [rev for rev in dict.fromkeys((installed, main_hit)) if rev]
        if not candidates:
            return  # no identifiable install; today's behaviour
        for rev in candidates:
            deps = committed(cwd, rev)  # HEAD's own too: a rebase round prepares main's files
            if deps is None:
                return
            if deps == target:
                if rev != head:
                    log(
                        f"host build caches: TauCeti cache is a same-dependency fallback from {rev[:12]} "
                        f"for {head[:12]}; only the commits between them rebuild"
                    )
                clear(head, scope=scope, env=env)
                return
        toolchain = (_git(cwd, "show", f"{head}:lean-toolchain") or "").strip()
        marker = record(head, scope=scope, toolchain=toolchain, installed=candidates[0], env=env)
        waited = time.time() - float(marker.get("first_wait_at", time.time()))
        if scope == "pr" and committed(cwd, main) != target and waited >= PR_GRACE_SECONDS:
            # A PR that changes dependencies itself has a cache only once CI builds it; CI may
            # never manage to. After the grace, build as before so its repair can proceed.
            release(head, env=env)
            log(
                f"warning: PR head {head[:12]} changes dependencies and has waited {waited / 3600:.1f}h "
                "for its own build cache; building against the fallback cache"
            )
            return
    except Exception as error:  # noqa: BLE001 - the check must never block work by failing
        log(f"build-cache dependency check skipped ({type(error).__name__}: {error})")
        return
    label = "main" if scope == "main" else "PR head"
    message = (
        f"waiting-build-cache: {label} {head[:12]} has no published TauCeti cache with its dependencies "
        f"(the cache found, from {' and '.join(rev[:12] for rev in candidates)}, has a different "
        f"lake-manifest.json or lean-toolchain); agent not started, waiting {waited / 60:.0f}m so far"
    )
    raise BuildCacheWait(message, retry_at=marker.get("retry_at"), commit=head)

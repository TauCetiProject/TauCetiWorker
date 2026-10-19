"""Immutable extracted Mathlib snapshots with rootless per-round copy-on-write views.

Only fresh local clones feed publication. Agent workspaces are NEVER snapshot sources.
Locks outside entries protect publication and leases against GC; no root or daemon.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import pwd
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .runtime_status import atomic_json, read_json


class Unavailable(RuntimeError):
    pass


def root(env=None):
    env = os.environ if env is None else env
    login = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if value := env.get("TAUCETI_SHARED_MATHLIB_ROOT"):
        return (login / value[2:] if value.startswith("~/") else Path(value)).resolve()
    return login / ".cache/tauceti/mathlib-builds"


def pin_root(env):
    env.setdefault("TAUCETI_SHARED_MATHLIB_ROOT", str(root(env)))


def mode(env):
    policy = read_json(root(env) / "policy.json")
    expired = policy.get("expires_at", float("inf")) <= time.time()
    value = env.get(
        "TAUCETI_SHARED_MATHLIB_BUILDS", policy.get("expiry_mode", "off") if expired else policy.get("mode", "auto")
    ).lower()
    if value not in {"auto", "off", "required"}:
        raise Unavailable("TAUCETI_SHARED_MATHLIB_BUILDS must be auto, off or required")
    allowed = env.get("TAUCETI_SHARED_MATHLIB_WORKERS", ",".join(policy["workers"]) if "workers" in policy else None)
    if allowed is not None and env.get("TAUCETI_WORKER_ID", "default") not in allowed.split(","):
        return "off"
    return value


def overlay_command(lower, upper, work, target, pool, *, lease_fd=None):
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--ro-bind",
        str(pool),
        str(pool),
        *(["--sync-fd", str(lease_fd)] if lease_fd is not None else []),
        "--overlay-src",
        str(lower),
        "--overlay",
        str(upper),
        str(work),
        str(target),
        "--",
    ]


def capability(directory=None):
    """Exercise the actual filesystem, namespace, OverlayFS and bwrap flag support."""
    if platform.system() != "Linux":
        return False, "Linux user namespaces and OverlayFS are required; use private builds on this system"
    if not shutil.which("bwrap"):
        return False, "install bubblewrap with --overlay support (0.12 or newer) to share extracted Mathlib"
    try:
        with tempfile.TemporaryDirectory(prefix="tauceti-overlay-probe-", dir=directory) as raw:
            here = Path(raw)
            lower, upper, work = (here / n for n in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            (lower / "probe").write_text("shared")
            command = overlay_command(lower, upper, work, upper, lower)
            p = subprocess.run(
                command
                + ["sh", "-c", 'test "$(cat "$1/probe")" = shared && printf private > "$1/probe"', "sh", str(upper)],
                capture_output=True,
                timeout=10,
            )
            if p.returncode or (lower / "probe").read_text() != "shared" or (upper / "probe").read_text() != "private":
                return (
                    False,
                    "rootless OverlayFS probe failed: "
                    + p.stderr.decode(errors="replace")[-400:].strip()
                    + "; administrator may need to allow unprivileged user namespaces or provide an OverlayFS-capable filesystem",
                )
        return True, "rootless OverlayFS works; immutable lower with private copy-on-write builds"
    except (OSError, subprocess.TimeoutExpired) as error:
        return (
            False,
            f"rootless OverlayFS unavailable: {error}; use a local OverlayFS-capable filesystem and allow user namespaces",
        )


def git(path, *args):
    p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise Unavailable("cannot identify clean dependency sources: " + p.stderr.strip()[-200:])
    return p.stdout.strip()


def signature(cwd, env):
    # Loading Lake synchronizes pinned dependency checkouts without compiling
    # Mathlib. Do this before deciding the key, so a manifest bump can hit a
    # shared snapshot on its first round instead of downloading privately first.
    lean = subprocess.run(
        ["lake", "env", "lean", "--version"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
    )
    if lean.returncode:
        raise Unavailable("Lean toolchain is unavailable; run lake env lean --version")
    manifest = json.loads((cwd / "lake-manifest.json").read_text())
    packages = manifest.get("packages", [])
    mathlib = cwd / ".lake/packages/mathlib"
    if not mathlib.is_dir():
        raise Unavailable("Mathlib dependency is not checked out yet")
    dependencies = {}
    for package in packages:
        name = package.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name or package.get("type") != "git":
            raise Unavailable("shared snapshots require pinned git dependencies; path dependencies use private builds")
        source = cwd / ".lake/packages" / name
        revision = git(source, "rev-parse", "HEAD")
        if revision != package.get("rev") or git(source, "status", "--porcelain", "--untracked-files=normal"):
            raise Unavailable(f"dependency {name} differs from its pinned revision; using private builds")
        dependencies[name] = revision
    if "mathlib" not in dependencies:
        raise Unavailable("manifest has no pinned Mathlib dependency")
    toolchain = (cwd / "lean-toolchain").read_text().strip()
    prefix = subprocess.run(
        ["lake", "env", "lean", "--print-prefix"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
    )
    library = Path(prefix.stdout.strip()) / "lib/lean"
    if prefix.returncode or not (library / "Init.olean").is_file():
        raise Unavailable("cannot identify toolchain artifact format; run lake env lean --print-prefix")
    artifacts = [
        ".olean",
        ".ilean",
        *[
            suffix
            for suffix in (".olean.server", ".olean.private", ".ir", ".ir.sig")
            if (library / ("Init" + suffix)).is_file()
        ],
    ]
    config = {
        name: (cwd / name).read_text() if (cwd / name).is_file() else None
        for name in ("lake-manifest.json", "lakefile.toml", "lakefile.lean", "lean-toolchain")
    }
    spec = dict(
        version=1,
        artifacts=artifacts,
        mathlib=dependencies["mathlib"],
        dependencies=dependencies,
        config=config,
        mathlib_manifest=(mathlib / "lake-manifest.json").read_text(),
        toolchain=toolchain,
        abi=lean.stdout.strip(),
        architecture=platform.machine(),
        system=platform.system(),
    )
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), spec


@contextlib.contextmanager
def key_lock(pool, key, exclusive=True, nonblocking=False):
    locks = pool / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (key + ".lock")).open("a+") as stream:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(stream, flags | (fcntl.LOCK_NB if nonblocking else 0))
        yield stream


def remove_tree(path):
    """OverlayFS can leave work/work mode 000; make directories removable first."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    os.chmod(path, 0o700)
    for here, directories, _ in os.walk(path, followlinks=False):
        for name in directories:
            child = Path(here) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(path)


def detach_upper(upper, marker):
    """A sparse overlay delta, including whiteouts, is never a standalone build."""
    if not marker.exists():
        return
    delta = upper.with_name("build-shared-delta")
    remove_tree(delta)
    if upper.exists():
        os.replace(upper, delta)
    upper.mkdir(parents=True, exist_ok=True)
    remove_tree(upper.with_name("tauceti-overlay-work"))
    marker.unlink(missing_ok=True)


def durable_tree(path):
    """Sync immutable data and directories before publishing its ready marker."""
    for here, _, files in os.walk(path, topdown=False):
        for name in files:
            file = Path(here) / name
            if file.is_symlink():
                if not file.resolve().is_relative_to(path.resolve()):
                    raise Unavailable("snapshot has a symlink outside its immutable build tree")
                continue
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(here, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def collect(pool, *, protect=(), now=None):
    """Bounded by snapshot count. Locked readers/builders are never reclaimed."""
    now = time.time() if now is None else now
    entries = pool / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    # A killed builder cannot run TemporaryDirectory cleanup. Reclaim its
    # staging tree before evicting useful published snapshots for disk pressure.
    for stage in (pool / "staging").glob("*"):
        key = stage.name.split("-", 1)[0]
        if key in protect or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            continue
        if not stage.is_dir() or now - stage.stat().st_mtime < 4 * 3600:
            continue
        try:
            with key_lock(pool, key, nonblocking=True):
                remove_tree(stage)
        except (BlockingIOError, FileNotFoundError):
            pass
    candidates = sorted((p for p in entries.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    removed = []
    for entry in candidates:
        if entry.name in protect:
            continue
        usage = shutil.disk_usage(pool)
        if now - entry.stat().st_mtime < 7 * 86400 and usage.free / usage.total >= 0.1:
            continue
        try:
            with key_lock(pool, entry.name, nonblocking=True):
                shutil.rmtree(entry)
                removed.append(entry.name)
        except (BlockingIOError, FileNotFoundError):
            pass
    return removed


def clone(source, target, revision):
    for command in (
        ["git", "clone", "--shared", "--quiet", "--no-checkout", str(source), str(target)],
        ["git", "-C", str(target), "checkout", "--quiet", "--detach", revision],
    ):
        p = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if p.returncode:
            raise Unavailable("fresh snapshot checkout failed: " + p.stderr.strip()[-300:])


def verify(build, mathlib, artifacts=(".olean", ".ilean")):
    modules = [mathlib / "Mathlib.lean", *sorted((mathlib / "Mathlib").rglob("*.lean"))]
    if len(modules) < 2:
        raise Unavailable("fresh Mathlib source tree is incomplete")
    count = 0
    for source in modules:
        rel = source.relative_to(mathlib)
        for suffix in artifacts:
            path = build / "lib/lean" / rel.with_suffix(suffix)
            if not path.is_file() or path.stat().st_size == 0:
                raise Unavailable(f"cache extraction incomplete: missing {rel.with_suffix(suffix)}")
            count += 1
    return count


def publish(pool, key, spec, cwd, env, logdir, fetch):
    """Called under exclusive key lock. A failed download never publishes a lower."""
    entry = pool / "entries" / key
    if read_json(entry / "ready.json").get("key") == key and (entry / "build").is_dir():
        os.utime(entry, None)
        return entry, True
    collect(pool, protect=(key,))
    usage = shutil.disk_usage(pool)
    if usage.free / usage.total < 0.1:
        raise Unavailable("shared snapshot pool has under 10% disk free and no unused snapshots can be removed")
    staging = pool / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=key + "-", dir=staging) as raw:
        stage = Path(raw)
        project = stage / "project"
        clone(cwd, project, git(cwd, "rev-parse", "HEAD"))
        # Config comes from the selected workspace, but no mutable build output
        # or agent-written source is copied. Dependency revisions are verified.
        for name, text in spec["config"].items():
            if text is not None:
                (project / name).write_text(text)
            else:
                (project / name).unlink(missing_ok=True)
        for name, revision in spec["dependencies"].items():
            clone(cwd / ".lake/packages" / name, project / ".lake/packages" / name, revision)
        logdir.mkdir(parents=True, exist_ok=True)
        log = logdir / "shared-mathlib-prepare.log"
        with log.open("ab") as output:
            rc = fetch(["lake", "exe", "cache", "get"], project, env, output, 1200)
        if rc:
            raise Unavailable(f"fresh Mathlib extraction failed (exit {rc}); see {log}")
        mathlib = project / ".lake/packages/mathlib"
        build = mathlib / ".lake/build"
        count = verify(build, mathlib, spec["artifacts"])
        # Detect dependency/source changes while staging, before publication.
        if signature(cwd, env)[0] != key:
            raise Unavailable("workspace dependency configuration changed during cache preparation")
        ready = stage / "ready"
        ready.mkdir()
        shutil.move(str(build), ready / "build")
        durable_tree(ready / "build")
        atomic_json(ready / "ready.json", dict(key=key, spec=spec, files_verified=count, published_at=time.time()))
        collect(pool, protect=(key,))
        usage = shutil.disk_usage(pool)
        if usage.free / usage.total < 0.1:
            raise Unavailable("snapshot extraction would leave under 10% disk free; using private builds")
        entry.parent.mkdir(parents=True, exist_ok=True)
        if entry.exists():
            shutil.rmtree(entry)  # only incomplete entry; no reader can hold this exclusive lock
        os.replace(ready, entry)
        fd = os.open(entry.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry, False


def report(pool, env, **fields):
    worker = env.get("TAUCETI_WORKER_ID", "default")
    identity = hashlib.sha256(worker.encode()).hexdigest()
    try:
        atomic_json(
            pool / "status" / (identity + ".json"),
            dict(worker=worker, scope=env.get("TAUCETI_FLEET_MACHINE_ID", "local"), observed_at=time.time(), **fields),
        )
    except OSError:
        from .config import log

        log("shared Mathlib reporting unavailable; " + fields.get("reason", fields.get("mode", "unknown")))


def summary(env=None, *, scope=None, worker=None):
    """Background preparation only; status itself reads the resulting summary JSON."""
    pool = root(env)
    rows = [read_json(p) for p in (pool / "status").glob("*.json")]
    rows = [
        r
        for r in rows
        if time.time() - r.get("observed_at", 0) < 86400
        and (scope is None or r.get("scope", "local") == scope)
        and (worker is None or r.get("worker") == worker)
    ]
    return dict(
        shared=sum(r.get("mode") == "shared" for r in rows),
        private=sum(r.get("mode") == "private" for r in rows),
        cache_hits=sum(r.get("cache_hit", False) for r in rows),
        preparing=sum(r.get("mode") == "preparing" for r in rows),
        preparation_seconds=sum(r.get("preparation_seconds", 0) for r in rows),
        reasons=sorted({r["reason"] for r in rows if r.get("reason")}),
    )


@contextlib.contextmanager
def view(cwd, env, logdir, fetch):
    """Yield an argv prefix and keep the lower leased until the whole agent exits."""
    selected = mode(env)
    pool = root(env)
    started = time.monotonic()
    upper = cwd / ".lake/packages/mathlib/.lake/build"
    marker = upper.parent / "tauceti-shared-mathlib.json"
    if selected == "off" or not (cwd / ".lake/packages/mathlib").is_dir():
        # Normal private cache warming reconstructs any files previously visible
        # only in the lower. Clearing the marker forces a fresh empty upper when
        # sharing is enabled again at a later idle boundary.
        detach_upper(upper, marker)
        if selected != "off" and selected == "required":
            raise Unavailable("Mathlib dependency is not available for sharing")
        if selected == "off":
            report(pool, env, mode="private", reason="sharing disabled")
        yield []
        return
    prefix, backup, backup_created, backup_sparse = [], None, False, False
    with contextlib.ExitStack() as stack:
        try:
            pool.mkdir(parents=True, exist_ok=True, mode=0o700)
            ok, reason = capability(pool)
            if not ok:
                raise Unavailable(reason)
            key, spec = signature(cwd, env)
            report(pool, env, mode="preparing", key=key)
            lease = stack.enter_context(key_lock(pool, key, exclusive=False))
            entry = pool / "entries" / key
            hit = read_json(entry / "ready.json").get("key") == key and (entry / "build").is_dir()
            if hit:
                os.utime(entry, None)
            else:
                # Drop the read lease before competing to build. publish checks
                # again after locking, so only one builder downloads this key.
                fcntl.flock(lease, fcntl.LOCK_UN)
                fcntl.flock(lease, fcntl.LOCK_EX)
                entry, hit = publish(pool, key, spec, cwd, env, logdir, fetch)
                fcntl.flock(lease, fcntl.LOCK_SH)
            upper.parent.mkdir(parents=True, exist_ok=True)
            ok, reason = capability(upper.parent)
            if not ok:
                raise Unavailable(reason)
            old = read_json(marker)
            backup = upper.with_name("build-before-sharing")
            work = upper.with_name("tauceti-overlay-work")
            if old.get("key") != key:
                if backup.exists():
                    remove_tree(backup)
                backup_sparse = marker.exists()
                if upper.exists():
                    os.replace(upper, backup)
                    backup_created = True
                upper.mkdir(parents=True)
                if work.exists():
                    remove_tree(work)
                atomic_json(marker, dict(key=key, activated_at=time.time()))
            work.mkdir(exist_ok=True)
            candidate = overlay_command(entry / "build", upper, work, upper, pool, lease_fd=lease.fileno())
            probe = subprocess.run(
                candidate + ["test", "-f", str(upper / "lib/lean/Mathlib.olean")],
                capture_output=True,
                timeout=15,
                pass_fds=(lease.fileno(),),
            )
            if probe.returncode:
                raise Unavailable("shared Mathlib view failed: " + probe.stderr.decode(errors="replace")[-300:])
            fcntl.flock(lease, fcntl.LOCK_SH)
            prefix = candidate
            env["TAUCETI_SHARED_MATHLIB_LEASE_FD"] = str(lease.fileno())
            env["TAUCETI_SHARED_MATHLIB_KEY"] = key
            report(pool, env, mode="shared", key=key, cache_hit=hit, preparation_seconds=time.monotonic() - started)
        except (Unavailable, OSError, ValueError, subprocess.TimeoutExpired) as error:
            if backup_created and backup and backup.exists():
                remove_tree(upper)
                if backup_sparse:
                    upper.mkdir(parents=True)
                else:
                    os.replace(backup, upper)
                marker.unlink(missing_ok=True)
            else:
                detach_upper(upper, marker)
            report(pool, env, mode="private", reason=str(error), preparation_seconds=time.monotonic() - started)
            if selected == "required":
                raise Unavailable(str(error)) from error
            prefix = []
        # The body is outside the preparation handler: never retry paid work.
        try:
            yield prefix
        finally:
            env.pop("TAUCETI_SHARED_MATHLIB_LEASE_FD", None)
            env.pop("TAUCETI_SHARED_MATHLIB_KEY", None)
        if prefix and backup and backup.exists():
            remove_tree(backup)


def lease_fds(env):
    value = env.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
    return (int(value),) if value is not None else ()


def record_result(env, code):
    key = env.get("TAUCETI_SHARED_MATHLIB_KEY")
    if not key:
        return
    from .activity import current_round_id

    record = dict(
        at=time.time(),
        worker=env.get("TAUCETI_WORKER_ID", "default"),
        round_id=current_round_id(),
        key=key,
        exit_code=code,
    )
    pool = root(env)
    try:
        with (pool / "results.jsonl").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        from .config import log

        log("shared Mathlib result receipt unavailable; paid invocation exit status preserved")

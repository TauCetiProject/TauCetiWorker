"""Immutable extracted Mathlib snapshots with rootless per-round copy-on-write views.

Only fresh local clones feed publication. Agent workspaces are NEVER snapshot sources.
Locks outside entries protect publication and leases against GC; no root or daemon.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .paths import HERE
from .processes import run as run_process
from .runtime_status import atomic_json, read_json


class Unavailable(RuntimeError):
    pass


class IntegrityFailure(Unavailable):
    pass


LOCK_WAIT_SECONDS = 1500  # fresh extraction can take up to 1200 seconds


def disk_reserve(usage):
    # A percentage alone reserves hundreds of GiB on large fleet disks and
    # disables sharing precisely when removing duplicate builds would help.
    # Keep the small-filesystem policy, capped at 20 GiB of actual free space.
    return min(int(usage.total * 0.1), 20 * 1024**3)


def root(env=None):
    env = os.environ if env is None else env
    login = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if value := env.get("TAUCETI_SHARED_MATHLIB_ROOT"):
        return (login / value[2:] if value.startswith("~/") else Path(value)).resolve()
    return (login / ".cache/tauceti/mathlib-builds").resolve()


def pin_root(env):
    env.setdefault("TAUCETI_SHARED_MATHLIB_ROOT", str(root(env)))


def selection(env):
    pool = root(env)
    path = pool / "policy.json"
    policy = read_json(path)
    if path.exists() and (
        not policy
        or policy.get("mode") not in ("off", "auto", "required")
        or not isinstance(policy.get("expires_at", 0), (int, float))
        or not math.isfinite(policy.get("expires_at", 0))
        or policy.get("expiry_mode", "off") not in ("off", "auto", "required")
        or not isinstance(policy.get("workers", []), list)
        or any(not isinstance(worker, str) for worker in policy.get("workers", []))
    ):
        if env.get("TAUCETI_SHARED_MATHLIB_BUILDS") == "required":
            raise Unavailable("invalid shared Mathlib policy; repair policy.json before requiring sharing")
        return "off", "invalid policy.json; repair before enabling sharing"
    expired = policy.get("expires_at", float("inf")) <= time.time()
    value = env.get(
        "TAUCETI_SHARED_MATHLIB_BUILDS", policy.get("expiry_mode", "off") if expired else policy.get("mode", "off")
    ).lower()
    if value not in {"auto", "off", "required"}:
        raise Unavailable("TAUCETI_SHARED_MATHLIB_BUILDS must be auto, off or required")
    allowed = env.get("TAUCETI_SHARED_MATHLIB_WORKERS", ",".join(policy["workers"]) if "workers" in policy else None)
    if allowed is not None and env.get("TAUCETI_WORKER_ID", "default") not in allowed.split(","):
        return "off", "worker is not allowlisted for shared Mathlib"
    stop_path = pool / "safety-stop.json"
    stop = read_json(stop_path)
    reason = stop.get("reason") or policy.get("safety_stop")
    if stop_path.exists() and (not isinstance(reason, str) or not reason):
        reason = "unreadable safety-stop.json"
    if reason:
        if value == "required":
            raise Unavailable(
                "shared Mathlib safety stop: "
                + reason
                + "; investigate before clearing safety-stop.json and restoring policy"
            )
        return "off", "shared Mathlib safety stop: " + reason
    reason = "sharing enabled"
    if value == "off":
        reason = (
            "sharing disabled by environment"
            if "TAUCETI_SHARED_MATHLIB_BUILDS" in env
            else "shared Mathlib policy expired"
            if expired
            else "sharing disabled by policy"
            if policy
            else "sharing disabled: no opt-in policy.json"
        )
    return value, reason


def mode(env):
    return selection(env)[0]


def overlay_preload():
    """Stock bwrap has no overlay-option flag; keep the cache-only shim local."""
    source = HERE / "scripts/OverlayMount.c"
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/tauceti/overlay-mount"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / (key + ".so")
    with (cache / "build.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            compiler = shutil.which("cc")
            if not compiler:
                raise Unavailable("sharing needs a C compiler once to build the cache overlay helper; install cc")
            with tempfile.TemporaryDirectory(dir=cache, prefix="build-") as temporary:
                built = Path(temporary) / "overlay.so"
                result = subprocess.run(
                    [compiler, "-shared", "-fPIC", "-O2", str(source), "-o", str(built), "-ldl"],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode:
                    raise Unavailable(
                        "cannot compile cache overlay helper: " + result.stderr.decode(errors="replace")[-400:]
                    )
                os.replace(built, target)
    return target


def overlay_command(lower, upper, work, target, pool, *, lease_fd=None):
    """A private mount namespace with the overlay view and a read-only pool.

    Deliberately no PID namespace: agents and their tools must see ordinary host PIDs and /proc.
    Tools that report a PID to a service outside the namespace (systemd, D-Bus) break otherwise."""
    original = os.environ.get("LD_PRELOAD")
    preload = str(overlay_preload()) + (":" + original if original else "")
    return [
        "env",
        "LD_PRELOAD=" + preload,
        "bwrap",
        *(["--setenv", "LD_PRELOAD", original] if original else ["--unsetenv", "LD_PRELOAD"]),
        "--die-with-parent",
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
                + [
                    "sh",
                    "-c",
                    'test "$(cat "$1/probe")" = shared && printf private > "$1/probe" '
                    '&& ! (printf poison > "$2/probe")',
                    "sh",
                    str(upper),
                    str(lower),
                ],
                capture_output=True,
                timeout=10,
            )
            if (lower / "probe").read_text() != "shared":
                return (
                    False,
                    "isolation failure: overlay probe modified its immutable lower; stop sharing and investigate",
                )
            if p.returncode or (upper / "probe").read_text() != "private":
                return (
                    False,
                    "rootless OverlayFS probe failed: "
                    + p.stderr.decode(errors="replace")[-400:].strip()
                    + "; administrator may need to allow unprivileged user namespaces or provide an OverlayFS-capable filesystem",
                )
        return True, "rootless OverlayFS works; immutable lower with private copy-on-write builds"
    except (Unavailable, OSError, subprocess.TimeoutExpired) as error:
        return (
            False,
            f"rootless OverlayFS unavailable: {error}; use a local OverlayFS-capable filesystem and allow user namespaces",
        )


def git(path, *args):
    p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise Unavailable("cannot identify clean dependency sources: " + p.stderr.strip()[-200:])
    return p.stdout.strip()


def optional_git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else None


def cache_context(path):
    """Git metadata used by Mathlib's cache selector; URLs stay private in memory."""
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    tracking = optional_git(path, "config", "--get", f"branch.{branch}.remote")
    selected = tracking or "origin"
    names = sorted({"origin", selected})
    raw = {name: optional_git(path, "config", "--get", f"remote.{name}.url") for name in names}
    resolved = {name: optional_git(path, "remote", "get-url", name) for name in names}
    return dict(
        branch=branch,
        tracking=tracking,
        selected=selected,
        raw_urls={name: url for name, url in raw.items() if url},
        resolved_urls=resolved,
    )


def cache_identity(context):
    # Bind the key to the repository scope without persisting credentials that
    # an operator may have embedded in a Git URL.
    return dict(
        branch=context["branch"],
        selected=context["selected"],
        remotes={
            name: hashlib.sha256(url.encode()).hexdigest() if url else None
            for name, url in context["resolved_urls"].items()
        },
    )


def signature(cwd, env):
    # Loading Lake synchronizes pinned dependency checkouts without compiling
    # Mathlib. Do this before deciding the key, so a manifest bump can hit a
    # shared snapshot on its first round instead of downloading privately first.
    lean = run_process(
        ["lake", "env", "lean", "--version"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
    )
    if lean.returncode:
        raise Unavailable("Lean toolchain is unavailable; run lake env lean --version")
    manifest = json.loads((cwd / "lake-manifest.json").read_text())
    packages = manifest.get("packages", [])
    mathlib = cwd / ".lake/packages/mathlib"
    if not mathlib.is_dir():
        raise Unavailable("Mathlib dependency is not checked out yet")
    dependencies, cache_contexts = {}, {}
    for package in packages:
        name = package.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name or package.get("type") != "git":
            raise Unavailable("shared snapshots require pinned git dependencies; path dependencies use private builds")
        source = cwd / ".lake/packages" / name
        revision = git(source, "rev-parse", "HEAD")
        if revision != package.get("rev") or git(source, "status", "--porcelain", "--untracked-files=normal"):
            raise Unavailable(f"dependency {name} differs from its pinned revision; using private builds")
        dependencies[name] = revision
        cache_contexts[name] = cache_identity(cache_context(source))
    if "mathlib" not in dependencies:
        raise Unavailable("manifest has no pinned Mathlib dependency")
    toolchain = (cwd / "lean-toolchain").read_text().strip()
    prefix = run_process(
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
    # Temporary LAKE_CONFIG filenames and private download directories are not
    # cache selectors. Bind their content/selection settings, without publishing
    # endpoint credentials in ready.json.
    selection = {
        name: hashlib.sha256(value.encode()).hexdigest()
        for name, value in env.items()
        if (
            name.startswith(("LAKE_", "MATHLIB_"))
            and name not in {"LAKE_CONFIG", "LAKE_CACHE_DIR", "MATHLIB_CACHE_DIR"}
        )
        or name in {"ELAN_TOOLCHAIN", "LEAN_SYSROOT", "LEAN_PATH", "LEAN_SRC_PATH"}
    }
    lake_config = Path(env["LAKE_CONFIG"]) if env.get("LAKE_CONFIG") else None
    if lake_config and not lake_config.is_absolute():
        lake_config = cwd / lake_config
    spec = dict(
        version=3,
        artifacts=artifacts,
        mathlib=dependencies["mathlib"],
        dependencies=dependencies,
        cache_contexts=cache_contexts,
        config=config,
        mathlib_manifest=(mathlib / "lake-manifest.json").read_text(),
        toolchain=toolchain,
        abi=lean.stdout.strip(),
        resolved_toolchain=str(library.parent.parent.resolve()),
        toolchain_init=hashlib.sha256((library / "Init.olean").read_bytes()).hexdigest(),
        lake_config=hashlib.sha256(lake_config.read_bytes()).hexdigest() if lake_config else None,
        cache_selection=selection,
        architecture=platform.machine(),
        system=platform.system(),
    )
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), spec


@contextlib.contextmanager
def key_lock(pool, key, exclusive=True, nonblocking=False, timeout=None, readonly=False):
    locks = pool / "locks"
    path = locks / (key + ".lock")
    if readonly and not path.exists():
        yield None
        return
    if not readonly:
        locks.mkdir(parents=True, exist_ok=True)
    with path.open("r" if readonly else "a+") as stream:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        lease_lock(stream, flags | (fcntl.LOCK_NB if nonblocking else 0), timeout=timeout)
        if not readonly:
            yield stream
            return
    yield None  # dry-run eligibility probe; release before any recursive inspection


def lease_lock(stream, flags, *, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        try:
            fcntl.flock(stream, flags | (fcntl.LOCK_NB if deadline is not None else 0))
            return
        except BlockingIOError:
            if deadline is None:
                raise
            if time.monotonic() >= deadline:
                raise Unavailable(
                    "shared Mathlib lock wait timed out; use private mode and inspect publisher/reader leases"
                ) from None
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except OSError as error:
            raise IntegrityFailure(f"shared Mathlib lease failure: {error}; stop sharing and investigate") from error


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


def upper_usage(upper):
    try:
        files = [path.lstat() for path in upper.rglob("*")]
        regular = [info for info in files if stat.S_ISREG(info.st_mode)]
        return dict(upper_files=len(regular), upper_bytes=sum(info.st_size for info in regular))
    except OSError:
        return dict(upper_files=None, upper_bytes=None)


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


def inventory(build):
    """Fingerprint trusted local files without rereading gigabytes on every hit.

    ctime detects overwrites even if size and mtime are restored. Readers cannot
    change these files through the read-only pool mount. The host owner remains
    trusted, as with the existing cache (this is not a hostile-host boundary).
    Reject links and special files, including symlinked directories.
    """
    result = {}
    device = build.stat().st_dev
    for here, directories, files in os.walk(build):
        for path in [Path(here), *(Path(here) / name for name in directories + files)]:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise Unavailable(f"unsupported snapshot file {path.relative_to(build)}")
            if info.st_dev != device or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise Unavailable(f"snapshot file has a mount or hardlink alias: {path.relative_to(build)}")
            if stat.S_ISREG(info.st_mode):
                result[str(path.relative_to(build))] = [
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
    return result


def ready_entry(entry, key, spec):
    """Called with a lease. Preserve a damaged published lower for investigation."""
    if not entry.exists():
        return False
    ready = read_json(entry / "ready.json")
    try:
        valid = (
            ready.get("key") == key
            and ready.get("spec") == spec
            and bool(ready.get("inventory"))
            and (entry / "build").is_dir()
            and not (entry / "build").is_symlink()
            and inventory(entry / "build") == ready["inventory"]
        )
    except (OSError, Unavailable) as error:
        raise IntegrityFailure(
            f"snapshot integrity failure for {key}: {error}; retain lower and use private mode"
        ) from error
    if not valid:
        raise IntegrityFailure(f"snapshot integrity failure for {key}; retain lower and use private mode")
    return True


def stop_sharing(pool, reason):
    """Stop future rounds; never disturb existing leases or immutable readers."""
    from .config import log

    recorded = False
    try:
        atomic_json(pool / "safety-stop.json", dict(reason=reason, stopped_at=time.time()))
        recorded = True
    except OSError as error:
        log(f"shared Mathlib SAFETY STOP receipt could not be written: {error}")
    try:
        with key_lock(pool, "policy", timeout=1):
            policy = read_json(pool / "policy.json")
            atomic_json(pool / "policy.json", {**policy, "mode": "off"})
            recorded = True
    except (OSError, Unavailable) as error:
        log(f"shared Mathlib SAFETY STOP policy could not be written: {error}")
    if recorded:
        try:
            fd = os.open(pool, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as error:
            log(f"shared Mathlib SAFETY STOP directory sync unavailable: {error}")
    return recorded


def discard_entry(pool, entry):
    """Hide an unlocked entry atomically before deletion can be interrupted."""
    trash = pool / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    retired = trash / (entry.name + "-" + uuid.uuid4().hex)
    os.replace(entry, retired)
    for directory in (entry.parent, trash):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    remove_tree(retired)


def collect(pool, *, protect=(), now=None, max_age=7 * 86400, dry_run=False):
    try:
        with key_lock(pool, "gc", nonblocking=True, readonly=dry_run):
            return _collect(pool, protect=protect, now=now, max_age=max_age, dry_run=dry_run)
    except BlockingIOError:
        return []


def _collect(pool, *, protect=(), now=None, max_age=7 * 86400, dry_run=False):
    """Bounded by snapshot count. Locked readers/builders are never reclaimed."""
    now = time.time() if now is None else now
    entries = pool / "entries"
    if dry_run:
        if not entries.is_dir():
            return []
    else:
        entries.mkdir(parents=True, exist_ok=True)
    for retired in (pool / "trash").glob("*"):
        with contextlib.suppress(FileNotFoundError):
            if not dry_run:
                remove_tree(retired)
    # A killed builder cannot run TemporaryDirectory cleanup. Reclaim its
    # staging tree before evicting useful published snapshots for disk pressure.
    for stage in (pool / "staging").glob("*"):
        key = stage.name.split("-", 1)[0]
        if key in protect or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            continue
        try:
            if not stage.is_dir() or now - stage.stat().st_mtime < 4 * 3600:
                continue
            with key_lock(pool, key, nonblocking=True, readonly=dry_run):
                if not dry_run:
                    remove_tree(stage)
        except (BlockingIOError, FileNotFoundError):
            pass
    candidates = sorted((p for p in entries.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    removed = []
    for entry in candidates:
        if entry.name in protect:
            continue
        usage = shutil.disk_usage(pool)
        if now - entry.stat().st_mtime < max_age and usage.free >= disk_reserve(usage):
            continue
        try:
            with key_lock(pool, entry.name, nonblocking=True, readonly=dry_run):
                if not dry_run:
                    discard_entry(pool, entry)
                removed.append(entry.name)
        except (BlockingIOError, FileNotFoundError):
            pass
    return removed


def clone(source, target, revision):
    context = cache_context(source)
    for command in (
        ["git", "clone", "--shared", "--quiet", "--no-checkout", str(source), str(target)],
        ["git", "-C", str(target), "checkout", "--quiet", "--detach", revision],
    ):
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise Unavailable("fresh snapshot checkout failed: " + result.stderr.strip()[-300:])
    # The local clone's origin points into the mutable workspace. Reconstruct
    # the source's cache-selection context, never a writable push path to it.
    git(target, "remote", "remove", "origin")
    for name, url in context["raw_urls"].items():
        git(target, "remote", "add", name, url)
        git(target, "remote", "set-url", "--push", name, "no_push")
    if context["branch"] != "HEAD":
        git(target, "checkout", "--quiet", "-B", context["branch"], revision)
    if context["tracking"]:
        git(target, "config", f"branch.{context['branch']}.remote", context["tracking"])
    if cache_identity(cache_context(target)) != cache_identity(context):
        raise Unavailable(
            "Git cache selection context differs in the fresh clone; normalize repository-specific URL rules or use private builds"
        )


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
    if ready_entry(entry, key, spec):
        os.utime(entry, None)
        return entry, True
    collect(pool, protect=(key,))
    usage = shutil.disk_usage(pool)
    if usage.free < disk_reserve(usage):
        raise Unavailable(
            "shared snapshot pool is below its free-space reserve (10%, capped at 20 GiB) and no unused snapshots can be removed"
        )
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
            # Keep the pool read-only for the extraction tool; only its trusted
            # staging project is writable. This optional mount view has no PID gate.
            prefix = [
                "bwrap",
                "--die-with-parent",
                "--bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "--ro-bind",
                str(pool),
                str(pool),
                "--bind",
                str(project),
                str(project),
                "--",
            ]
            rc = fetch([*prefix, "lake", "exe", "cache", "get"], project, env, output, 1200)
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
        files = inventory(ready / "build")
        durable_tree(ready / "build")
        atomic_json(
            ready / "ready.json",
            dict(key=key, spec=spec, files_verified=count, inventory=files, published_at=time.time()),
        )
        collect(pool, protect=(key,))
        usage = shutil.disk_usage(pool)
        if usage.free < disk_reserve(usage):
            raise Unavailable(
                "snapshot extraction would leave less than the free-space reserve (10%, capped at 20 GiB); using private builds"
            )
        entry.parent.mkdir(parents=True, exist_ok=True)
        os.replace(ready, entry)
        fd = os.open(entry.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry, False


def process_identity(pid):
    try:
        if platform.system() == "Linux":
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return dict(pid=pid, start=fields[19], boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        os.kill(pid, 0)
        return dict(pid=pid)
    except (OSError, ValueError, IndexError):
        return None


def report(pool, env, **fields):
    worker = env.get("TAUCETI_WORKER_ID", "default")
    identity = hashlib.sha256(worker.encode()).hexdigest()
    from .activity import current_round_id

    try:
        atomic_json(
            pool / "status" / (identity + ".json"),
            dict(
                worker=worker,
                scope=env.get("TAUCETI_FLEET_MACHINE_ID", "local"),
                round_id=current_round_id(),
                supervisor=process_identity(os.getpid()),
                observed_at=time.time(),
                **fields,
            ),
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
    for row in rows:
        supervisor = row.get("supervisor")
        if row.get("mode") in {"preparing", "shared", "overlay-ready"} and (
            not isinstance(supervisor, dict)
            or not isinstance(supervisor.get("pid"), int)
            or process_identity(supervisor["pid"]) != supervisor
        ):
            row.update(mode="idle", reason="shared preparation supervisor exited; namespace leases remain protected")
        if row.get("mode") in {"shared", "overlay-ready"} and row.get("key"):
            if not isinstance(row["key"], str) or not re.fullmatch("[0-9a-f]{64}", row["key"]):
                row.update(mode="blocked", reason="invalid shared Mathlib key in preparation receipt")
                continue
            try:
                with key_lock(pool, row["key"], nonblocking=True):
                    row.update(mode="idle", reason="shared lease no longer active")
            except BlockingIOError:
                pass  # Includes a namespace retaining its lease after supervisor death.
            except (OSError, Unavailable):
                row.update(mode="blocked", reason="shared Mathlib lease status unavailable")
    return dict(
        shared=sum(r.get("mode") == "shared" for r in rows),
        private=sum(r.get("mode") == "private" for r in rows),
        cache_hits=sum(r.get("cache_hit", False) for r in rows),
        preparing=sum(r.get("mode") in {"preparing", "overlay-ready"} for r in rows),
        blocked=sum(r.get("mode") == "blocked" for r in rows),
        idle=sum(r.get("mode") == "idle" for r in rows),
        preparation_seconds=sum(r.get("preparation_seconds", 0) for r in rows),
        reasons=sorted({r["reason"] for r in rows if r.get("reason")}),
    )


@contextlib.contextmanager
def view(cwd, env, logdir, fetch):
    """Yield an argv prefix and keep the lower leased until the whole agent exits."""
    pool = root(env)
    try:
        selected, selection_reason = selection(env)
    except Unavailable as error:
        report(pool, env, mode="blocked", reason=str(error))
        raise
    started = time.monotonic()
    upper = cwd / ".lake/packages/mathlib/.lake/build"
    marker = upper.parent / "tauceti-shared-mathlib.json"
    if selected == "off" or not (cwd / ".lake/packages/mathlib").is_dir():
        # Normal private cache warming reconstructs any files previously visible
        # only in the lower. Clearing the marker forces a fresh empty upper when
        # sharing is enabled again at a later idle boundary.
        detach_upper(upper, marker)
        if selected != "off" and selected == "required":
            report(pool, env, mode="blocked", reason="Mathlib dependency is not available for sharing")
            raise Unavailable("Mathlib dependency is not available for sharing")
        report(
            pool,
            env,
            mode="private",
            reason=selection_reason if selected == "off" else "Mathlib dependency is not available for sharing",
        )
        yield []
        return
    prefix, backup, backup_created, backup_sparse = [], None, False, False
    safety_stop_recorded = None
    with contextlib.ExitStack() as stack:
        try:
            report(pool, env, mode="preparing", reason="checking namespace capability and dependency identity")
            pool.mkdir(parents=True, exist_ok=True, mode=0o700)
            ok, reason = capability(pool)
            if not ok:
                if reason.startswith("isolation failure"):
                    raise IntegrityFailure(reason)
                raise Unavailable(reason)
            key, spec = signature(cwd, env)
            report(pool, env, mode="preparing", key=key)
            lease = stack.enter_context(key_lock(pool, key, exclusive=False, timeout=LOCK_WAIT_SECONDS))
            entry = pool / "entries" / key
            hit = ready_entry(entry, key, spec)
            if hit:
                os.utime(entry, None)
            else:
                # Serialize publishers separately: a waiting first user must not
                # wait for the winner's entire paid round to release its lease.
                lease_lock(lease, fcntl.LOCK_UN)
                with key_lock(pool, key + "-publisher", timeout=LOCK_WAIT_SECONDS):
                    lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
                    hit = ready_entry(entry, key, spec)
                    if not hit:
                        lease_lock(lease, fcntl.LOCK_UN)
                        lease_lock(lease, fcntl.LOCK_EX, timeout=LOCK_WAIT_SECONDS)
                        entry, hit = publish(pool, key, spec, cwd, env, logdir, fetch)
                        lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
            upper.parent.mkdir(parents=True, exist_ok=True)
            ok, reason = capability(upper.parent)
            if not ok:
                if reason.startswith("isolation failure"):
                    raise IntegrityFailure(reason)
                raise Unavailable(reason)
            # Prior copies and whiteouts must not shadow the lower next round.
            detach_upper(upper, marker)
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
                atomic_json(marker, dict(key=key, pool=str(pool), activated_at=time.time()))
            work.mkdir(exist_ok=True)
            candidate = overlay_command(entry / "build", upper, work, upper, pool, lease_fd=lease.fileno())
            probe = subprocess.run(
                candidate + ["sh", "-c", 'test -f "$1/lib/lean/Mathlib.olean" || exit 42', "sh", str(upper)],
                capture_output=True,
                timeout=15,
                pass_fds=(lease.fileno(),),
            )
            if probe.returncode == 42:
                raise IntegrityFailure("verified lower missing through fresh overlay; stop sharing and investigate")
            if probe.returncode:
                raise Unavailable("shared Mathlib view unavailable: " + probe.stderr.decode(errors="replace")[-300:])
            lease_lock(lease, fcntl.LOCK_SH, timeout=LOCK_WAIT_SECONDS)
            prefix = candidate
            env["TAUCETI_SHARED_MATHLIB_LEASE_FD"] = str(lease.fileno())
            env["TAUCETI_SHARED_MATHLIB_KEY"] = key
            env["TAUCETI_SHARED_MATHLIB_PREP_STARTED"] = str(started)
            report(
                pool, env, mode="overlay-ready", key=key, cache_hit=hit, preparation_seconds=time.monotonic() - started
            )
        except (Unavailable, OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, IntegrityFailure):
                safety_stop_recorded = stop_sharing(pool, str(error))
            if backup_created and backup and backup.exists():
                remove_tree(upper)
                if backup_sparse:
                    upper.mkdir(parents=True)
                else:
                    os.replace(backup, upper)
                marker.unlink(missing_ok=True)
            else:
                detach_upper(upper, marker)
            report(
                pool,
                env,
                mode="blocked" if selected == "required" else "private",
                reason=str(error),
                preparation_seconds=time.monotonic() - started,
                safety_stop_recorded=safety_stop_recorded,
            )
            stack.close()
            env.pop("TAUCETI_SHARED_MATHLIB_LEASE_FD", None)
            env.pop("TAUCETI_SHARED_MATHLIB_KEY", None)
            env.pop("TAUCETI_SHARED_MATHLIB_PREP_STARTED", None)
            if selected == "required":
                raise Unavailable(str(error)) from error
            prefix = []
        # The body is outside the preparation handler: never retry paid work.
        body_error = None
        cleanup_error = None
        try:
            yield prefix
        except BaseException as error:
            body_error = type(error).__name__
            raise
        finally:
            if prefix:
                if body_error is None and backup and backup.exists():
                    try:
                        remove_tree(backup)
                    except OSError as error:
                        cleanup_error = str(error)
                stack.close()
                record_result(
                    env,
                    None,
                    phase="cleanup",
                    backup_retained=bool(backup and backup.exists()),
                    body_error=body_error,
                    cleanup_error=cleanup_error,
                    cleanup_success=body_error is None and cleanup_error is None,
                    safety_stop_recorded=safety_stop_recorded,
                    **upper_usage(upper),
                )
                previous = read_json(
                    pool
                    / "status"
                    / (hashlib.sha256(env.get("TAUCETI_WORKER_ID", "default").encode()).hexdigest() + ".json")
                )
                if previous.get("mode") != "blocked":
                    report(
                        pool,
                        env,
                        mode="blocked" if cleanup_error else "idle",
                        key=env["TAUCETI_SHARED_MATHLIB_KEY"],
                        reason=cleanup_error
                        or read_json(pool / "safety-stop.json").get("reason")
                        or "shared view released; awaiting next natural round",
                        preparation_seconds=previous.get("preparation_seconds", 0),
                        cache_hit=previous.get("cache_hit", False),
                    )
            env.pop("TAUCETI_SHARED_MATHLIB_LEASE_FD", None)
            env.pop("TAUCETI_SHARED_MATHLIB_KEY", None)
            env.pop("TAUCETI_SHARED_MATHLIB_PREP_STARTED", None)


def preparation_status(env, *, error=None):
    """Mark ready only after both cache warmers pass, before any inference starts."""
    worker = env.get("TAUCETI_WORKER_ID", "default")
    pool = root(env)
    previous = read_json(pool / "status" / (hashlib.sha256(worker.encode()).hexdigest() + ".json"))
    elapsed = time.monotonic() - float(env.get("TAUCETI_SHARED_MATHLIB_PREP_STARTED", time.monotonic()))
    report(
        pool,
        env,
        mode="blocked" if error else "shared",
        reason=str(error) if error else None,
        key=env.get("TAUCETI_SHARED_MATHLIB_KEY"),
        cache_hit=previous.get("cache_hit", False),
        preparation_seconds=elapsed,
    )


def lease_fds(env):
    value = env.get("TAUCETI_SHARED_MATHLIB_LEASE_FD")
    return (int(value),) if value is not None else ()


def record_result(env, code, *, phase="invocation", **fields):
    key = env.get("TAUCETI_SHARED_MATHLIB_KEY")
    if not key:
        return
    from .activity import current_round_id

    pool = root(env)
    previous = read_json(
        pool / "status" / (hashlib.sha256(env.get("TAUCETI_WORKER_ID", "default").encode()).hexdigest() + ".json")
    )
    record = dict(
        at=time.time(),
        worker=env.get("TAUCETI_WORKER_ID", "default"),
        round_id=current_round_id(),
        key=key,
        exit_code=code,
        phase=phase,
        preparation_seconds=previous.get("preparation_seconds"),
        cache_hit=previous.get("cache_hit"),
        **fields,
    )
    try:
        with (pool / "results.jsonl").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        from .config import log

        log("shared Mathlib result receipt unavailable; paid invocation exit status preserved")

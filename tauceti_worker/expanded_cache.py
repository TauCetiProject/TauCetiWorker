"""Expand CI archives once, retaining private Lake metadata and native hash invalidation.

Only the host's fresh `lake cache get` manifest is published, never an agent's outputs.
Cooperative same-user workers share read-only files; this is not a security boundary.
Entry locks cover publication/hydration/eviction, not paid rounds. Lake's ordinary
read-only artifact hard links keep hydrated files alive independently of pool GC.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .config import log
from .paths import HERE
from .runtime_status import read_json

HASH = re.compile(r"[0-9a-f]{16}")
ARTIFACT = re.compile(r"[0-9a-f]{16}\.(?:ltar|olean(?:\.server|\.private)?|ilean|ir(?:\.sig)?|c|bc)")
SCHEMA = "2026-02-25"


class Unavailable(RuntimeError):
    """Shared preparation failed; continuing would leave this round with private copies."""


class SharedLinkUnavailable(Unavailable):
    """A hard link into or out of the pool failed. Copying instead would duplicate the bytes."""


class CorruptArchive(ValueError):
    def __init__(self, name):
        self.name = name
        super().__init__(f"CI archive hash mismatch: {name}")


def root(env=None):
    env = os.environ if env is None else env
    return Path(
        env.get("TAUCETI_BUILD_CACHE_ROOT") or (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/tauceti/builds")
    )


def lock_path(pool, key):
    namespace = "entry" if HASH.fullmatch(key) else key.split("-", 1)[0]
    return pool / "locks" / (namespace + "-" + hashlib.sha256(key.encode()).hexdigest()[:3] + ".lock")


@contextlib.contextmanager
def locked_all(pool, keys, *, deadline):
    """Hold the exclusive locks of many keys: each shard once, in one global order.

    Keys share 4096 shards, so one batch often maps two keys to one shard; taking that shard
    twice through two descriptors would wait on itself, and unordered batches can form cycles.
    """
    with contextlib.ExitStack() as stack:
        for path in sorted({lock_path(pool, key) for key in keys}):
            stack.enter_context(_flock(path, deadline=deadline))
        yield


@contextlib.contextmanager
def _flock(path, *, deadline):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        yield


@contextlib.contextmanager
def locked(pool, key, *, readonly=False, deadline=None):
    """Exclusive entry lock; with a monotonic `deadline`, wait for it instead of failing at once."""
    directory = pool / "locks"
    path = lock_path(pool, key)
    if readonly and not path.exists():
        yield
        return
    if not readonly:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("r" if readonly else "a+") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        yield


def atomic_json(path, value):
    # These are disposable cache indexes, not accounting/source records. Atomic rename
    # protects readers; fsync per module would serialize tens of thousands of writes.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        json.dump(value, out)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def link(source, target):
    """Hard-link, never copy: a copy would be a second private instance of shared bytes.

    A vanished source stays `FileNotFoundError` (a GC race the caller may treat as a miss).
    Only creates a new name (an existing target is kept), so it never replaces shared bytes and
    needs no `canonical_store.Guard`.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except (FileExistsError, FileNotFoundError) as error:
        if isinstance(error, FileNotFoundError):
            raise
    except OSError as error:
        raise SharedLinkUnavailable(f"cannot hard-link {source} -> {target}: {error}") from error


def native(cwd, env, pool):
    clean = dict(env, LEAN_PATH="")
    clean.pop("LEAN_SYSROOT", None)
    prefix = Path(
        subprocess.check_output(["lean", "--print-prefix"], cwd=cwd, env=clean, text=True, timeout=10).strip()
    )
    driver = HERE / "scripts/CacheHash.lean"
    info = (prefix / "bin/lean").stat()
    libraries = [
        (str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in (prefix / "lib/lean").glob("libLake_shared.*")
    ]
    key = hashlib.sha256(
        driver.read_bytes() + str((prefix, info.st_size, info.st_mtime_ns, libraries)).encode()
    ).hexdigest()
    target = pool / "helpers" / key
    if target.is_file():
        os.utime(target, None)
        return target
    failed = target.with_suffix(".failed.json")
    if read_json(failed).get("until", 0) > time.time():
        raise OSError("shared-cache helper unavailable for this toolchain (retried hourly)")
    with locked(pool, "helper-" + key):
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                tempfile.TemporaryDirectory(dir=target.parent, prefix="build-") as tmp,
                contextlib.ExitStack() as leases,
            ):
                work = Path(tmp)
                leases.enter_context(locked(pool, "helperstage-" + work.name))
                shutil.copyfile(driver, work / "Main.lean")
                subprocess.run(
                    [str(prefix / "bin/lean"), "-c", "Main.c", "Main.lean"],
                    cwd=work,
                    env=clean,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    [
                        str(prefix / "bin/leanc"),
                        "-O1",
                        "-o",
                        "hash",
                        "Main.c",
                        "-lLake_shared",
                        f"-Wl,-rpath,{prefix / 'lib/lean'}",
                    ],
                    cwd=work,
                    env=clean,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                os.replace(work / "hash", target)  # Our helper binary; no build output.
        except (OSError, subprocess.SubprocessError):
            atomic_json(failed, dict(until=time.time() + 3600))
            raise
    return target


def hashes(program, paths, directory):
    request = directory / "hash-request.json"
    request.write_text(json.dumps([str(p) for p in paths]))
    result = subprocess.check_output([str(program), str(request)], text=True, timeout=45)
    return {path: f"{value:016x}" for path, value in map(json.loads, result.splitlines())}


def descriptions(data):
    """Validate precisely the native module-output shape this adapter understands."""
    if not isinstance(data, dict) or not isinstance(data.get("o"), list) or not data["o"] or "i" not in data:
        raise ValueError("unsupported module outputs")
    result = []
    for key in ("o", "i", "rs", "r", "c", "b", "l"):
        values = data.get(key, [])
        for name in values if isinstance(values, list) else [values]:
            if not isinstance(name, str) or not ARTIFACT.fullmatch(name):
                raise ValueError("unsafe artifact description")
            result.append(name)
    return result


def clock():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.monotonic(), own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def record_phase(report, name, started):
    if report is not None:
        wall, cpu = clock()
        row = report.setdefault("phases", {}).setdefault(name, dict(seconds=0, cpu_seconds=0))
        row["seconds"] = round(row["seconds"] + wall - started[0], 6)
        row["cpu_seconds"] = round(row["cpu_seconds"] + cpu - started[1], 6)


@contextlib.contextmanager
def measured(report, name):
    started = clock()
    try:
        yield
    finally:
        record_phase(report, name, started)


def mappings(download):
    # This download directory contains only fresh native CI metadata, never a
    # worker's locally compiled output mappings. Keep it in memory for this pass.
    try:
        return {
            path.stem: read_json(path)
            for path in (download / "outputs/TauCeti").glob("*.json")
            if HASH.fullmatch(path.stem)
        }
    except OSError as error:
        log(f"expanded cache metadata unavailable; retaining private fallback ({error})")
        return {}


def revision_mtime(path):
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0  # Another preparation trimmed this optional revision hint.


def entries(download, manifest=None):
    for key, row in (mappings(download) if manifest is None else manifest).items():
        data = row.get("data")
        if isinstance(data, str) and ARTIFACT.fullmatch(data) and data.endswith(".ltar"):
            yield key, data


def private_mapping(private, key, known):
    if key not in known:
        known[key] = read_json(private / "outputs/TauCeti" / (key + ".json"))
    return known[key]


def hydrate(entry, private, input_hash, *, private_mappings=None, report=None, guard=None):
    row = read_json(entry / "entry.json")
    names = list(dict.fromkeys(descriptions(row["data"])))
    try:
        if not all(stat.S_ISREG((entry / name).lstat().st_mode) for name in names):
            raise ValueError("incomplete expanded entry")
    except FileNotFoundError:
        raise ValueError("incomplete expanded entry") from None
    desired = dict(schemaVersion=SCHEMA, service=None, data=row["data"])
    keys = input_hash if isinstance(input_hash, list) else [input_hash]
    known = {} if private_mappings is None else private_mappings
    changed = [key for key in keys if private_mapping(private, key, known) != desired]
    from .canonical_store import Guard, reconcile

    guard = Guard() if guard is None else guard
    for name in names:
        # A local publication may have made this name canonical first: keep one inode.
        reconcile(entry.parent.parent, name, entry / name, guard=guard)
    missing = [name for name in names if not (private / "artifacts" / name).is_file()]
    started = clock()
    for name in missing:
        link(entry / name, private / "artifacts" / name)
    for key in changed:
        atomic_json(private / "outputs/TauCeti" / (key + ".json"), desired)
        known[key] = desired
    if report is not None:
        report["installed_artifacts"] = report.get("installed_artifacts", 0) + len(missing)
        report["written_mappings"] = report.get("written_mappings", 0) + len(changed)
        record_phase(report, "install", started)
    # Explicit reuse timestamp: archive mtimes and filesystem atime are not LRU.
    if time.time() - (entry / "entry.json").stat().st_mtime > 60:
        os.utime(entry / "entry.json", None)


# Bounded by the host's single preparation deadline (agents.HOST_CACHE_TIMEOUT).
EXPANSION_TIMEOUT = 1200


def expand(download, private, cwd, env, pool, report, *, manifest=None, private_mappings=None, deadline=None):
    """Expand and hydrate every selected archive, or raise: partial hydration is not a fallback."""
    grouped = {}
    for input_hash, archive in entries(download, manifest):
        grouped.setdefault(archive, []).append(input_hash)
    pending = [(inputs, archive) for archive, inputs in grouped.items()]
    if not pending:
        return
    deadline = time.monotonic() + EXPANSION_TIMEOUT if deadline is None else deadline
    program = None
    completed = 0
    from .canonical_store import Guard, reconcile

    guard = Guard()  # One lazy /proc scan for every alias this expansion may replace.
    for offset in range(0, len(pending), 128):
        with contextlib.ExitStack() as stack:
            missing = []
            batch = pending[offset : offset + 128]
            try:
                # Another publisher is useful work: wait for it, then hydrate its entry.
                stack.enter_context(locked_all(pool, [archive[:-5] for _, archive in batch], deadline=deadline))
            except BlockingIOError:
                raise Unavailable("expansion entries still locked at the preparation deadline") from None
            for input_hash, archive in batch:
                key = archive[:-5]
                entry = pool / "entries" / key
                try:
                    if (entry / "entry.json").is_file():
                        hydrate(
                            entry, private, input_hash, private_mappings=private_mappings, report=report, guard=guard
                        )
                        report["hits"] += 1
                        completed += 1
                    else:
                        missing.append((input_hash, archive, entry))
                except (ValueError, KeyError) as error:
                    log(f"expanded cache: replacing unusable entry {key}: {error}")
                    if entry.is_dir() and not entry.is_symlink():
                        # Only this invalid entry's alias names go, under its exclusive entry
                        # lock; every linked inode survives in its other names.
                        shutil.rmtree(entry)
                    missing.append((input_hash, archive, entry))
            if not missing:
                continue
            if program is None:
                program = native(cwd, env, pool)
            staging = pool / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=staging, prefix="expand-") as tmp:
                work = Path(tmp)
                stack.enter_context(locked(pool, "stage-" + work.name))
                verified = hashes(program, [download / "artifacts" / a for _, a, _ in missing], work)
                jobs = []
                for input_hash, archive, _ in missing:
                    if verified[str(download / "artifacts" / archive)] != archive[:-5]:
                        raise CorruptArchive(archive)
                    base = work / archive[:-5]
                    (base / "lib").mkdir(parents=True)
                    (base / "ir").mkdir()
                    jobs.append(
                        dict(
                            file=str(download / "artifacts" / archive),
                            hash=input_hash[0],
                            base=[str(base / "lib"), str(base / "ir")],
                        )
                    )
                prefix = Path(
                    subprocess.check_output(["lean", "--print-prefix"], cwd=cwd, env=env, text=True, timeout=10).strip()
                )
                subprocess.run(
                    [str(prefix / "bin/leantar"), "--jobs", "4", "-x", "-j", "-"],
                    input=json.dumps(jobs),
                    text=True,
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                files = [p for p in work.rglob("*") if p.is_file() and p.suffix not in (".trace", ".hash", ".json")]
                computed = hashes(program, files, work)
                for input_hash, archive, entry in missing:
                    base = work / archive[:-5]
                    traces = list(base.rglob("*.trace"))
                    if len(traces) != 1:
                        raise ValueError("archive must contain one module trace")
                    data = read_json(traces[0]).get("outputs")
                    names = descriptions(data)
                    artifacts = {
                        computed[str(p)] + "." + p.name.split(".", 1)[1]: p
                        for p in base.rglob("*")
                        if str(p) in computed
                    }
                    data["l"] = archive
                    names = descriptions(data)
                    publication = base / "publish"
                    publication.mkdir()
                    artifacts[archive] = download / "artifacts" / archive
                    for name in names:
                        # Deduplicate identical outputs across archive bundles as well as workers.
                        shared = pool / ("archives" if name.endswith(".ltar") else "artifacts") / name
                        # An identical canonical object (a local build published first) wins.
                        reconcile(pool, name, artifacts[name], guard=guard)
                        # Create a publication link FIRST, so a newly published canonical
                        # artifact never appears to GC as an unreferenced single link.
                        link(artifacts[name], publication / name)
                        link(publication / name, shared)
                        shared.chmod(0o444)  # Only ever removes write permission.
                        reconcile(pool, name, shared, guard=guard)
                        if shared.stat().st_ino != (publication / name).stat().st_ino:
                            (publication / name).unlink()  # Our staging name.
                            link(shared, publication / name)
                    atomic_json(publication / "entry.json", dict(data=data))
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(publication, entry)  # A new entry name from our staging directory.
                    hydrate(entry, private, input_hash, private_mappings=private_mappings, report=report, guard=guard)
                    report["expanded"] += 1
                    completed += 1
    if completed != len(pending):
        raise Unavailable(f"expanded {completed} of {len(pending)} selected archives")


def transfer(download, private, seeded=(), *, manifest=None, private_mappings=None):
    """Preserve downloads on unsupported toolchains, without linking mutable metadata."""
    manifest = mappings(download) if manifest is None else manifest
    known = {} if private_mappings is None else private_mappings
    wanted = {name for _, name in entries(download, manifest)}
    for row in manifest.values():
        if isinstance(row.get("data"), dict):
            with contextlib.suppress(ValueError):
                wanted.update(descriptions(row["data"]))
    for directory in ("artifacts", "outputs", "revisions"):
        for source in (download / directory).rglob("*"):
            if directory == "artifacts" and source.name not in wanted and source.name in seeded:
                continue
            if source.is_symlink() or not source.is_file():
                continue
            target = private / directory / source.relative_to(download / directory)
            if directory == "artifacts":
                link(source, target)
                continue
            if source.suffix != ".json":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                continue
            key = source.stem if source.parent == download / "outputs/TauCeti" else None
            before = private_mapping(private, key, known) if key in manifest else read_json(target)
            if directory == "outputs" and isinstance(before.get("data"), dict):
                try:
                    descriptions(before["data"])
                    continue  # Preserve this worker's valid expanded native mapping.
                except ValueError:
                    pass  # Repair malformed private mappings using the fresh download.
            target.parent.mkdir(parents=True, exist_ok=True)
            data = manifest[key] if key in manifest else read_json(source)
            if not data:
                continue  # Interrupted/malformed fresh metadata must not replace good private state.
            if before != data:
                atomic_json(target, data)
            if key in manifest:
                known[key] = data


@contextlib.contextmanager
def preparation(cwd, env, *, refetch=None, report=None):
    """Use fresh CI metadata, reusing existing archive bytes without trusting agent mappings.

    Every failure after the operator opt-out raises `Unavailable`: a round that silently kept
    private copies would undo the one-copy-per-host invariant. `CorruptArchive` refetches once.
    """
    if env.get("TAUCETI_SHARED_ARTIFACTS", "1") == "0" or not env.get("LAKE_CACHE_DIR"):
        yield env
        return
    private = Path(env["LAKE_CACHE_DIR"])
    pool = root(env)
    staging = pool / "downloads"
    try:
        from .cache_storage import initialize

        initialize(pool)
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        same_filesystem(pool, private, cwd)
        temporary = tempfile.TemporaryDirectory(dir=staging, prefix="prepare-")
    except Unavailable:
        raise
    except (OSError, ValueError) as error:
        raise Unavailable(f"expanded cache unavailable: {error}") from error
    started = time.monotonic()
    deadline = started + EXPANSION_TIMEOUT
    report = {} if report is None else report
    report.update(hits=0, expanded=0)
    with temporary as tmp, contextlib.ExitStack() as leases:
        try:
            leases.enter_context(locked(pool, "download-" + Path(tmp).name))
        except OSError as error:
            raise Unavailable(f"expanded cache download lock unavailable: {error}") from error
        download = Path(tmp)
        # Pool contains only previously verified CI artifacts; private .ltars are also
        # candidates, but the native verifier checks their hash before publication.
        seeded = set()
        seed_started = clock()
        try:
            for source in (pool / "archives", private / "artifacts"):
                for path in source.glob("*.ltar"):
                    if path.name in seeded:
                        continue
                    try:
                        if ARTIFACT.fullmatch(path.name) and not path.is_symlink():
                            link(path, download / "artifacts" / path.name)
                            seeded.add(path.name)
                    except FileNotFoundError:
                        continue  # Pool GC won the race; native Lake can fetch this file.
            # Reuse only the host-owned immutable revision maps, never agent-created metadata.
            revisions = pool / "revisions/TauCeti"
            recent = sorted(revisions.glob("*.jsonl"), key=revision_mtime, reverse=True)[:64]
            for path in recent:
                with contextlib.suppress(FileNotFoundError):
                    link(path, download / "revisions/TauCeti" / path.name)
        except Unavailable:
            raise
        except (OSError, ValueError) as error:
            raise Unavailable(f"expanded cache setup unavailable: {error}") from error
        record_phase(report, "seed", seed_started)
        report["seeded_artifacts"] = len(seeded)
        warm_env = dict(env, LAKE_CACHE_DIR=str(download))
        ready = False
        try:
            with measured(report, "native_fetch"):
                yield warm_env
            ready = True
        finally:
            # Even failed expansion retains the ordinary verified downloads.
            # Copy mutable metadata, never hard-link it to something Lake may rewrite.
            with measured(report, "manifest"):
                manifest = mappings(download)
                report["mappings"] = len(manifest)
            private_mappings = {}
            try:
                with measured(report, "transfer"):
                    transfer(download, private, seeded, manifest=manifest, private_mappings=private_mappings)
            except Exception as error:
                if ready:
                    raise Unavailable(f"expanded cache transfer failed: {type(error).__name__}: {error}") from error
                # The fetch's own failure is the outcome to report, not this one.
                log(f"expanded cache: transfer after a failed fetch also failed ({type(error).__name__}: {error})")
            if ready:
                with contextlib.suppress(OSError):
                    for path in (download / "revisions/TauCeti").glob("*.jsonl"):
                        if not path.is_symlink():
                            link(path, pool / "revisions/TauCeti" / path.name)
                    revisions = sorted((pool / "revisions/TauCeti").glob("*.jsonl"), key=revision_mtime, reverse=True)
                    for old in revisions[64:]:
                        old.unlink(missing_ok=True)  # Revision maps: metadata, not build outputs.
            if ready and env.get("LAKE_ARTIFACT_CACHE", "1").lower() not in ("0", "false", "off"):
                try:
                    try:
                        with measured(report, "expand_hydrate"):
                            expand(
                                download,
                                private,
                                cwd,
                                warm_env,
                                pool,
                                report,
                                manifest=manifest,
                                private_mappings=private_mappings,
                                deadline=deadline,
                            )
                    except CorruptArchive as error:
                        # A stale local .ltar must not poison every future round. Detach
                        # the known-bad candidate and let the existing downloader repair it.
                        (download / "artifacts" / error.name).unlink(missing_ok=True)  # Our download.
                        from .canonical_store import Guard

                        alias = private / "artifacts" / error.name
                        with contextlib.suppress(FileNotFoundError):
                            if not Guard().unlink(alias, alias.lstat(), recent_ok=True):
                                raise Unavailable(f"corrupt archive {alias} is in use; not removing it") from error
                        if refetch is None:
                            raise
                        with measured(report, "refetch"):
                            refetch(warm_env)
                        with measured(report, "manifest"):
                            manifest = mappings(download)
                            report["mappings"] = len(manifest)
                        with measured(report, "transfer"):
                            transfer(download, private, seeded, manifest=manifest, private_mappings=private_mappings)
                        with measured(report, "expand_hydrate"):
                            expand(
                                download,
                                private,
                                cwd,
                                warm_env,
                                pool,
                                report,
                                manifest=manifest,
                                private_mappings=private_mappings,
                                deadline=deadline,
                            )
                except Unavailable:
                    raise
                except Exception as error:
                    # Partial hydration would leave Lake unpacking private copies (case b).
                    raise Unavailable(f"expanded cache preparation failed: {type(error).__name__}: {error}") from error
                finally:
                    finish(pool, private, report, started, env, warm_env)
            else:
                finish(pool, private, report, started, env, warm_env)


def finish(pool, private, report, started, env, warm_env):
    report["seconds"] = round(time.monotonic() - started, 3)
    report["at"] = time.time()
    status_key = hashlib.sha256(str(private).encode()).hexdigest()
    with contextlib.suppress(OSError):
        atomic_json(pool / "status" / (status_key + ".json"), report)
    env.update({key: value for key, value in warm_env.items() if key.startswith("TAUCETI_PR_CACHE_")})
    log(f"expanded cache: {report['hits']} reused, {report['expanded']} expanded; {report['seconds']:.1f}s preparation")


def same_filesystem(*paths):
    """Hard links need one filesystem; fail preparation instead of letting anyone copy."""
    devices = {}
    for path in paths:
        path = Path(path)
        while not path.exists() and path != path.parent:
            path = path.parent
        devices[str(path)] = path.stat().st_dev
    if len(set(devices.values())) > 1:
        raise SharedLinkUnavailable(f"build cache paths are on different filesystems: {devices}")


def summary():
    rows = [read_json(path) for path in (root() / "status").glob("*.json")]
    rows = [row for row in rows if time.time() - row.get("at", 0) < 3 * 86400]
    return dict(
        workers=len(rows),
        hits=sum(row.get("hits", 0) for row in rows),
        expanded=sum(row.get("expanded", 0) for row in rows),
        seconds=sum(row.get("seconds", 0) for row in rows),
    )

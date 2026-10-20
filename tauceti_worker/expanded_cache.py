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
import shutil
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


class CorruptArchive(ValueError):
    def __init__(self, name):
        self.name = name
        super().__init__(f"CI archive hash mismatch: {name}")


def root(env=None):
    env = os.environ if env is None else env
    return Path(
        env.get("TAUCETI_BUILD_CACHE_ROOT") or (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/tauceti/builds")
    )


@contextlib.contextmanager
def locked(pool, key, *, readonly=False):
    directory = pool / "locks"
    namespace = "entry" if HASH.fullmatch(key) else key.split("-", 1)[0]
    path = directory / (namespace + "-" + hashlib.sha256(key.encode()).hexdigest()[:3] + ".lock")
    if readonly and not path.exists():
        yield
        return
    if not readonly:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("r" if readonly else "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except FileExistsError:
        pass
    except OSError:
        # Different filesystems: preserve isolation without depending on hard links.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
            temporary = Path(out.name)
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o444)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


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
        raise OSError("shared-cache helper unavailable for this toolchain; using private cache")
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
                os.replace(work / "hash", target)
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


def entries(download):
    # A fresh download directory has no locally compiled output mappings. Every .ltar
    # mapping here came from the existing trusted CI service during this preparation.
    for path in (download / "outputs/TauCeti").glob("*.json"):
        if not HASH.fullmatch(path.stem):
            continue
        data = read_json(path).get("data")
        if isinstance(data, str) and ARTIFACT.fullmatch(data) and data.endswith(".ltar"):
            yield path.stem, data


def hydrate(entry, private, input_hash):
    row = read_json(entry / "entry.json")
    names = descriptions(row["data"])
    if not all((entry / name).is_file() and not (entry / name).is_symlink() for name in names):
        raise ValueError("incomplete expanded entry")
    desired = dict(schemaVersion=SCHEMA, service=None, data=row["data"])
    keys = input_hash if isinstance(input_hash, list) else [input_hash]
    if not (
        all(read_json(private / "outputs/TauCeti" / (key + ".json")) == desired for key in keys)
        and all((private / "artifacts" / name).is_file() for name in names)
    ):
        for name in names:
            link(entry / name, private / "artifacts" / name)
        for key in keys:
            atomic_json(private / "outputs/TauCeti" / (key + ".json"), desired)
    # Explicit reuse timestamp: archive mtimes and filesystem atime are not LRU.
    if time.time() - (entry / "entry.json").stat().st_mtime > 60:
        os.utime(entry / "entry.json", None)


def expand(download, private, cwd, env, pool, report):
    grouped = {}
    for input_hash, archive in entries(download):
        grouped.setdefault(archive, []).append(input_hash)
    pending = [(inputs, archive) for archive, inputs in grouped.items()]
    if not pending:
        return
    deadline = time.monotonic() + 120
    program = None
    for offset in range(0, len(pending), 128):
        if time.monotonic() >= deadline:
            break
        with contextlib.ExitStack() as stack:
            missing = []
            for input_hash, archive in pending[offset : offset + 128]:
                key = archive[:-5]
                try:
                    stack.enter_context(locked(pool, key))
                except BlockingIOError:
                    continue  # Another publisher is useful work, not a reason to wait.
                entry = pool / "entries" / key
                try:
                    if (entry / "entry.json").is_file():
                        hydrate(entry, private, input_hash)
                        report["hits"] += 1
                    else:
                        missing.append((input_hash, archive, entry))
                except OSError as error:
                    log(f"expanded cache: private hydration unavailable ({error})")
                except (ValueError, KeyError) as error:
                    log(f"expanded cache: replacing unusable entry {key}: {error}")
                    if entry.is_dir() and not entry.is_symlink():
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
                        # Create a publication link FIRST, so a newly published canonical
                        # artifact never appears to GC as an unreferenced single link.
                        link(artifacts[name], publication / name)
                        link(publication / name, shared)
                        shared.chmod(0o444)
                        if shared.stat().st_ino != (publication / name).stat().st_ino:
                            (publication / name).unlink()
                            link(shared, publication / name)
                    atomic_json(publication / "entry.json", dict(data=data))
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(publication, entry)
                    hydrate(entry, private, input_hash)
                    report["expanded"] += 1


def transfer(download, private):
    """Preserve downloads on unsupported toolchains, without linking mutable metadata."""
    try:
        wanted = {name for _, name in entries(download)}
        for directory in ("artifacts", "outputs", "revisions"):
            for source in (download / directory).rglob("*"):
                if source.is_file() and not source.is_symlink():
                    target = private / directory / source.relative_to(download / directory)
                    if directory == "artifacts":
                        if source.name in wanted:
                            link(source, target)
                    else:
                        if directory == "outputs" and isinstance(read_json(target).get("data"), dict):
                            continue  # Preserve this worker's already expanded native mapping.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if source.suffix == ".json":
                            data = json.loads(source.read_text())
                            if read_json(target) != data:
                                atomic_json(target, data)
                        else:
                            shutil.copyfile(source, target)
    except (OSError, ValueError) as error:
        log(f"expanded cache: retaining private fallback after transfer error: {error}")


@contextlib.contextmanager
def preparation(cwd, env, *, refetch=None):
    """Use fresh CI metadata, reusing existing archive bytes without trusting agent mappings."""
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
        temporary = tempfile.TemporaryDirectory(dir=staging, prefix="prepare-")
    except (OSError, ValueError) as error:
        log(f"expanded cache unavailable; using private cache: {error}")
        yield env
        return
    started = time.monotonic()
    report = dict(hits=0, expanded=0)
    with temporary as tmp, contextlib.ExitStack() as leases:
        try:
            leases.enter_context(locked(pool, "download-" + Path(tmp).name))
        except OSError:
            yield env
            return
        download = Path(tmp)
        # Pool contains only previously verified CI artifacts; private .ltars are also
        # candidates, but the native verifier checks their hash before publication.
        try:
            for source in (pool / "archives", private / "artifacts"):
                for path in source.glob("*.ltar"):
                    if ARTIFACT.fullmatch(path.name) and not path.is_symlink():
                        link(path, download / "artifacts" / path.name)
            # Reuse only the host-owned immutable revision maps, never agent-created metadata.
            revisions = pool / "revisions/TauCeti"
            recent = sorted(revisions.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:64]
            for path in recent:
                link(path, download / "revisions/TauCeti" / path.name)
        except (OSError, ValueError) as error:
            log(f"expanded cache setup unavailable; using private cache ({error})")
            yield env
            return
        warm_env = dict(env, LAKE_CACHE_DIR=str(download))
        ready = False
        try:
            yield warm_env
            ready = True
        finally:
            # Even failed/unsupported expansion retains the ordinary verified downloads.
            # Copy mutable metadata, never hard-link it to something Lake may rewrite.
            transfer(download, private)
            if ready:
                with contextlib.suppress(OSError):
                    for path in (download / "revisions/TauCeti").glob("*.jsonl"):
                        if not path.is_symlink():
                            link(path, pool / "revisions/TauCeti" / path.name)
                    revisions = sorted(
                        (pool / "revisions/TauCeti").glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
                    )
                    for old in revisions[64:]:
                        old.unlink(missing_ok=True)
            if ready and env.get("LAKE_ARTIFACT_CACHE", "1").lower() not in ("0", "false", "off"):
                try:
                    try:
                        expand(download, private, cwd, warm_env, pool, report)
                    except CorruptArchive as error:
                        # A stale local .ltar must not poison every future round. Detach
                        # the known-bad candidate and let the existing downloader repair it.
                        (download / "artifacts" / error.name).unlink(missing_ok=True)
                        (private / "artifacts" / error.name).unlink(missing_ok=True)
                        if refetch is not None:
                            refetch(warm_env)
                            transfer(download, private)
                            expand(download, private, cwd, warm_env, pool, report)
                except Exception as error:
                    # Sharing is an optimization; report its failure without spending a
                    # PR attempt or replacing the original host-preparation outcome.
                    log(f"expanded cache: private fallback ({type(error).__name__}: {error})")
            report["seconds"] = round(time.monotonic() - started, 3)
            report["at"] = time.time()
            status_key = hashlib.sha256(str(private).encode()).hexdigest()
            with contextlib.suppress(OSError):
                atomic_json(pool / "status" / (status_key + ".json"), report)
            env.update({key: value for key, value in warm_env.items() if key.startswith("TAUCETI_PR_CACHE_")})
            log(
                f"expanded cache: {report['hits']} reused, {report['expanded']} expanded; {report['seconds']:.1f}s preparation"
            )


def summary():
    rows = [read_json(path) for path in (root() / "status").glob("*.json")]
    rows = [row for row in rows if time.time() - row.get("at", 0) < 3 * 86400]
    return dict(
        workers=len(rows),
        hits=sum(row.get("hits", 0) for row in rows),
        expanded=sum(row.get("expanded", 0) for row in rows),
        seconds=sum(row.get("seconds", 0) for row in rows),
    )

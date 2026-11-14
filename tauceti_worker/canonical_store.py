"""Store each TauCeti build output once per host, as one inode every checkout links to.

`relink()` runs only at boundaries where no Lake process can be writing the checkout: before the
agent starts, after its process group has been reaped, and after a worker-driven build. Lake 4.36
has no build lock, so these boundaries are the only safe ones.

Objects live under the owned build-cache pool (`expanded_cache.root()`) in a versioned namespace:

- `objects/<lakehash>.<ext>`: the payload, mode 0444 (0555 keeps an execute bit);
- `meta/<prefix>/<lakehash>.<ext>.json`: `{sha256, size, provenance, published_at}`;
- `locks/obj-<prefix>.lock`: sharded object locks, shared for relinking, exclusive for eviction;
- `uses/<checkout>.list`: the names a checkout linked at its last relink; a lease for the collector.

Lake's 64-bit name is not an integrity check, so convergence also requires equal sha256 and size.
A mismatch is treated as tampering: the local file is kept and the round fails. Same-uid workers
share these files cooperatively; this is not yet a security boundary (step 6 of the plan).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import expanded_cache
from .config import log
from .runtime_status import read_json

NAMESPACE = "canonical-v1"
# Descriptor extensions Lake keeps next to the trace; the rest live under `ir/`.
LIBRARY = frozenset({"olean", "olean.server", "olean.private", "ilean", "ir", "ir.sig"})
# Only these write `.lake/build`; any other process with its cwd in the checkout is harmless.
WRITERS = frozenset({"lake", "lean", "leanc", "leantar", "leanir"})
HASH_BATCH = 512
PROBE = r"""
import os, sys
made = []
code = 0
try:
    for source, target in zip(sys.argv[1::2], sys.argv[2::2]):
        os.link(source, target)
        made.append(target)
except OSError as error:
    print(f"[Errno {error.errno}] {error.strerror}: {error.filename} -> {error.filename2}")
    code = 1
finally:
    for path in made:
        try:
            os.unlink(path)
        except OSError:
            pass
sys.exit(code)
"""


class Unavailable(expanded_cache.Unavailable):
    """Relinking could not complete; the round must not continue on private copies."""


class Tampered(Unavailable):
    """A build output carries a canonical name but different bytes."""


class Busy(Unavailable):
    """A Lake or Lean process may still be writing the checkout."""


def store(pool=None):
    return (expanded_cache.root() if pool is None else Path(pool)) / NAMESPACE


def legacy(pool, name):
    """The pre-namespace pool copy of `name`, which expansion verified against CI's archive."""
    return pool / ("archives" if name.endswith(".ltar") else "artifacts") / name


def sidecar(directory, name):
    return directory / "meta" / name[:2] / (name + ".json")


@contextlib.contextmanager
def object_lock(directory, name, *, exclusive=False, blocking=True):
    locks = directory / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locks / f"obj-{name[:3]}.lock").open("a+") as stream:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(stream, mode | (0 if blocking else fcntl.LOCK_NB))
        yield


def digest(path):
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        return hashlib.file_digest(stream, "sha256").hexdigest(), info.st_size


def outputs(build):
    """`(path, name)` for every output a completed module trace records, and the setup files.

    A file no trace names is not "completed" and is left alone.
    """
    lib, ir = build / "lib/lean", build / "ir"
    found, setups = [], []
    for base, dirs, files in os.walk(build, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for file in files:
            if file.endswith(".setup.json"):
                setups.append(Path(base) / file)
            if not file.endswith(".trace") or file.endswith(".ir.trace"):
                continue
            trace = Path(base) / file
            try:
                relative = trace.parent.relative_to(lib)
            except ValueError:
                continue
            try:
                names = expanded_cache.descriptions(read_json(trace).get("outputs"))
            except ValueError:
                continue  # Not a module trace, or an interrupted one.
            module = file[: -len(".trace")]
            for name in dict.fromkeys(names):
                extension = name.split(".", 1)[1]
                found.append(((lib if extension in LIBRARY else ir) / relative / (module + "." + extension), name))
    return found, setups


def writers(checkout):
    """Processes that could be writing the checkout's build directory (Linux `/proc` only)."""
    if not os.path.isdir("/proc/self"):
        return []
    root = os.path.realpath(checkout)
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if Path(entry.path, "comm").read_text().strip() not in WRITERS:
                continue
            cwd = os.readlink(os.path.join(entry.path, "cwd"))
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + os.sep):
            found.append(int(entry.name))
    return found


def probe(checkout, private_store, directory, *, prefix=(), env=None):
    """Hard-link store -> private store -> checkout inside the agent's exact namespace."""
    build = checkout / ".lake/build"
    target_dir = build if build.is_dir() else checkout / ".lake"
    if not target_dir.is_dir():
        return
    source = directory / "link-probe"
    if not source.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as out:
            out.write(b"tauceti hard-link probe\n")
        os.chmod(out.name, 0o444)
        os.replace(out.name, source)
    (private_store / "artifacts").mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    middle = private_store / "artifacts" / f".link-probe-{token}"
    final = target_dir / f".link-probe-{token}"
    args = [str(source), str(middle), str(middle), str(final)]
    if not prefix:
        made = []
        try:
            for a, b in ((source, middle), (middle, final)):
                os.link(a, b)
                made.append(b)
        except OSError as error:
            raise Unavailable(f"hard links unavailable ({error}); Lake would silently copy build outputs") from None
        finally:
            for path in made:
                path.unlink(missing_ok=True)
        return
    from .shared_mathlib import lease_fds

    try:
        result = subprocess.run(
            [*prefix, sys.executable, "-I", "-c", PROBE, *args],
            cwd=checkout,
            env=env,
            pass_fds=lease_fds(env or {}),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Unavailable(f"hard-link probe could not run in the agent namespace: {error}") from None
    finally:
        middle.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()[-400:]
        raise Unavailable(f"hard links unavailable in the agent namespace: {detail}")


def replace(canonical, path):
    """Atomically make `path` a link to `canonical`; a concurrent reader never sees it missing."""
    temporary = path.parent / f".relink-{secrets.token_hex(6)}"
    try:
        os.link(canonical, temporary)
    except OSError as error:
        raise Unavailable(f"cannot hard-link {canonical} -> {path}: {error}") from None
    try:
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Relinker:
    def __init__(self, pool, private_store, *, report):
        self.pool = Path(pool)
        self.directory = store(self.pool)
        self.objects = self.directory / "objects"
        self.private = Path(private_store) / "artifacts" if private_store else None
        self.report = report
        self.tampered = []
        self.digests = {}
        for name in ("objects", "meta", "locks", "uses"):
            (self.directory / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def count(self, key, amount=1):
        self.report[key] = self.report.get(key, 0) + amount

    def expected(self, name):
        """The canonical object's (sha256, size), healing a missing or stale sidecar."""
        canonical = self.objects / name
        row = read_json(sidecar(self.directory, name))
        info = canonical.stat()
        if isinstance(row.get("sha256"), str) and row.get("size") == info.st_size and row.get("ino") == info.st_ino:
            return row["sha256"], row["size"]
        sha, size = digest(canonical)
        self.record(name, sha, size, row.get("provenance", "unknown"), canonical.stat().st_ino)
        return sha, size

    def record(self, name, sha, size, provenance, inode):
        expanded_cache.atomic_json(
            sidecar(self.directory, name),
            dict(sha256=sha, size=size, provenance=provenance, published_at=time.time(), ino=inode),
        )

    def digest(self, path, info):
        """sha256 once per inode: a build file and its private-store link are often one file."""
        key = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        if key not in self.digests:
            self.digests[key] = digest(path)
        return self.digests[key]

    def check(self, path, name, info, expected):
        if info.st_size != expected[1] or self.digest(path, info) != expected:
            self.tampered.append(str(path))
            log(f"canonical store: {path} has the name {name} but different bytes; keeping it, failing the round")
            return False
        return True

    def publish(self, path, name, info, *, verified):
        """Make `path`'s inode (or the pool's CI-verified copy) the canonical object. True if done."""
        canonical = self.objects / name
        old = legacy(self.pool, name)
        try:
            pool_info = old.lstat()
        except FileNotFoundError:
            pool_info = None
        if pool_info is not None and stat.S_ISREG(pool_info.st_mode):
            sha, size = self.digest(old, pool_info)
            if pool_info.st_ino != info.st_ino and not self.check(path, name, info, (sha, size)):
                return False
            source, provenance, inode = old, "ci", pool_info.st_ino
        elif verified:
            mode = 0o555 if info.st_mode & 0o111 else 0o444
            if stat.S_IMODE(info.st_mode) != mode:
                os.chmod(path, mode)
            sha, size = digest(path)
            source, provenance, inode = path, "local", info.st_ino
        else:
            self.count("unverified")
            return False
        try:
            os.link(source, canonical)
        except FileExistsError:
            return None  # A concurrent identical publisher won; converge on its object.
        except OSError as error:
            raise Unavailable(f"cannot publish {name}: {error}") from None
        self.record(name, sha, size, provenance, inode)
        self.count("published_" + provenance)
        return True

    def converge(self, path, name, *, verified, private=False):
        """Under the object's shared lock: leave `path` linked to the canonical object, or fail."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        for _ in range(3):
            try:
                canonical_info = (self.objects / name).stat()
            except FileNotFoundError:
                if private:
                    old = legacy(self.pool, name)
                    if not old.is_file():
                        return  # Private-only metadata is Lake's; leave unverifiable names alone.
                done = self.publish(path, name, info, verified=verified)
                if done is None:
                    continue
                if not done:
                    return
                canonical_info = (self.objects / name).stat()
            if canonical_info.st_ino == info.st_ino:
                return
            if not self.check(path, name, info, self.expected(name)):
                return
            replace(self.objects / name, path)
            self.count("relinked_private" if private else "relinked")
            self.count("relinked_bytes", info.st_blocks * 512 if info.st_nlink == 1 else 0)
            return
        raise Unavailable(f"canonical object {name} kept disappearing while relinking")


def relink(checkout, private_store, pool=None, *, env=None, sweep_private=False, report=None):
    """Converge `checkout/.lake/build` (and the private Lake store) on canonical objects.

    Missing or empty build directories are a no-op. Raises `Unavailable` for link failures,
    `Tampered` for bytes that do not match an existing canonical object, `Busy` if a Lake or
    Lean process has its cwd in the checkout.
    """
    checkout = Path(checkout)
    report = {} if report is None else report
    build = checkout / ".lake/build"
    if not (build / "lib/lean").is_dir():
        return report
    if busy := writers(checkout):
        raise Busy(f"Lake or Lean processes {busy} are running in {checkout}; not relinking")
    pool = expanded_cache.root(env) if pool is None else Path(pool)
    started = time.monotonic()
    try:
        expanded_cache.same_filesystem(build, *([private_store] if private_store else []), pool)
    except expanded_cache.SharedLinkUnavailable as error:
        raise Unavailable(str(error)) from None
    relinker = Relinker(pool, private_store, report=report)
    found, setups = outputs(build)
    report["outputs"] = len(found)
    # Fast path: no lock and no hashing for outputs that already share the canonical inode.
    pending = []
    for path, name in found:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            if (relinker.objects / name).stat().st_ino == info.st_ino:
                relinker.count("in_place")
                pending.append((None, name))
                continue
        except FileNotFoundError:
            pass
        pending.append((path, name))
    names = {name for _, name in pending}
    if relinker.private is not None and relinker.private.is_dir():
        if sweep_private:
            names |= {
                entry.name
                for entry in os.scandir(relinker.private)
                if expanded_cache.ARTIFACT.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            }
    # Lake's name is only verified for bytes that would become a new local publication.
    unpublished = {
        path: name
        for path, name in pending
        if path is not None and not (relinker.objects / name).exists() and not legacy(pool, name).exists()
    }
    verified = set()
    if unpublished:
        try:
            program = expanded_cache.native(checkout, env if env is not None else dict(os.environ), pool)
            with tempfile.TemporaryDirectory(dir=relinker.directory, prefix="hash-") as tmp:
                paths = list(unpublished)
                for offset in range(0, len(paths), HASH_BATCH):
                    batch = paths[offset : offset + HASH_BATCH]
                    computed = expanded_cache.hashes(program, batch, Path(tmp))
                    # A trace's name is the Lake hash of the file it recorded.
                    verified.update(p for p in batch if computed.get(str(p)) == unpublished[p].split(".", 1)[0])
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise Unavailable(f"cannot verify Lake hashes of new build outputs: {error}") from None
    by_name = {}
    for path, name in pending:
        if path is not None:
            by_name.setdefault(name, []).append(path)
    for name in sorted(names):
        paths = by_name.get(name, [])
        private = relinker.private / name if relinker.private is not None else None
        if private is not None:
            try:
                inode = private.lstat().st_ino
            except FileNotFoundError:
                private = None
            else:
                with contextlib.suppress(FileNotFoundError):
                    if (relinker.objects / name).stat().st_ino == inode:
                        private = None
        if not paths and private is None:
            continue
        with object_lock(relinker.directory, name):
            for path in paths:
                relinker.converge(path, name, verified=path in verified)
            if private is not None:
                relinker.converge(private, name, verified=False, private=True)
    for path in setups:
        with contextlib.suppress(FileNotFoundError):
            report["setup_bytes"] = report.get("setup_bytes", 0) + path.stat().st_blocks * 512
            path.unlink()
    report["setup_removed"] = len(setups)
    used = sorted({name for _, name in found})
    lease = relinker.directory / "uses" / (hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16] + ".list")
    with tempfile.NamedTemporaryFile("w", dir=lease.parent, delete=False) as out:
        out.write("\n".join(used) + ("\n" if used else ""))
    os.replace(out.name, lease)
    report["seconds"] = round(time.monotonic() - started, 3)
    if relinker.tampered:
        report["tampered"] = relinker.tampered[:20]
        raise Tampered(
            f"{len(relinker.tampered)} build outputs differ from canonical objects of the same name, e.g. "
            + relinker.tampered[0]
        )
    return report


def boundary(checkout, env, *, phase, prefix=()):
    """Relink at a round boundary; `None` when sharing is off or there is no build directory."""
    if env.get("TAUCETI_SHARED_ARTIFACTS", "1") == "0" or not env.get("LAKE_CACHE_DIR"):
        return None
    checkout = Path(checkout)
    if not (checkout / ".lake/build/lib/lean").is_dir():
        return None
    pool = expanded_cache.root(env)
    private_store = Path(env["LAKE_CACHE_DIR"])
    try:
        from .cache_storage import initialize

        initialize(pool)
        if phase == "prepare":
            probe(checkout, private_store, store(pool), prefix=prefix, env=env)
        report = relink(checkout, private_store, pool, env=env, sweep_private=phase == "prepare")
    except Unavailable:
        raise
    except (OSError, ValueError) as error:
        raise Unavailable(f"{type(error).__name__}: {error}") from error
    log(
        f"canonical store ({phase}): {report.get('outputs', 0)} outputs, {report.get('in_place', 0)} shared, "
        f"{report.get('relinked', 0)} relinked, {report.get('relinked_private', 0)} store links, "
        f"{report.get('published_local', 0) + report.get('published_ci', 0)} published, "
        f"{report.get('setup_removed', 0)} setup files removed; {report.get('seconds', 0):.1f}s"
    )
    return report


def sweep(pool, report, *, dry=False, urgent=False, now=None, lifetime, remove):
    """Evict canonical objects only their own pool names link to, under the exclusive object lock.

    Link counts are evidence, not a refcount: a checkout or private store that links an object
    keeps it alive. Names a checkout recorded at a recent relink are kept unless under pressure.
    """
    from .retention import note

    now = time.time() if now is None else now
    directory = store(pool)
    objects = directory / "objects"
    if not objects.is_dir():
        return 0
    leased = None
    freed = 0
    for lease in (directory / "uses").glob("*.list"):
        with contextlib.suppress(FileNotFoundError):
            if now - lease.stat().st_mtime > lifetime:
                remove(lease, pool, report, dry=dry, check_users=False, mounts=[])
    for path in objects.iterdir():
        name = path.name
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            aliases = [path]
            old = legacy(pool, name)
            with contextlib.suppress(FileNotFoundError):
                if old.lstat().st_ino == info.st_ino:
                    aliases.append(old)
            if info.st_nlink > len(aliases):
                continue
            if not urgent:
                if leased is None:
                    leased = set()
                    for lease in (directory / "uses").glob("*.list"):
                        with contextlib.suppress(FileNotFoundError):
                            leased.update(lease.read_text().split())
                if name in leased:
                    continue
            with object_lock(directory, name, exclusive=True, blocking=False):
                info = path.lstat()
                if info.st_nlink > len(aliases):
                    continue  # A relink linked it after the first look.
                if dry:
                    freed += info.st_blocks * 512
                    continue
                for alias in aliases:
                    alias.unlink(missing_ok=True)
                sidecar(directory, name).unlink(missing_ok=True)
                freed += info.st_blocks * 512
        except BlockingIOError:
            note(report, "protected", str(path))
        except FileNotFoundError:
            continue
        except OSError as error:
            note(report, "errors", f"{path}: {error}")
    for meta in (directory / "meta").glob("*/*.json"):
        with contextlib.suppress(FileNotFoundError):
            if not (objects / meta.name[: -len(".json")]).exists() and now - meta.stat().st_mtime > 3600:
                if not dry:
                    meta.unlink()
    return freed

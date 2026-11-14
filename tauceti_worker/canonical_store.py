"""Store each TauCeti build output once per host, as one inode every checkout links to.

The boundary is the worker's own process lifecycle: `relink()` runs before the agent starts and
after the round's process group has been reaped (and after a worker-driven build). A `/proc` scan
for Lake or Lean writers, an open-for-writing check and an mtime check are supplementary evidence;
any doubt skips an output, it never publishes it.

Objects live under the owned build-cache pool (`expanded_cache.root()`) in a versioned namespace:

- `objects/<lakehash>.<ext>`: the payload, mode 0444 (0555 keeps an execute bit);
- `meta/<prefix>/<lakehash>.<ext>.json`: `{sha256, size, provenance, published_at, ino}`;
- `locks/obj-<prefix>.lock`: sharded object locks: shared to link, exclusive to publish or evict;
- `slots/slot-<n>.lock`: a host-wide bound on concurrent relinks;
- `uses/<checkout>.list`: the names a checkout linked at its last relink; a lease for the collector.

Lake's 64-bit name is not an integrity check, so convergence requires equal sha256 and size.
A build file or private-store file whose bytes differ from an established object is stale build
state (an interrupted rebuild leaves new bytes beside the old trace): its module is invalidated,
so Lake rebuilds it. An object that no longer matches its own sidecar is an integrity failure: it
is evicted and reported. Nothing is ever copied. Same-uid workers share these files cooperatively;
this is not yet a security boundary (step 6 of the plan).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
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
# Only these write `.lake/build`; servers never do.
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
    """Hard links are unavailable or relinking hit an I/O failure."""


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


@contextlib.contextmanager
def slot(directory, *, count, wait):
    """Yield True while holding one of `count` host-wide relink slots, or False after `wait` s."""
    slots = directory / "slots"
    slots.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + wait
    while True:
        for index in range(count):
            stream = (slots / f"slot-{index}.lock").open("a+")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                continue
            try:
                yield True
            finally:
                stream.close()
            return
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(0.5)


def digest(path):
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        return hashlib.file_digest(stream, "sha256").hexdigest(), info.st_size


def record(directory, name, sha, size, provenance, *, exclusive=False):
    path = sidecar(directory, name)
    info = (directory / "objects" / name).stat()
    value = dict(
        sha256=sha,
        size=size,
        provenance=provenance,
        published_at=time.time(),
        ino=info.st_ino,
        mtime_ns=info.st_mtime_ns,
    )
    if not exclusive:
        expanded_cache.atomic_json(path, value)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
        json.dump(value, out)
    try:
        os.link(out.name, path)
    except FileExistsError:
        pass  # The publisher's own record wins.
    finally:
        os.unlink(out.name)


def expected(directory, name):
    """`(sha256, size, intact)` of the canonical object, from its sidecar.

    The sidecar is trusted while the object's inode, size and mtime are as recorded; otherwise
    the object is re-hashed (Lake's `adjustMTime` legitimately moves mtimes). A re-hash that
    disagrees with the recorded sha256 means the object was written in place: `intact` is False.
    A missing sidecar is healed from the object, created only if still absent so a concurrent
    publisher's provenance wins.
    """
    canonical = directory / "objects" / name
    row = read_json(sidecar(directory, name))
    info = canonical.stat()
    recorded = isinstance(row.get("sha256"), str) and isinstance(row.get("size"), int)
    if recorded and (row["size"], row.get("ino"), row.get("mtime_ns")) == (
        info.st_size,
        info.st_ino,
        info.st_mtime_ns,
    ):
        return row["sha256"], row["size"], True
    sha, size = digest(canonical)
    if recorded and (sha, size) != (row["sha256"], row["size"]) and row.get("ino") == info.st_ino:
        return row["sha256"], row["size"], False
    record(directory, name, sha, size, row.get("provenance", "unknown"), exclusive=not row)
    return sha, size, True


def outputs(build):
    """Completed module outputs as `{trace: [(path, name), ...]}`, and the setup files.

    A file no trace names is not "completed" and is left alone.
    """
    lib, ir = build / "lib/lean", build / "ir"
    modules, setups = {}, []
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
            found = modules[trace] = []
            for name in dict.fromkeys(names):
                extension = name.split(".", 1)[1]
                found.append(((lib if extension in LIBRARY else ir) / relative / (module + "." + extension), name))
    return modules, setups


def writers(checkout):
    """Lake or Lean processes that name the checkout by cwd or argument (Linux `/proc` only).

    Evidence only: the owned process lifecycle is the boundary. Language servers never write
    `.lake/build`, so they never count.
    """
    if not os.path.isdir("/proc/self"):
        return []
    root = os.path.realpath(checkout)
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            comm = Path(entry.path, "comm").read_text().strip()
            if comm not in WRITERS:
                continue
            args = Path(entry.path, "cmdline").read_bytes().decode(errors="replace").split("\0")
            if "--server" in args or (comm == "lake" and "serve" in args):
                continue
            cwd = os.readlink(os.path.join(entry.path, "cwd"))
        except OSError:
            continue
        inside = [cwd, *(os.path.realpath(a) for a in args[1:] if a.startswith(os.sep))]
        if any(p == root or p.startswith(root + os.sep) for p in inside):
            found.append(int(entry.name))
    return found


def open_for_writing(inodes):
    """The subset of `(dev, ino)` that some process of this user holds open for writing."""
    if not inodes or not os.path.isdir("/proc/self/fd"):
        return set()
    found = set()
    uid = os.getuid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            descriptors = list(os.scandir(os.path.join(entry.path, "fd")))
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                info = os.stat(descriptor.path)
            except OSError:
                continue
            key = info.st_dev, info.st_ino
            if key not in inodes or key in found:
                continue
            try:
                text = Path(entry.path, "fdinfo", descriptor.name).read_text()
                flags = int(next(line.split()[1] for line in text.splitlines() if line.startswith("flags:")), 8)
            except (OSError, StopIteration, ValueError):
                found.add(key)  # Unknown is not quiescent.
                continue
            if flags & (os.O_WRONLY | os.O_RDWR):
                found.add(key)
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
    def __init__(self, pool, private_store, *, report, not_after, busy, checkout=None, env=None):
        self.checkout = checkout
        self.env = env
        self.pool = Path(pool)
        self.directory = store(self.pool)
        self.objects = self.directory / "objects"
        self.private = Path(private_store) / "artifacts" if private_store else None
        self.report = report
        self.not_after = not_after
        self.busy = busy  # (dev, ino) some process may still be writing
        self.digests = {}
        self.module_of = {}
        for name in ("objects", "meta", "locks", "uses"):
            (self.directory / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def count(self, key, amount=1):
        self.report[key] = self.report.get(key, 0) + amount

    def digest(self, path, info):
        """sha256 once per inode: a build file and its private-store link are often one file."""
        key = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        if key not in self.digests:
            self.digests[key] = digest(path)
        return self.digests[key]

    def invalidate(self, path, private):
        """Stale or partial bytes: delete them (and their module's trace) so Lake rebuilds."""
        if private:
            path.unlink(missing_ok=True)
            self.count("stale_store_files")
            return
        trace, paths = self.module_of.get(path, (None, [path]))
        for output in paths:
            output.unlink(missing_ok=True)
            output.with_name(output.name + ".hash").unlink(missing_ok=True)
        if trace is not None:
            trace.unlink(missing_ok=True)
        self.count("invalidated_modules")
        log(f"canonical store: {path} does not match its trace; removed its module outputs so Lake rebuilds it")

    def evict_corrupt(self, name, info):
        canonical = self.objects / name
        aliases = [canonical]
        old = legacy(self.pool, name)
        with contextlib.suppress(FileNotFoundError):
            if old.lstat().st_ino == info.st_ino:
                aliases.append(old)
        for alias in aliases:
            alias.unlink(missing_ok=True)
        sidecar(self.directory, name).unlink(missing_ok=True)
        self.report.setdefault("integrity_failures", []).append(name)
        log(f"canonical store: INTEGRITY FAILURE: object {name} no longer matches its sha256; evicted")

    def lake_verified(self, path, name):
        """Lake-hash one file against its trace name; None if the helper is unavailable."""
        try:
            program = expanded_cache.native(self.checkout, dict(self.env), self.pool)
            with tempfile.TemporaryDirectory(dir=self.directory, prefix="hash-") as tmp:
                computed = expanded_cache.hashes(program, [path], Path(tmp))
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return computed.get(str(path)) == name.split(".", 1)[0]

    def publishable(self, path, info):
        """Only a quiescent inode may become canonical: never one still open or changed since."""
        if (info.st_dev, info.st_ino) in self.busy or info.st_mtime > self.not_after:
            self.count("deferred_busy")
            return False
        return True

    def converge(self, path, name, *, verified, private, exclusive):
        """Leave `path` linked to the canonical object, invalidate it, or defer it.

        Returns "exclusive" when the caller must retry holding the exclusive object lock.
        """
        canonical = self.objects / name
        for _ in range(3):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                return None
            try:
                canonical_info = canonical.stat()
            except FileNotFoundError:
                if not exclusive:
                    return "exclusive"  # Publish under the exclusive lock: one hasher per object.
                if self.publish(path, name, info, verified=verified, private=private) == "again":
                    continue
                return None
            if canonical_info.st_ino == info.st_ino:
                return None
            sha, size, intact = expected(self.directory, name)
            if not intact:
                if not exclusive:
                    return "exclusive"
                self.evict_corrupt(name, canonical_info)
                if verified is None and not private:
                    verified = self.lake_verified(path, name)
                continue
            if info.st_size == size and self.digest(path, info) == (sha, size):
                replace(canonical, path)
                self.count("relinked_private" if private else "relinked")
                return None
            self.invalidate(path, private)
            return None
        raise Unavailable(f"canonical object {name} kept changing while relinking")

    def publish(self, path, name, info, *, verified, private):
        """Under the exclusive object lock: adopt the pool's CI copy, or publish a verified local file."""
        canonical = self.objects / name
        old = legacy(self.pool, name)
        try:
            pool_info = old.lstat()
            if not stat.S_ISREG(pool_info.st_mode):
                raise FileNotFoundError(old)
            sha, size = self.digest(old, pool_info)
            if pool_info.st_ino != info.st_ino and (info.st_size != size or self.digest(path, info) != (sha, size)):
                self.invalidate(path, private)
                return None
            os.link(old, canonical)
        except FileNotFoundError:
            pass  # No pool copy, or legacy GC won the race: fall through to local publication.
        except FileExistsError:
            return "again"  # A concurrent publisher won; converge on its object.
        except OSError as error:
            raise Unavailable(f"cannot publish {name}: {error}") from None
        else:
            record(self.directory, name, sha, size, "ci")
            self.count("published_ci")
            if pool_info.st_ino != info.st_ino:
                replace(canonical, path)
                self.count("relinked_private" if private else "relinked")
            return None
        if private:
            return None  # Private-store-only names are Lake's metadata to keep.
        if verified is False:
            self.invalidate(path, private)
            return None
        if verified is None or not self.publishable(path, info):
            self.count("deferred")
            return None
        mode = 0o555 if info.st_mode & 0o111 else 0o444
        if stat.S_IMODE(info.st_mode) != mode:
            os.chmod(path, mode)
        sha, size = digest(path)
        try:
            os.link(path, canonical)
        except FileExistsError:
            return "again"
        except OSError as error:
            raise Unavailable(f"cannot publish {name}: {error}") from None
        record(self.directory, name, sha, size, "local")
        self.count("published_local")
        return None


def relink(
    checkout,
    private_store,
    pool=None,
    *,
    env=None,
    sweep_private=False,
    delete_setup=True,
    budget=None,
    slots=None,
    slot_wait=None,
    report=None,
):
    """Converge `checkout/.lake/build` (and the private Lake store) on canonical objects.

    Bounded and resumable: at most `slots` relinks run on the host at once, a relink that cannot
    get a slot within `slot_wait` seconds is skipped, and one that exceeds `budget` seconds stops
    and leaves the rest for the next boundary. Missing build directories are a no-op. Raises
    `Unavailable` only for link failures.
    """
    checkout = Path(checkout)
    report = {} if report is None else report
    build = checkout / ".lake/build"
    if not (build / "lib/lean").is_dir():
        return report
    env = os.environ if env is None else env
    budget = float(env.get("TAUCETI_RELINK_BUDGET_S", 180)) if budget is None else budget
    slots = int(env.get("TAUCETI_RELINK_SLOTS", 4)) if slots is None else slots
    slot_wait = float(env.get("TAUCETI_RELINK_SLOT_WAIT_S", 60)) if slot_wait is None else slot_wait
    if busy := writers(checkout):
        report["skipped"] = f"Lake or Lean processes {busy} name the checkout"
        return report
    pool = expanded_cache.root(env) if pool is None else Path(pool)
    try:
        expanded_cache.same_filesystem(build, *([private_store] if private_store else []), pool)
    except expanded_cache.SharedLinkUnavailable as error:
        raise Unavailable(str(error)) from None
    directory = store(pool)
    with slot(directory, count=slots, wait=slot_wait) as held:
        if not held:
            report["skipped"] = f"{slots} relinks already running on this host"
            return report
        return _relink(checkout, build, private_store, pool, env, sweep_private, delete_setup, budget, report)


def _relink(checkout, build, private_store, pool, env, sweep_private, delete_setup, budget, report):
    started = time.monotonic()
    deadline = started + budget
    relinker = Relinker(
        pool, private_store, report=report, not_after=time.time(), busy=set(), checkout=checkout, env=env
    )
    modules, setups = outputs(build)
    found = [(path, name) for pairs in modules.values() for path, name in pairs]
    for trace, pairs in modules.items():
        paths = [path for path, _ in pairs]
        for path in paths:
            relinker.module_of[path] = (trace, paths)
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
                pending.append((None, name, info))
                continue
        except FileNotFoundError:
            pass
        pending.append((path, name, info))
    names = {name for _, name, _ in pending}
    if sweep_private and relinker.private is not None and relinker.private.is_dir():
        names |= {
            entry.name
            for entry in os.scandir(relinker.private)
            if expanded_cache.ARTIFACT.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
        }
    # Lake's name is verified only for bytes that could become a new local publication.
    unpublished = {
        path: (name, info)
        for path, name, info in pending
        if path is not None and not (relinker.objects / name).exists() and not legacy(pool, name).exists()
    }
    verified = {}
    if unpublished:
        relinker.busy = open_for_writing({(i.st_dev, i.st_ino) for _, i in unpublished.values()})
        try:
            program = expanded_cache.native(checkout, dict(env), pool)
            with tempfile.TemporaryDirectory(dir=relinker.directory, prefix="hash-") as tmp:
                paths = list(unpublished)
                for offset in range(0, len(paths), HASH_BATCH):
                    if time.monotonic() >= deadline:
                        break
                    batch = paths[offset : offset + HASH_BATCH]
                    computed = expanded_cache.hashes(program, batch, Path(tmp))
                    # A trace's name is the Lake hash of the file it recorded.
                    for p in batch:
                        if str(p) in computed:
                            verified[p] = computed[str(p)] == unpublished[p][0].split(".", 1)[0]
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            log(f"canonical store: Lake hashes unavailable, deferring new publications ({error})")
    by_name = {}
    for path, name, _ in pending:
        if path is not None:
            by_name.setdefault(name, []).append(path)
    for name in sorted(names):
        if time.monotonic() >= deadline:
            report["incomplete"] = True
            break
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
        work = [(path, False) for path in paths] + ([(private, True)] if private is not None else [])
        if not work:
            continue
        exclusive = not (relinker.objects / name).exists()
        while work:
            with object_lock(relinker.directory, name, exclusive=exclusive):
                retry = []
                for path, is_private in work:
                    outcome = relinker.converge(
                        path, name, verified=verified.get(path), private=is_private, exclusive=exclusive
                    )
                    if outcome == "exclusive":
                        retry.append((path, is_private))
            work, exclusive = retry, True
    if delete_setup and not report.get("incomplete"):
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
    return report


def reconcile(pool, name, path):
    """Make a pool alias of `name` (an entry file, `artifacts/`) the canonical inode, if identical.

    Expansion publishes CI outputs independently; this keeps one inode when a local publication
    came first. Best effort: any doubt leaves the alias as it is.
    """
    directory = store(pool)
    canonical = directory / "objects" / name
    try:
        if canonical.stat().st_ino == path.stat().st_ino:
            return True
    except FileNotFoundError:
        return False
    try:
        with object_lock(directory, name):
            sha, size, intact = expected(directory, name)
            if not intact:
                return False  # The next relink evicts it; never spread a damaged object.
            info = path.stat()
            if info.st_size != size or digest(path) != (sha, size):
                return False
            replace(canonical, path)
            return True
    except (OSError, Unavailable):
        return False


def boundary(checkout, env, *, phase, prefix=()):
    """Relink at a round boundary. Only a failed hard-link probe before the agent starts raises.

    Everything else (I/O errors, busy checkouts, a busy host) is logged and left for the next
    boundary: relinking is maintenance and must not discard or block a round.
    """
    if env.get("TAUCETI_SHARED_ARTIFACTS", "1") == "0" or not env.get("LAKE_CACHE_DIR"):
        return None
    checkout = Path(checkout)
    if not (checkout / ".lake/build/lib/lean").is_dir():
        return None
    pool = expanded_cache.root(env)
    private_store = Path(env["LAKE_CACHE_DIR"])
    if phase == "prepare":
        try:
            from .cache_storage import initialize

            initialize(pool)
            probe(checkout, private_store, store(pool), prefix=prefix, env=env)
        except Unavailable:
            raise
        except (OSError, ValueError) as error:
            raise Unavailable(f"hard-link probe failed: {type(error).__name__}: {error}") from error
    try:
        report = relink(
            checkout,
            private_store,
            pool,
            env=env,
            sweep_private=phase == "prepare",
            # Lean reads setup files while compiling; delete them only at round boundaries.
            delete_setup=phase in ("prepare", "round-end"),
        )
    except Exception as error:  # noqa: BLE001 - maintenance; the next boundary retries
        log(f"canonical store ({phase}): relinking deferred ({type(error).__name__}: {error})")
        return None
    if report.get("skipped"):
        log(f"canonical store ({phase}): relinking skipped: {report['skipped']}")
        return report
    log(
        f"canonical store ({phase}): {report.get('outputs', 0)} outputs, {report.get('in_place', 0)} shared, "
        f"{report.get('relinked', 0)} relinked, {report.get('relinked_private', 0)} store links, "
        f"{report.get('published_local', 0) + report.get('published_ci', 0)} published, "
        f"{report.get('invalidated_modules', 0)} stale modules removed, "
        f"{report.get('setup_removed', 0)} setup files removed"
        f"{', incomplete' if report.get('incomplete') else ''}; {report.get('seconds', 0):.1f}s"
    )
    if report.get("integrity_failures"):
        log(f"canonical store ({phase}): INTEGRITY FAILURES evicted: {report['integrity_failures'][:10]}")
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
                if old.lstat().st_ino != info.st_ino and not dry and reconcile(pool, name, old):
                    info = path.lstat()  # Expansion had published a second inode; now one.
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

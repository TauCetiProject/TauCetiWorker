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
def object_lock(directory, name, *, exclusive=False, blocking=True, deadline=None):
    """The object's shard lock. With a monotonic `deadline`, poll until then, then raise TimeoutError."""
    locks = directory / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locks / f"obj-{name[:3]}.lock").open("a+") as stream:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if deadline is None:
            fcntl.flock(stream, mode | (0 if blocking else fcntl.LOCK_NB))
        else:
            while True:
                try:
                    fcntl.flock(stream, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"object lock for {name} still held at the relink deadline") from None
                    time.sleep(0.05)
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


def digest(path, deadline=None):
    """`(sha256, size)`; with a monotonic `deadline`, stop between 1 MiB chunks (TimeoutError)."""
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        if deadline is None:
            return hashlib.file_digest(stream, "sha256").hexdigest(), info.st_size
        hasher = hashlib.sha256()
        while chunk := stream.read(1 << 20):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"hashing {path} reached the relink deadline")
            hasher.update(chunk)
        return hasher.hexdigest(), info.st_size


def rejected(pool):
    """Fingerprints of bytes relinking proved wrong; hydration must not reinstall them."""
    try:
        lines = (store(pool) / "rejected.list").read_text().split()
    except FileNotFoundError:
        return set()
    found = set()
    for line in lines:
        with contextlib.suppress(ValueError):
            found.add(tuple(int(part) for part in line.split(":")))
    return found


def reject(pool, info):
    """Remember a rejected inode snapshot (one short O_APPEND line: atomic across workers)."""
    path = store(pool) / "rejected.list"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(":".join(map(str, fingerprint(info))) + "\n")


def fingerprint(info):
    """What must not change between inspecting a file and acting on it."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def recorded(directory, name, info):
    """True if the sidecar still describes this inode: the cheap check before any shortcut."""
    row = read_json(sidecar(directory, name))
    return (row.get("size"), row.get("ino"), row.get("mtime_ns")) == (info.st_size, info.st_ino, info.st_mtime_ns)


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


def expected(directory, name, deadline=None):
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
    sha, size = digest(canonical, deadline)
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


def open_for_writing():
    """Every `(dev, ino)` some process of this user holds open for writing (Linux `/proc` only).

    One scan per relink pass. An fd whose flags cannot be read counts as writable.
    """
    if not os.path.isdir("/proc/self/fd"):
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
            if not stat.S_ISREG(info.st_mode):
                continue
            key = info.st_dev, info.st_ino
            if key in found:
                continue
            try:
                text = Path(entry.path, "fdinfo", descriptor.name).read_text()
                flags = int(next(line.split()[1] for line in text.splitlines() if line.startswith("flags:")), 8)
            except (OSError, StopIteration, ValueError):
                found.add(key)
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


class Guard:
    """The one gate for mutating a shared name.

    Every link, rename or unlink of a canonical object, pool artifact, private-store alias or
    checkout output goes through a guard. It proceeds only if each affected inode is not open for
    writing by this user's processes (one lazy `/proc` scan per guard), is not in `opened` (inodes
    a collector saw open at all), was not modified after `not_after`, and still has the
    fingerprint it had when it was inspected, re-checked immediately before the system call.
    Otherwise the mutation is deferred and `deferred_busy` / `deferred_changed` counted.
    Only our own temporary files, cursors, leases and sidecars bypass it.
    """

    def __init__(self, *, writable=None, opened=(), not_after=None, report=None):
        self._writable = writable
        self.opened = set(opened or ())
        self.not_after = not_after
        self.report = {} if report is None else report

    @property
    def writable(self):
        if self._writable is None:
            self._writable = open_for_writing()
        return self._writable

    def _count(self, key):
        self.report[key] = self.report.get(key, 0) + 1

    def busy(self, info):
        key = info.st_dev, info.st_ino
        return key in self.opened or key in self.writable

    def idle(self, *pairs, recent_ok=False):
        """True if every `(path, inspected_info)` is quiescent right now."""
        for path, before in pairs:
            if self.busy(before) or (not recent_ok and self.not_after is not None and before.st_mtime > self.not_after):
                self._count("deferred_busy")
                return False
            try:
                now = path.lstat()
            except FileNotFoundError:
                return False
            if fingerprint(now) != fingerprint(before):
                self._count("deferred_changed")
                return False
        return True

    def unlink(self, path, before, *, recent_ok=False):
        if not self.idle((path, before), recent_ok=recent_ok):
            return False
        path.unlink(missing_ok=True)
        return True

    def link(self, source, before, target, *, recent_ok=False):
        """`os.link(source, target)`; FileExistsError and other OSErrors reach the caller."""
        if not self.idle((source, before), recent_ok=recent_ok):
            return False
        os.link(source, target)
        return True

    def replace(self, source, source_info, path, path_info, *, recent_ok=False):
        """Atomically make `path` a link to `source`, if both are quiescent."""
        if not self.idle((source, source_info), recent_ok=True) or not self.idle(
            (path, path_info), recent_ok=recent_ok
        ):
            return False
        replace(source, path)
        return True

    def chmod(self, path, before, mode):
        if not self.idle((path, before)):
            return False
        os.chmod(path, mode)
        return True


def replace(canonical, path):
    """Atomically make `path` a link to `canonical`; a concurrent reader never sees it missing.

    Callers go through `Guard.replace`; the temporary name is ours alone.
    """
    temporary = path.parent / f".relink-{secrets.token_hex(6)}"
    try:
        os.link(canonical, temporary)
    except OSError as error:
        raise Unavailable(f"cannot hard-link {canonical} -> {path}: {error}") from None
    try:
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# Files hashed and then converged together; small enough that one batch fits any budget.
BATCH = 16
GRACE = 30


class Pass:
    """One bounded relink pass over a checkout. Every destructive step re-checks quiescence.

    Decisions follow the bytes: a file is trusted when its Lake hash matches its name (and, to
    converge, when its sha256 matches the canonical object's). A file that fails its Lake hash is
    removed with its module trace so Lake rebuilds it; a verified file is never deleted.
    """

    def __init__(self, checkout, pool, private_store, env, report, *, deadline, not_after):
        self.checkout = checkout
        self.pool = Path(pool)
        self.env = env
        self.directory = store(self.pool)
        self.objects = self.directory / "objects"
        self.private = Path(private_store) / "artifacts" if private_store else None
        self.report = report
        self.deadline = deadline
        self.not_after = not_after
        self.guard = Guard(writable=open_for_writing(), not_after=not_after, report=report)
        self.writable = self.guard.writable
        self.shas = {}
        self.lakes = {}
        self.seen = set()  # Fingerprints asked about this pass: what the persisted cache keeps.
        self.program = None
        self.helper_failed = False
        self.trace_of = {}
        self.suspect = set()  # Names whose bytes failed here: their private aliases get checked.
        self.rejected = rejected(self.pool)  # Snapshots earlier passes proved wrong.
        for name in ("objects", "meta", "locks", "uses", "cursors", "hashes"):
            (self.directory / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def count(self, key, amount=1):
        self.report[key] = self.report.get(key, 0) + amount

    def quiescent(self, path, before):
        """Unchanged since inspected, not open for writing, not modified since the boundary began."""
        return self.guard.idle((path, before))

    def sha(self, path, info):
        key = fingerprint(info)
        if key not in self.shas:
            self.shas[key] = digest(path, self.deadline)
        return self.shas[key]

    def helper(self):
        if self.program is None and not self.helper_failed:
            try:
                self.program = expanded_cache.native(self.checkout, dict(self.env), self.pool)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self.helper_failed = True
                log(f"canonical store: Lake hash helper unavailable; deferring decisions ({error})")
        return self.program

    def prefetch(self, items):
        """Lake-hash `(path, name, info)` items in one helper call; unknown stays unknown."""
        self.seen.update(fingerprint(i) for _, _, i in items)
        items = [(p, n, i) for p, n, i in items if fingerprint(i) not in self.lakes]
        if not items or time.monotonic() >= self.deadline or self.helper() is None:
            return
        try:
            with tempfile.TemporaryDirectory(dir=self.directory, prefix="hash-") as tmp:
                computed = expanded_cache.hashes(self.program, [p for p, _, _ in items], Path(tmp))
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            log(f"canonical store: Lake hashing failed; deferring ({error})")
            return
        for path, _, info in items:
            if str(path) in computed:
                # The bytes' hash, not a verdict: two names may share one inode.
                self.lakes[fingerprint(info)] = computed[str(path)]

    def lake(self, path, name, info):
        """True if the bytes hash to `name`, False if not, None if unknown."""
        self.seen.add(fingerprint(info))
        if fingerprint(info) not in self.lakes:
            self.prefetch([(path, name, info)])
        computed = self.lakes.get(fingerprint(info))
        return None if computed is None else computed == name.split(".", 1)[0]

    def invalidate(self, path, name, info, private):
        """Remove bytes that fail their own Lake hash so Lake rebuilds them.

        The module trace stays: Lake's up-to-date check requires every recorded output to exist,
        so one missing output already forces a rebuild, and the trace still lists the module's
        unfinished siblings for the next relink pass.
        """
        self.suspect.add(name)
        self.remember(info)
        if not private:
            self.drop_private_alias(name, info)
        if not self.guard.unlink(path, info):
            return
        if private:
            self.count("stale_store_files")
            return
        # Lake's per-checkout `.hash` sidecar: never shared, and Lake rewrites it on restore.
        path.with_name(path.name + ".hash").unlink(missing_ok=True)
        self.count("invalidated_outputs")
        log(f"canonical store: {path} fails its Lake hash {name}; removed it so Lake rebuilds its module")

    def idle(self, path, info):
        """A pool file may be published, quarantined or evicted only while nothing can write it."""
        return self.guard.idle((path, info), recent_ok=True)

    def remember(self, info):
        if fingerprint(info) not in self.rejected:
            self.rejected.add(fingerprint(info))
            with contextlib.suppress(OSError):
                reject(self.pool, info)

    def drop_private_alias(self, name, rejected_info):
        """The private store's link to bad bytes would let Lake restore them: remove it too.

        Only while the alias is still exactly the rejected snapshot: repaired bytes are kept.
        """
        if self.private is None:
            return
        alias = self.private / name
        with contextlib.suppress(FileNotFoundError):
            if fingerprint(alias.lstat()) == fingerprint(rejected_info):
                if self.guard.unlink(alias, rejected_info, recent_ok=True):
                    self.count("stale_store_files")

    def evict(self, name, info, reason):
        canonical = self.objects / name
        aliases = [canonical]
        old = legacy(self.pool, name)
        with contextlib.suppress(FileNotFoundError):
            if old.lstat().st_ino == info.st_ino:
                aliases.append(old)
        if not self.guard.idle(*((alias, info) for alias in aliases), recent_ok=True):
            return False
        for alias in aliases:
            self.guard.unlink(alias, info, recent_ok=True)
        sidecar(self.directory, name).unlink(missing_ok=True)  # Our metadata for the evicted name.
        self.suspect.add(name)
        self.remember(info)
        self.drop_private_alias(name, info)
        self.report.setdefault("integrity_failures", []).append(name)
        log(f"canonical store: INTEGRITY FAILURE: object {name} {reason}; evicted")
        return True

    def link_in(self, canonical, canonical_info, path, info, private):
        if self.guard.replace(canonical, canonical_info, path, info):
            self.count("relinked_private" if private else "relinked")

    def decide(self, path, name, private):
        """Under the exclusive object lock: converge, publish, invalidate or defer `path`."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        canonical = self.objects / name
        for _ in range(3):
            try:
                canonical_info = canonical.stat()
            except FileNotFoundError:
                canonical_info = None
            if canonical_info is not None:
                if (canonical_info.st_dev, canonical_info.st_ino) in self.writable:
                    self.count("deferred_busy")  # Neither trusted nor re-recorded while writable.
                    return
                sha, size, intact = expected(self.directory, name, self.deadline)
                if not intact:
                    if self.evict(name, canonical_info, "no longer matches its recorded sha256"):
                        continue
                    return
                if canonical_info.st_ino == info.st_ino:
                    return
                if info.st_size == size and self.sha(path, info) == (sha, size):
                    self.link_in(canonical, canonical_info, path, info, private)
                    return
                self.differs(path, name, info, private)
                return
            outcome = self.publish(path, name, info, private)
            if outcome != "again":
                return
        self.count("deferred")

    def differs(self, path, name, info, private):
        """`path` disagrees with a verified object: its own Lake hash decides."""
        verdict = self.lake(path, name, info)
        if verdict is False:
            self.invalidate(path, name, info, private)
        elif verdict is True:
            self.count("conflicts")
            log(f"canonical store: {path} and object {name} both verify but differ; leaving both")
        else:
            self.count("deferred")

    def publish(self, path, name, info, private):
        """No canonical object yet: adopt a verified pool copy or publish a verified local file."""
        canonical = self.objects / name
        old = legacy(self.pool, name)
        try:
            pool_info = old.lstat()
            if not stat.S_ISREG(pool_info.st_mode):
                pool_info = None
        except FileNotFoundError:
            pool_info = None
        verdict = None
        if pool_info is not None:
            pool_verdict = self.lake(old, name, pool_info)
            if pool_verdict is None:
                self.count("deferred")
                return None
            if pool_verdict:
                try:
                    if not self.idle(old, pool_info):
                        return None
                    sha, size = self.sha(old, pool_info)
                    # Re-checked inside: changed while hashing means not the bytes that were verified.
                    if not self.guard.link(old, pool_info, canonical, recent_ok=True):
                        return None
                except FileExistsError:
                    return "again"
                except FileNotFoundError:
                    return "again"  # Legacy GC won the race: decide again without it.
                except OSError as error:
                    raise Unavailable(f"cannot publish {name}: {error}") from None
                record(self.directory, name, sha, size, "ci")
                self.count("published_ci")
                return "again"  # Converge `path` on the object just adopted.
            verdict = False if pool_info.st_ino == info.st_ino else self.lake(path, name, info)
            if verdict is not True:
                # Neither copy verifies (or the checkout's is unknown): leave both.
                self.count("unverifiable" if verdict is False else "deferred")
                log(f"canonical store: pool copy of {name} fails its Lake hash, and so does {path}; leaving it")
                return None
            # The checkout's bytes are right and the pool's are not: quarantine the pool copy.
            if not self.guard.unlink(old, pool_info, recent_ok=True):
                return None
            self.count("pool_quarantined")
            log(f"canonical store: pool copy of {name} fails its Lake hash; removed it")
        elif private and name not in self.suspect and fingerprint(info) not in self.rejected:
            return None  # Private-store-only names are Lake's metadata; not hashed.
        elif private:
            if self.lake(path, name, info) is False:
                self.invalidate(path, name, info, private)  # A corrupt alias Lake could restore.
            return None
        if verdict is None:
            verdict = self.lake(path, name, info)
        if verdict is False:
            self.invalidate(path, name, info, private)
            return None
        if verdict is None:
            self.count("deferred")
            return None
        if not self.quiescent(path, info):
            return None
        mode = 0o555 if info.st_mode & 0o111 else 0o444
        if stat.S_IMODE(info.st_mode) != mode and not self.guard.chmod(path, info, mode):
            return None
        sha, size = self.sha(path, info)
        try:
            if not self.guard.link(path, info, canonical):
                return None
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
    limit=None,
    report=None,
):
    """Converge `checkout/.lake/build` (and the private Lake store) on canonical objects.

    Bounded and resumable: at most `slots` relinks run on the host at once, a relink that cannot
    get a slot within `slot_wait` seconds is skipped, and a pass stops at `budget` seconds (or
    after `limit` names), keeping a per-checkout cursor so the next pass continues where this one
    stopped. Missing build directories are a no-op. Raises `Unavailable` only for link failures.
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
    with slot(store(pool), count=slots, wait=slot_wait) as held:
        if not held:
            report["skipped"] = f"{slots} relinks already running on this host"
            return report
        started = time.monotonic()
        work = Pass(checkout, pool, private_store, env, report, deadline=started + budget, not_after=time.time())
        _relink(work, build, sweep_private, delete_setup, limit)
        report["seconds"] = round(time.monotonic() - started, 3)
        return report


def _relink(work, build, sweep_private, delete_setup, limit):
    report = work.report
    modules, setups = outputs(build)
    by_name = {}
    found = 0
    for trace, pairs in modules.items():
        for path, name in pairs:
            found += 1
            work.trace_of[path] = trace
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                # Shortcut only while the sidecar still describes this exact inode.
                if (work.objects / name).stat().st_ino == info.st_ino and recorded(work.directory, name, info):
                    work.count("in_place")
                    continue
            except FileNotFoundError:
                pass
            by_name.setdefault(name, []).append(path)
    report["outputs"] = found
    private_names = set()
    if sweep_private and work.private is not None and work.private.is_dir():
        for entry in os.scandir(work.private):
            if not expanded_cache.ARTIFACT.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            with contextlib.suppress(FileNotFoundError):
                if (work.objects / entry.name).stat().st_ino == entry.stat(follow_symlinks=False).st_ino:
                    continue
            private_names.add(entry.name)
    elif work.private is not None:
        for name in by_name:
            with contextlib.suppress(FileNotFoundError):
                if (work.private / name).lstat().st_ino != (work.objects / name).stat().st_ino:
                    private_names.add(name)
    # One work item per file, so a name with many copies is bounded like many names.
    items = sorted(
        [(name, str(path), False) for name, paths in by_name.items() for path in paths]
        + [(name, str(work.private / name), True) for name in private_names]
    )
    key = hashlib.sha256(str(work.checkout.resolve()).encode()).hexdigest()[:16]
    cursor_path = work.directory / "cursors" / (key + ".json")
    cursor = read_json(cursor_path)
    after = tuple(cursor.get("after") or ("", ""))
    split = next((i for i, item in enumerate(items) if item[:2] > after), len(items))
    order = items[split:] + items[:split]
    # Files whose sha256 outlasted an earlier pass: skipped then, retried one per pass, first.
    slow = set(cursor.get("slow") or ()) & {raw for _, raw, _ in items}
    retry = next((item for item in order if item[1] in slow), None)
    if retry is not None:
        order = [retry] + [item for item in order if item is not retry]
    done = 0
    last = None
    regranted = False
    deadline = work.deadline
    # Lake hashes computed by earlier passes, keyed by fingerprint: a pass that ran out of time
    # while hashing leaves that work for the next one instead of discarding it.
    hashes_path = work.directory / "hashes" / (key + ".json")
    for raw_key, value in read_json(hashes_path).items():
        with contextlib.suppress(ValueError):
            work.lakes[tuple(int(part) for part in raw_key.split(":"))] = value
    reserve = min(30.0, max(0.5, 0.2 * (deadline - time.monotonic())))  # Kept for converging.
    per_file = None
    # Every pass finishes at least one file, so even an exhausted budget makes progress.
    work.deadline = max(deadline, time.monotonic() + (3 * GRACE if retry is not None else GRACE))
    try:
        offset = 0
        while offset < len(order):
            batch = order[offset : offset + BATCH]
            if limit is not None:
                batch = batch[: max(0, limit - done)]
            if not batch or time.monotonic() >= work.deadline:
                raise TimeoutError("relink budget reached")
            # Hash what this batch needs to decide new names, then converge this batch.
            fetch, names, sizes = [], set(), []
            for name, raw, _ in batch:
                before = len(fetch)
                if not (work.objects / name).exists():
                    candidates = [Path(raw)] + ([legacy(work.pool, name)] if name not in names else [])
                    names.add(name)
                    for candidate in candidates:
                        with contextlib.suppress(FileNotFoundError):
                            info = candidate.lstat()
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            work.seen.add(fingerprint(info))
                            if fingerprint(info) not in work.lakes:
                                fetch.append((candidate, name, info))
                sizes.append(len(fetch) - before)
            if done and per_file:
                # Shrink the batch so its hashing leaves time to converge it.
                allowed = (work.deadline - time.monotonic() - reserve) / per_file
                keep, used = 0, 0
                while keep < len(batch) and used + sizes[keep] <= allowed:
                    used += sizes[keep]
                    keep += 1
                if keep == 0:
                    raise TimeoutError("relink budget reached")
                batch, fetch = batch[:keep], fetch[:used]
            started = time.monotonic()
            for start in range(0, len(fetch), HASH_BATCH):
                work.prefetch(fetch[start : start + HASH_BATCH])
            if fetch:
                per_file = max(1e-4, (time.monotonic() - started) / len(fetch))
            if not done:
                # Grace covers converging, too; a retried slow file keeps its longer allowance.
                allowance = 3 * GRACE if retry is not None else GRACE
                work.deadline = max(work.deadline, deadline, time.monotonic() + allowance)
            index = 0
            while index < len(batch):
                name = batch[index][0]
                end = index
                while end < len(batch) and batch[end][0] == name:
                    end += 1
                group = batch[index:end]
                with object_lock(work.directory, name, exclusive=True, deadline=work.deadline):
                    for _, raw, is_private in group:
                        if time.monotonic() >= work.deadline:
                            raise TimeoutError("relink budget reached")
                        skipped = False
                        try:
                            work.decide(Path(raw), name, is_private)
                            slow.discard(raw)
                        except TimeoutError:
                            # Its sha256 did not fit: skip it, retry it first next pass.
                            slow.add(raw)
                            work.count("deferred_slow")
                            skipped = True
                        done += 1
                        if retry is None or (name, raw) != retry[:2]:
                            last = (name, raw)
                            work.deadline = deadline  # The first file's grace is over.
                            if skipped and not regranted:
                                # Once per pass, a skipped file hands its grace to the next one.
                                regranted = True
                                work.deadline = max(deadline, time.monotonic() + GRACE)
                        else:
                            # The retried file is out of order (no cursor move), and it never
                            # uses up the next file's grace: a slow file cannot pin later work.
                            work.deadline = max(deadline, time.monotonic() + GRACE)
                index += len(group)
            offset += len(batch)
    except TimeoutError:
        pass
    finally:
        with contextlib.suppress(OSError):
            expanded_cache.atomic_json(
                hashes_path,
                {":".join(map(str, k)): v for k, v in work.lakes.items() if v is not None and k in work.seen},
            )
    work.deadline = deadline
    complete = done >= len(order)
    report["remaining"] = len(order) - done
    state = {}
    if not complete:
        report["incomplete"] = True
        state["after"] = list(last) if last is not None else list(after)
    if slow:
        state["slow"] = sorted(slow)
    if state:
        expanded_cache.atomic_json(cursor_path, state)
    else:
        cursor_path.unlink(missing_ok=True)
    if delete_setup and complete:
        removed = 0
        for path in setups:
            with contextlib.suppress(FileNotFoundError):
                info = path.lstat()
                if work.guard.unlink(path, info):
                    report["setup_bytes"] = report.get("setup_bytes", 0) + info.st_blocks * 512
                    removed += 1
        report["setup_removed"] = removed
    used = sorted({name for pairs in modules.values() for _, name in pairs})
    lease = work.directory / "uses" / (key + ".list")
    with tempfile.NamedTemporaryFile("w", dir=lease.parent, delete=False) as out:
        out.write("\n".join(used) + ("\n" if used else ""))
    os.replace(out.name, lease)  # Our lease file; no build output.


def pool_aliases(pool, name, info):
    """The pool-owned names of `info`'s inode: its legacy `artifacts/` copy and canonical object.

    An entry alias is the caller's own. Collectors reclaim an inode once its link count equals
    the pool names they can account for, so canonical adoption never strands a third link.
    """
    found = []
    for path in (legacy(pool, name), store(pool) / "objects" / name):
        with contextlib.suppress(FileNotFoundError):
            alias = path.lstat()
            if stat.S_ISREG(alias.st_mode) and (alias.st_dev, alias.st_ino) == (info.st_dev, info.st_ino):
                found.append(path)
    return found


def release(pool, name, guard, *, dry=False):
    """Remove `name`'s pool-owned names once nothing else links them; returns bytes freed.

    Takes the object lock without waiting (a relink holding it wins) and goes through `guard`.
    Works with relinking disabled, so the switch never strands canonical links.
    """
    directory = store(pool)
    freed = 0
    candidates = [legacy(pool, name), directory / "objects" / name]
    with object_lock(directory, name, exclusive=True, blocking=False):
        for path in candidates:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            aliases = pool_aliases(pool, name, info)
            if info.st_nlink != len(aliases) or not guard.idle(*((a, info) for a in aliases), recent_ok=True):
                continue
            if not dry:
                for alias in aliases:
                    guard.unlink(alias, info, recent_ok=True)
                if directory / "objects" / name in aliases:
                    sidecar(directory, name).unlink(missing_ok=True)  # Our metadata for the evicted name.
            freed += info.st_blocks * 512
    return freed


def reconcile(pool, name, path, *, blocking=True, guard=None):
    """Make a pool alias of `name` (an entry file, `artifacts/`) the canonical inode, if identical.

    Expansion publishes CI outputs independently; this keeps one inode when a local publication
    came first. Best effort: any doubt, or (with `blocking` False) a busy shard, leaves the alias.
    """
    directory = store(pool)
    canonical = directory / "objects" / name
    try:
        canonical_info = canonical.stat()
        info = path.lstat()
        if canonical_info.st_ino == info.st_ino:
            return True
    except FileNotFoundError:
        return False
    guard = Guard() if guard is None else guard
    try:
        with object_lock(directory, name, blocking=blocking):
            if guard.busy(canonical_info) or guard.busy(info):
                return False  # Neither spread nor re-record an inode someone may be writing.
            sha, size, intact = expected(directory, name)
            if not intact:
                return False  # The next relink evicts it; never spread a damaged object.
            if info.st_size != size or digest(path) != (sha, size):
                return False
            return guard.replace(canonical, canonical_info, path, info, recent_ok=True)
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
    # Operator switches, read at every boundary: no restart needed for the file.
    if env.get("TAUCETI_CANONICAL_RELINK", "1") == "0" or (store(pool) / "disabled").exists():
        return None
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


def sweep(pool, report, *, dry=False, urgent=False, now=None, lifetime, remove, reconcile_budget=60, opened=()):
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
    # Open descriptors add no link: protect every inode the collector saw open.
    guard = Guard(opened=opened)
    reconciling_until = time.monotonic() + reconcile_budget
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
                if (
                    old.lstat().st_ino != info.st_ino
                    and not dry
                    and time.monotonic() < reconciling_until
                    and reconcile(pool, name, old, blocking=False, guard=guard)  # Busy shards wait for the next sweep.
                ):
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
                if not guard.idle(*((alias, info) for alias in aliases), recent_ok=True):
                    note(report, "protected", str(path))
                    continue
                if dry:
                    freed += info.st_blocks * 512
                    continue
                for alias in aliases:
                    guard.unlink(alias, info, recent_ok=True)
                sidecar(directory, name).unlink(missing_ok=True)  # Our metadata for the evicted name.
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

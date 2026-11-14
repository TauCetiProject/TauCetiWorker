"""Per-round private bytes: measure what a host round adds to disk, and abandon it past a cap.

A round's *private bytes* are the bytes allocated to inodes whose links all lie inside the round's
own writable roots: its checkout (including `.lake` and the shared-Mathlib overlay upper, which
lives at `.lake/packages/mathlib/.lake/build`), the worker's private Lake and Mathlib stores, and a
private `TMPDIR`. A file hard-linked into a shared store has a link outside those roots, so it is
not private, while a file Lake or an agent wrote, unpacked or copied is. This is the
"externally linked" rule `cache_storage.inventory` uses, counted per inode.

Filesystem quotas would be exact, but hard links fail with EXDEV across XFS project IDs and bind
mounts, which would make Lake copy instead of link. So the cap is measured instead:

- Before the agent starts, one full walk records the baseline (the build state the round inherits).
- During the round a monitor thread lstats every directory each pass, rescans only those whose
  inode or mtime changed (plus a rotating slice of the rest), and re-lstats files changed since the
  round started, which catches a file growing in place. A coarse tripwire on the worker scope's
  cgroup `io.stat` write bytes triggers an early pass when the disk is being written fast.
- When `private - baseline` exceeds the cap, the round's process group is killed. After it is
  reaped, the checkout is moved whole into the worker's discard queue, unsaved: no recovery
  record and no retained checkout state. The collector deletes it without following links or
  crossing filesystems. Nothing is ever deleted through a shared path.

At round start, while this is enabled, the shared Mathlib entry the round leases is also checked
against its publication manifest (`ready.json`). A mismatch stops the round before the agent
starts and stops sharing on the host; it never deletes anything.

`TAUCETI_ROUND_PRIVATE_BYTES` (or `mode` in `<workers state>/round-storage.json`) selects `off`
(the default), `report` (measure and report, never kill) or `enforce`.
"""

from __future__ import annotations

import contextlib
import math
import os
import secrets
import signal
import stat
import threading
import time
from array import array
from pathlib import Path

from .agents import BuildCacheUnavailable
from .config import Die, NoProgress, log
from .runtime_status import atomic_json, read_json, report_runtime

GIB = 1024**3
MODES = ("off", "report", "enforce")
DEFAULT_CAP_GIB = 6.0
DEFAULT_SCAN_SECONDS = 30.0
TICK_SECONDS = 2.0  # tripwire polling; a pass runs at most every `scan_seconds` without it
TRIP_MIN_SECONDS = 5.0  # a tripwire pass waits at least this long after the previous pass
ROTATION = 8  # one directory in eight is rescanned each pass even if its mtime is unchanged
RACY_NS = 2_000_000_000  # a directory changed this recently may change again within one mtime tick
TRACK_LIMIT = 200_000  # files changed since the round started, re-lstat'd each pass
KILL_GRACE = 10.0
SHARED_TEMP = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))

_LATEST: dict | None = None


class RoundAbandoned(NoProgress):
    """The round wrote more private bytes than its cap; it was killed and its checkout discarded."""


class SharedViewMismatch(BuildCacheUnavailable):
    """A protected shared view does not match its publication manifest; the agent never started."""


def latest() -> dict | None:
    """The current round's most recent measurement, for the round's resource report."""
    return dict(_LATEST) if _LATEST else None


def policy_path() -> Path:
    from .worker_manager import workers_state_dir

    return workers_state_dir() / "round-storage.json"


def policy(env=None) -> dict:
    """The host's setting; the environment overrides the per-host file. Invalid values are fatal."""
    env = os.environ if env is None else env
    path = policy_path()
    host = read_json(path)
    if path.exists() and not host:
        raise Die(f"{path} is not a JSON object; repair or remove it")
    mode = env.get("TAUCETI_ROUND_PRIVATE_BYTES") or host.get("mode", "off")
    if mode not in MODES:
        raise Die(f"round private-bytes mode must be one of {', '.join(MODES)}, not {mode!r}")

    def number(variable, field, default):
        raw = env.get(variable) or host.get(field, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value <= 0:
            raise Die(f"{variable} (or `{field}` in {path}) must be a positive number, not {raw!r}")
        return value

    return dict(
        mode=mode,
        cap_bytes=int(number("TAUCETI_ROUND_PRIVATE_CAP_GIB", "cap_gib", DEFAULT_CAP_GIB) * GIB),
        scan_seconds=number("TAUCETI_ROUND_PRIVATE_SCAN_SECONDS", "scan_seconds", DEFAULT_SCAN_SECONDS),
    )


def _shared_roots(env) -> list[Path]:
    from . import expanded_cache, shared_mathlib

    return [shared_mathlib.root(env), expanded_cache.root(env).resolve()]


def _overlaps(path: Path, others) -> bool:
    return any(path == other or path.is_relative_to(other) or other.is_relative_to(path) for other in others)


def round_roots(cwd: Path, env) -> list[Path]:
    """The round's writable roots, resolved, outermost first; never shared state or shared temp."""
    shared = _shared_roots(env)
    found = []
    for raw in (cwd, env.get("LAKE_CACHE_DIR"), env.get("MATHLIB_CACHE_DIR"), env.get("TMPDIR")):
        if not raw:
            continue
        path = Path(raw)
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            real = path.resolve()
        except OSError:
            continue
        if real in SHARED_TEMP or _overlaps(real, shared):
            continue
        found.append(real)
    roots = []
    for path in sorted(set(found), key=lambda p: (len(p.parts), str(p))):
        if not any(path.is_relative_to(kept) for kept in roots):
            roots.append(path)
    return roots


class _Dir:
    __slots__ = ("ino", "mtime", "dev", "subdirs", "solo", "inos", "racy")

    def __init__(self, ino, mtime, dev, subdirs, solo, inos, racy):
        self.ino, self.mtime, self.dev = ino, mtime, dev
        self.subdirs, self.solo, self.inos, self.racy = subdirs, solo, inos, racy


class Scanner:
    """Incremental private-bytes measurement over fixed roots.

    Files with one link are private by definition and only summed per directory. Files with
    several links are counted per inode: private once every link has been seen inside the roots.
    Per file this keeps one 8-byte inode number and, for multi-link files, one dictionary entry.
    """

    def __init__(self, roots):
        self.roots = [str(root) for root in roots]
        self.dirs: dict[str, _Dir] = {}
        self.seen: dict[int, dict[int, int]] = {}  # dev -> ino -> links seen inside the roots
        self.meta: dict[int, dict[int, list]] = {}  # dev -> ino -> [bytes, nlink], for links seen >= 2
        self.tracked: dict[str, tuple] = {}  # path -> (directory, bytes, nlink), changed this round
        self.dirty: set[str] = set()
        self.since_ns = None  # files modified from here on are tracked individually
        self.cursor = 0
        self.passes = 0
        self.directories = 0
        self.listed = 0

    def _unlink(self, dev, ino):
        links = self.seen.get(dev)
        if links is None or ino not in links:
            return
        links[ino] -= 1
        if links[ino] <= 0:
            del links[ino]
        if links.get(ino, 0) < 2:
            self.meta.get(dev, {}).pop(ino, None)

    def _forget(self, path):
        state = self.dirs.pop(path, None)
        if state is None:
            return
        for ino in state.inos:
            self._unlink(state.dev, ino)
        for name in state.subdirs:
            self._forget(os.path.join(path, name))

    def _list(self, path, info, old, now_ns):
        solo, subdirs, inos = 0, [], array("Q")
        dev = info.st_dev
        links = self.seen.setdefault(dev, {})
        meta = self.meta.setdefault(dev, {})
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(entry.name)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        found = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    self.listed += 1
                    size = found.st_blocks * 512
                    if found.st_nlink <= 1 or found.st_dev != dev:
                        solo += size
                    else:
                        inos.append(found.st_ino)
                        count = links.get(found.st_ino, 0) + 1
                        links[found.st_ino] = count
                        if count >= 2:
                            meta[found.st_ino] = [size, found.st_nlink]
                    if (
                        self.since_ns is not None
                        and found.st_mtime_ns >= self.since_ns
                        and (entry.path in self.tracked or len(self.tracked) < TRACK_LIMIT)
                    ):
                        self.tracked[entry.path] = (path, size, found.st_nlink)
        except OSError:
            pass  # unreadable (an overlay work directory is mode 000): counted as empty
        if old is not None:
            for ino in old.inos:
                self._unlink(old.dev, ino)
            for name in set(old.subdirs) - set(subdirs):
                self._forget(os.path.join(path, name))
        state = _Dir(
            info.st_ino, info.st_mtime_ns, dev, tuple(subdirs), solo, inos, now_ns - info.st_mtime_ns < RACY_NS
        )
        self.dirs[path] = state
        return state

    def scan(self, *, full=False) -> int:
        now_ns = time.time_ns()
        for path, (parent, size, nlink) in list(self.tracked.items()):
            try:
                found = os.lstat(path)
            except OSError:
                del self.tracked[path]
                self.dirty.add(parent)
                continue
            if (found.st_blocks * 512, found.st_nlink) != (size, nlink):
                self.dirty.add(parent)
        dirty, self.dirty = self.dirty, set()
        stack = list(reversed(self.roots))
        index = 0
        while stack:
            path = stack.pop()
            try:
                info = os.lstat(path)
            except OSError:
                self._forget(path)
                continue
            if not stat.S_ISDIR(info.st_mode):
                self._forget(path)
                continue
            old = self.dirs.get(path)
            if old is not None and (old.ino, old.dev) != (info.st_ino, info.st_dev):
                self._forget(path)
                old = None
            if (
                full
                or old is None
                or old.racy
                or old.mtime != info.st_mtime_ns
                or index % ROTATION == self.cursor
                or path in dirty
            ):
                state = self._list(path, info, old, now_ns)
            else:
                state = old
            index += 1
            stack.extend(os.path.join(path, name) for name in reversed(state.subdirs))
        self.cursor = (self.cursor + 1) % ROTATION
        self.passes += 1
        self.directories = index
        return self.private_bytes()

    def private_bytes(self) -> int:
        total = sum(state.solo for state in self.dirs.values())
        for dev, rows in self.meta.items():
            links = self.seen.get(dev, {})
            total += sum(size for ino, (size, nlink) in rows.items() if links.get(ino, 0) >= nlink)
        return total


def _write_bytes(directory) -> int | None:
    """Cumulative bytes written by the worker's cgroup scope, over all devices."""
    if directory is None:
        return None
    try:
        total = 0
        for line in (directory / "io.stat").read_text().splitlines():
            fields = dict(field.split("=", 1) for field in line.split()[1:])
            total += int(fields.get("wbytes", 0))
        return total
    except (OSError, ValueError):
        return None


def validate_shared_views(env, *, enforce: bool) -> dict:
    """Check the leased shared Mathlib entry against its `ready.json` inventory.

    Never deletes: a mismatch records a host safety stop (in `enforce`) and stops this round.
    The canonical TauCeti object store has no publication manifest yet and is not checked here.
    """
    key = env.get("TAUCETI_SHARED_MATHLIB_KEY")
    if not key:
        return dict(shared_mathlib="not leased")
    from . import shared_mathlib

    pool = shared_mathlib.root(env)
    entries = pool / "entries"
    entry = entries / key
    problem = None
    try:
        if any(path.is_symlink() for path in (entries, entry, entry / "build")):
            problem = "a symlink replaced part of the entry"
        elif entry.resolve().parent != entries.resolve():
            problem = "the entry resolves outside the pool"
        else:
            ready = read_json(entry / "ready.json")
            if ready.get("key") != key or not ready.get("inventory"):
                problem = "ready.json does not describe this entry"
            elif shared_mathlib.inventory(entry / "build") != ready["inventory"]:
                problem = "files differ from the published inventory"
    except (OSError, shared_mathlib.Unavailable) as error:
        problem = str(error)
    if problem is None:
        return dict(shared_mathlib="matches ready.json")
    reason = f"shared Mathlib entry {key} does not match its publication manifest: {problem}"
    if not enforce:
        log(f"round storage: {reason} (report mode; round continues)")
        return dict(shared_mathlib=reason)
    shared_mathlib.stop_sharing(pool, reason)
    raise SharedViewMismatch(reason + "; agent not started, sharing stopped, nothing deleted")


class RoundStorage:
    """One host agent invocation's private-bytes measurement and cap."""

    def __init__(self, cwd: Path, env, cfg, settings: dict, roots: list[Path]):
        self.cwd = Path(cwd)
        self.env = env
        self.cfg = cfg
        self.mode = settings["mode"]
        self.cap = settings["cap_bytes"]
        self.interval = settings["scan_seconds"]
        self.roots = roots
        self.scanner = Scanner(roots)
        self.baseline = 0
        self.private = 0
        self.peak = 0
        self.scan_seconds = 0.0
        self.error = None
        self.exceeded: dict | None = None
        self.killed = False
        self.views: dict = {}
        self.started_at = time.time()
        self.scope = None
        if cfg is not None:
            with contextlib.suppress(Exception):
                from .resources import scope_directory

                self.scope = scope_directory(cfg)
        self.trip_bytes = max(self.cap // 4, 256 * 1024**2)
        self.thread = None
        self.lock = threading.Lock()

    @classmethod
    def start(cls, cwd: Path, env, cfg=None) -> RoundStorage | None:
        """Validate shared views and record the baseline, before the agent starts; None when off."""
        global _LATEST
        _LATEST = None
        settings = policy(env)
        if settings["mode"] == "off":
            return None
        views = validate_shared_views(env, enforce=settings["mode"] == "enforce")
        guard = cls(cwd, env, cfg, settings, round_roots(Path(cwd), env))
        guard.views = views
        guard.scanner.since_ns = time.time_ns()
        guard.measure(full=True)
        guard.baseline = guard.private
        guard.peak = 0
        guard.publish()
        log(
            f"round storage ({guard.mode}): baseline {guard.baseline / GIB:.2f} GiB private in "
            f"{len(guard.roots)} roots; cap {guard.cap / GIB:.2f} GiB of new private bytes"
        )
        return guard

    @property
    def new_bytes(self) -> int:
        return self.private - self.baseline

    def measure(self, *, full=False) -> None:
        started = time.monotonic()
        try:
            self.private = self.scanner.scan(full=full)
        except Exception as error:  # measurement trouble is reported; it never kills a round
            self.error = f"{type(error).__name__}: {error}"
            return
        finally:
            self.scan_seconds += time.monotonic() - started
        self.peak = max(self.peak, self.new_bytes)
        if self.new_bytes > self.cap and self.exceeded is None:
            self.exceeded = dict(at=time.time(), new_private_bytes=self.new_bytes)

    def summary(self) -> dict:
        return dict(
            mode=self.mode,
            cap_bytes=self.cap,
            baseline_private_bytes=self.baseline,
            private_bytes=self.private,
            new_private_bytes=self.new_bytes,
            peak_new_private_bytes=self.peak,
            exceeded=self.exceeded is not None,
            killed=self.killed,
            roots=[str(root) for root in self.roots],
            passes=self.scanner.passes,
            directories=self.scanner.directories,
            scan_seconds=round(self.scan_seconds, 3),
            shared_views=self.views,
            error=self.error,
            started_at=self.started_at,
        )

    def publish(self) -> None:
        global _LATEST
        _LATEST = self.summary()
        report_runtime(round_storage=_LATEST)

    def watch(self, proc, finished: threading.Event) -> None:
        self.thread = threading.Thread(target=self._run, args=(proc, finished), daemon=True, name="round-storage")
        self.thread.start()

    def _run(self, proc, finished):
        last = time.monotonic()
        written = _write_bytes(self.scope)
        while not finished.wait(TICK_SECONDS):
            now = time.monotonic()
            current = _write_bytes(self.scope)
            tripped = (
                written is not None
                and current is not None
                and current - written >= self.trip_bytes
                and now - last >= TRIP_MIN_SECONDS
            )
            if not tripped and now - last < self.interval:
                continue
            with self.lock:
                self.measure()
                self.publish()
            last, written = time.monotonic(), current
            if self.exceeded and self.mode == "enforce":
                self._kill(proc, finished)
                return

    def _kill(self, proc, finished):
        self.killed = True
        log(
            f"round storage: {self.new_bytes / GIB:.2f} GiB of new private bytes exceeds the "
            f"{self.cap / GIB:.2f} GiB cap; killing the agent's process group"
        )
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        if not finished.wait(KILL_GRACE):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    def conclude(self, rc) -> None:
        """After the agent's group is reaped: final measurement, report, and abandonment."""
        if self.thread is not None:
            self.thread.join(KILL_GRACE + 60)
        if self.thread is None or not self.thread.is_alive():
            with self.lock:
                self.measure()
        self.publish()
        report = dict(self.summary(), exit_code=rc)
        with contextlib.suppress(Exception):
            from .activity import current_round_id, emit

            report["round_id"] = current_round_id()
            emit("round_storage", **report)
        if self.cfg is not None:
            with contextlib.suppress(OSError):
                atomic_json(self.cfg.state / "round-storage.json", report)
        log(
            f"round storage: {self.new_bytes / GIB:.2f} GiB new private bytes "
            f"(peak {self.peak / GIB:.2f}, cap {self.cap / GIB:.2f}); "
            f"{self.scanner.passes} passes, {self.scan_seconds:.1f}s scanning"
        )
        if self.exceeded is None:
            return
        if self.mode != "enforce":
            log("round storage: cap exceeded in report mode; the round is kept")
            return
        where = self.abandon()
        raise RoundAbandoned(
            f"round wrote {self.peak / GIB:.2f} GiB of new private build bytes, over the "
            f"{self.cap / GIB:.2f} GiB cap; killed and abandoned without recovery ({where})"
        )

    def abandon(self) -> str:
        """Move the checkout whole into the discard queue: no recovery record, nothing retained."""
        state = getattr(self.cfg, "state", None)
        if state is None:
            return "no worker state to discard into; checkout left in place"
        cwd = self.cwd
        try:
            if cwd.is_symlink() or not cwd.is_dir():
                return f"{cwd} is not a directory; left in place"
            if _overlaps(cwd.resolve(), _shared_roots(self.env)):
                return f"{cwd} overlaps shared state; left in place"
            trash = Path(state) / "recovery-cleanup" / "discarded"
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / f"abandoned-{cwd.name}-{secrets.token_hex(8)}"
            os.rename(cwd, target)
        except OSError as error:
            log(f"round storage: could not discard {cwd}: {error}")
            return f"discarding {cwd} failed: {error}"
        for directory in (cwd.parent, trash):
            with contextlib.suppress(OSError):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        with contextlib.suppress(Exception):
            from .retention import request_cleanup

            request_cleanup()
        log(f"round storage: moved {cwd} to {target} for deletion, unsaved")
        return f"checkout moved to {target}"


def start(cwd: Path, env, cfg=None) -> RoundStorage | None:
    return RoundStorage.start(cwd, env, cfg)

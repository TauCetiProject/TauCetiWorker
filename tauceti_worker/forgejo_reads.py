"""Direct qualified source reads for Worker's review-state consumer.

The client selects no credentials, process environment, shared-service fallback or
mutation path. Its explicitly supplied Forgejo client stays bound for its life.
Production bootstrap still refuses unfinished non-GitHub worker configurations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from . import github_transport as transport


@dataclass(frozen=True)
class ReadFailure:
    operation: str
    status: int
    kind: str


class ReadInvalidated(RuntimeError):
    """An overlapping write invalidated this observation before it returned."""


class SourceBindingChanged(RuntimeError):
    """This client no longer holds the origin/credential it was constructed for."""


def prune_state_cache(root, *, max_files=384, max_bytes=16 * 1024 * 1024):
    """Bound disposable source sidecars across credential scopes under one root.

    Only owned regular files with review-state cache names in digest directories
    are eligible. Outboxes, unrelated files and symlinks are never followed or
    removed. Concurrent readers tolerate disappearing cache files. This is
    best-effort cache maintenance; it cannot affect a fresh observation's result.
    """
    if type(max_files) is not int or type(max_bytes) is not int or min(max_files, max_bytes) < 1:
        raise ValueError("state cache bounds must be positive integers")
    directories, files = [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return
    try:
        for directory in os.listdir(root_fd):
            if not re.fullmatch(r"[0-9a-f]{64}", directory):
                continue
            try:
                fd = os.open(directory, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    continue
                directories.append(directory)
                for name in os.listdir(fd):
                    if not re.fullmatch(r"[1-9][0-9]*(?:\.(?:key|contest))?(?:\.json|\.[0-9a-f]{32}\.tmp)", name):
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                        files.append((info.st_mtime_ns, directory, name, info.st_size, info.st_dev, info.st_ino))
            finally:
                os.close(fd)
        files.sort()
        count, size = len(files), sum(row[3] for row in files)
        for _, directory, name, length, device, inode in files:
            if count <= max_files and size <= max_bytes:
                break
            try:
                fd = os.open(directory, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (device, inode) or not stat.S_ISREG(info.st_mode):
                    continue  # Another writer replaced it; leave the newer value.
                os.unlink(name, dir_fd=fd)
                count -= 1
                size -= length
            except FileNotFoundError:
                count -= 1
                size -= length
            finally:
                os.close(fd)
        for directory in directories:
            try:
                os.rmdir(directory, dir_fd=root_fd)
            except OSError:
                pass
    except OSError:
        return
    finally:
        os.close(root_fd)


class BoundedReadCache:
    """Process-local JSON cache; coalesce triage reads and never cache failures.

    Fresh reads bypass entries and the triage lock. Generations prevent an
    overlapping invalidation from publishing a pre-write result. Both stored
    bytes and entries are bounded, and every returned value is an independent
    JSON copy. Cache entries convey no fresh observation or approval authority.
    """

    def __init__(self, *, max_entries=128, max_bytes=4 * 1024 * 1024, clock=time.monotonic):
        if type(max_entries) is not int or type(max_bytes) is not int or min(max_entries, max_bytes) < 1:
            raise ValueError("cache bounds must be positive integers")
        self.max_entries, self.max_bytes, self.clock = max_entries, max_bytes, clock
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._triage = tuple(threading.Lock() for _ in range(64))
        self._fresh_generations = [0] * len(self._triage)
        self._generation = 0

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def read(self, key, fetch, *, ttl, fresh=False):
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(ttl) or ttl < 0:
            raise ValueError("cache TTL must be finite and nonnegative")
        key = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        bucket = hash(key) % len(self._triage)
        if fresh or ttl == 0:
            with self._lock:
                epoch = self._generation
                self._fresh_generations[bucket] += 1
                fresh_epoch = self._fresh_generations[bucket]
                old = self._entries.pop(key, None)
                if old:
                    self._bytes -= len(key) + len(old[0])
            value = fetch()
            encoded = (
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") if value is not None else None
            )
            with self._lock:
                if epoch != self._generation or fresh_epoch != self._fresh_generations[bucket]:
                    raise ReadInvalidated("source changed during a fresh read")
                if encoded is not None and ttl > 0:
                    self._publish(key, encoded, ttl)
            return (json.loads(encoded) if encoded is not None else None), False
        # A fixed lock stripe set coalesces identical reads without an unbounded
        # lock registry or serializing all independent repository operations.
        with self._triage[bucket]:
            now = self.clock()
            with self._lock:
                epoch = self._generation
                fresh_epoch = self._fresh_generations[bucket]
                for old_key, (saved, saved_at, expires) in list(self._entries.items()):
                    if now >= expires or now < saved_at:
                        self._bytes -= len(old_key) + len(saved)
                        del self._entries[old_key]
                item = self._entries.get(key)
                if item is not None:
                    if now - item[1] < ttl:
                        self._entries.move_to_end(key)
                        return json.loads(item[0]), True
                    self._bytes -= len(key) + len(item[0])
                    del self._entries[key]
            value = fetch()
            if value is None:
                return None, False
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            with self._lock:
                if epoch != self._generation or fresh_epoch != self._fresh_generations[bucket]:
                    raise ReadInvalidated("source changed during a triage read")
                self._publish(key, encoded, ttl)
            return json.loads(encoded), False

    def _publish(self, key, encoded, ttl):
        # Caller owns _lock. Successful fresh reads replace known stale triage
        # entries; an older in-flight read cannot overwrite that observation.
        size = len(key) + len(encoded)
        if size > self.max_bytes:
            return
        prior = self._entries.pop(key, None)
        if prior:
            self._bytes -= len(key) + len(prior[0])
        while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
            removed_key, (removed, _, _) = self._entries.popitem(last=False)
            self._bytes -= len(removed_key) + len(removed)
        saved_at = self.clock()
        self._entries[key] = (encoded, saved_at, saved_at + ttl)
        self._bytes += size

    def stats(self):
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes, "generation": self._generation}


class ForgejoReadSource:
    """Worker-shaped PR metadata and discussion reads on one qualified source.

    This is the review-state read adapter, not a full worker or CLI backend.
    Inline thread semantics, claims/reactions, authorship and writes remain
    separate adapters. None signals unavailable; [] is a verified empty read.
    """

    direct_source = True

    def __init__(self, repository, api, *, cache=None, ttl=15):
        from tauceti_tools.forge import RepositoryRef
        from tauceti_tools.forge_read import ForgejoReader

        if (
            not isinstance(repository, RepositoryRef)
            or repository.forge.kind != "forgejo"
            or api.forge != repository.forge
        ):
            raise ValueError("direct source requires a qualified repository on the supplied Forgejo origin")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(ttl) or not 0 <= ttl <= 300:
            raise ValueError("triage TTL must be finite and between zero and 300 seconds")
        self.repository, self.api = repository, api
        self.reader = ForgejoReader(api)
        self.cache = cache if cache is not None else BoundedReadCache()
        self.ttl = ttl
        self._credential = self._credential_identity()
        self._cache_namespace = hashlib.sha256((repository.key + "\0" + self._credential).encode()).hexdigest()
        self._failure = threading.local()

    @property
    def last_failure(self):
        return getattr(self._failure, "value", None)

    @property
    def cache_namespace(self):
        return self._cache_namespace

    def _credential_identity(self):
        # Permissions belong to a token, not merely to a login. A hash is used
        # only in private cache keys; no token or raw server diagnostic is saved.
        return hashlib.sha256((self.api.forge.key + "\0" + (self.api._token or "")).encode()).hexdigest()

    def _bound(self):
        if self.api.forge != self.repository.forge or self._credential_identity() != self._credential:
            raise SourceBindingChanged("source origin or credential changed; construct a new read client")

    def binding_current(self):
        try:
            self._bound()
            return True
        except SourceBindingChanged:
            self._failure.value = ReadFailure("binding", 0, "SourceBindingChanged")
            return False

    def invalidate(self):
        self.cache.invalidate()

    def _read(self, operation, args, fetch, *, fresh=False):
        from tauceti_tools.forge import ForgeError, Incomplete

        self._failure.value = None
        try:
            self._bound()
            force, revision = transport.read_policy()

            def bounded_fetch():
                value = fetch()
                self._bound()
                try:
                    if (
                        value is not None
                        and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > self.api.max_bytes
                    ):
                        raise Incomplete("direct source observation exceeded the configured byte bound")
                except (ValueError, TypeError):
                    raise Incomplete("direct source observation is not finite JSON") from None
                return value

            key = (self.cache_namespace, operation, json.dumps(args, sort_keys=True), revision)
            value, hit = self.cache.read(key, bounded_fetch, ttl=self.ttl, fresh=fresh or force)
            self._bound()
            if hit:
                transport.note_cache_hit()
            return value
        except ForgeError as error:
            self._failure.value = ReadFailure(operation, error.status, type(error).__name__)
            return None
        except ReadInvalidated:
            self._failure.value = ReadFailure(operation, 0, "ReadInvalidated")
            return None
        except SourceBindingChanged:
            self._failure.value = ReadFailure(operation, 0, "SourceBindingChanged")
            return None

    def pr_view(self, number, fields, *, fresh=False):
        return self._read(
            "pr_view", [number, fields], lambda: self.reader.pull(self.repository, number, fields), fresh=fresh
        )

    def issue_comments(self, number, *, fresh=False):
        self.repository.pull_url(number)

        # --paginate/--slurp joins all synthetic output pages; one complete
        # unpaginated native response supplies this discussion in Forgejo17.
        def fetch():
            from tauceti_tools.forge_api import execute, plan
            from tauceti_tools.forge_command import parse_command

            request = plan(
                parse_command(
                    ["api", "repos/" + self.repository.name + f"/issues/{number}/comments", "--paginate", "--slurp"]
                )
            )
            output, _, _ = execute(request, self.api)
            return [row for page in json.loads(output) for row in page]

        return self._read("issue_comments", [number], fetch, fresh=fresh)

    def reviews(self, number, *, fresh=False):
        self.repository.pull_url(number)

        def fetch():
            from tauceti_tools.forge_api import execute, plan
            from tauceti_tools.forge_command import parse_command

            request = plan(
                parse_command(
                    ["api", "repos/" + self.repository.name + f"/pulls/{number}/reviews", "--paginate", "--slurp"]
                )
            )
            output, _, _ = execute(request, self.api)
            return [row for page in json.loads(output) for row in page]

        return self._read("reviews", [number], fetch, fresh=fresh)

    def review_comments(self, number):
        from tauceti_tools.forge import Unsupported

        self.repository.pull_url(number)

        def unavailable():
            raise Unsupported("inline thread/reply identities need their source adapter")

        return self._read("review_comments", [number], unavailable, fresh=True)

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
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

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
                # Another process may have just made an empty namespace and be
                # about to open it for an atomic write. Leave recent ones alone.
                if time.time() - os.stat(directory, dir_fd=root_fd, follow_symlinks=False).st_mtime < 30:
                    continue
                os.rmdir(directory, dir_fd=root_fd)
            except OSError:
                pass
    except OSError:
        return
    finally:
        os.close(root_fd)


@dataclass
class _ReadSlot:
    users: int = 0
    epoch: int = 0
    latest: int = 0
    flight: Future | None = None


class BoundedReadCache:
    """Bound persistent JSON observations and coalesce exact triage requests.

    Active slots last only for running/waiting calls, with no persistent lock or
    generation registry. Fresh reads bypass coalescing. Only a write invalidates
    returned observations; the latest-started fetch wins cache publication.
    """

    def __init__(self, *, max_entries=128, max_bytes=4 * 1024 * 1024, clock=time.monotonic):
        if type(max_entries) is not int or type(max_bytes) is not int or min(max_entries, max_bytes) < 1:
            raise ValueError("cache bounds must be positive integers")
        self.max_entries, self.max_bytes, self.clock = max_entries, max_bytes, clock
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._active = {}
        self._sequence = 0
        self._generation = 0

    def invalidate(self, *, scope=None):
        self._validate_scope(scope)
        with self._lock:
            self._generation += 1
            for key, slot in self._active.items():
                if self._affected(key[0], scope):
                    slot.epoch += 1
                    if slot.flight is not None and not slot.flight.done():
                        slot.flight.set_exception(ReadInvalidated("source changed during a coalesced read"))
                    slot.flight = None
            for key in list(self._entries):
                if self._affected(key[0], scope):
                    self._drop(key)

    @staticmethod
    def _validate_scope(scope):
        if (
            scope is not None
            and not isinstance(scope, str)
            and not (
                isinstance(scope, tuple)
                and len(scope) == 2
                and isinstance(scope[0], str)
                and (scope[1] is None or (type(scope[1]) is int and scope[1] > 0))
            )
        ):
            raise ValueError("cache scope must be a string or a qualified repository/PR pair")

    @staticmethod
    def _affected(candidate, target):
        if target is None or candidate == target:
            return True
        # A PR write also invalidates lists, across isolated credential keys.
        return (
            isinstance(candidate, tuple)
            and isinstance(target, tuple)
            and candidate[0] == target[0]
            and (candidate[1] is None or target[1] is None)
        )

    @staticmethod
    def _key_bytes(key):
        return len(key[1]) + len(json.dumps(key[0], ensure_ascii=False).encode())

    def _drop(self, key):
        old = self._entries.pop(key)
        self._bytes -= self._key_bytes(key) + len(old[0])

    def read(self, key, fetch, *, ttl, fresh=False, scope=None):
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(ttl) or ttl < 0:
            raise ValueError("cache TTL must be finite and nonnegative")
        self._validate_scope(scope)
        key = (
            scope,
            json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
        )
        started = self.clock()
        with self._lock:
            for old_key, (_, saved_at, expires) in list(self._entries.items()):
                if started >= expires or started < saved_at:
                    self._drop(old_key)
            item = self._entries.get(key)
            if item is not None:
                if not fresh and ttl > 0 and started - item[1] < ttl:
                    self._entries.move_to_end(key)
                    return json.loads(item[0]), True
                self._drop(key)
            slot = self._active.setdefault(key, _ReadSlot())
            slot.users += 1
            epoch = slot.epoch
            future = slot.flight if not fresh and ttl > 0 else None
            leader = future is None
            if leader:
                self._sequence += 1
                sequence = slot.latest = self._sequence
                if not fresh and ttl > 0:
                    future = slot.flight = Future()
        try:
            if leader:
                value = fetch()
                encoded = (
                    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    if value is not None
                    else None
                )
            else:
                encoded = future.result()
            with self._lock:
                if epoch != slot.epoch:
                    raise ReadInvalidated("source changed during a read")
                if leader and encoded is not None and ttl > 0 and sequence == slot.latest:
                    self._publish(key, encoded, ttl, started)
                if leader and future is not None:
                    future.set_result(encoded)
            return (json.loads(encoded) if encoded is not None else None), not leader
        except BaseException as error:
            with self._lock:
                if leader and future is not None and not future.done():
                    future.set_exception(
                        error if isinstance(error, Exception) else ReadInvalidated("coalesced read leader interrupted")
                    )
            raise
        finally:
            with self._lock:
                if leader and future is not None and slot.flight is future:
                    slot.flight = None
                slot.users -= 1
                if not slot.users:
                    del self._active[key]

    def _publish(self, key, encoded, ttl, started):
        # Caller owns _lock. A slow request cannot extend the observation's TTL
        # or overwrite the result of a later-started fetch.
        size = self._key_bytes(key) + len(encoded)
        now = self.clock()
        if size > self.max_bytes or now < started or now - started >= ttl:
            return
        if key in self._entries:
            self._drop(key)
        while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
            self._drop(next(iter(self._entries)))
        self._entries[key] = (encoded, started, started + ttl)
        self._bytes += size

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "generation": self._generation,
                "active": len(self._active),
            }


class _ProjectionAPI:
    """Validate native pull identity before Tools projects requested fields."""

    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        return getattr(self.api, name)

    def pull(self, repository, number):
        from tauceti_tools.forge import Incomplete, Unsupported

        row = self.api.pull(repository, number)
        if not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] != number:
            raise Incomplete("PR projection did not identify its requested number")
        if not isinstance(row.get("html_url"), str):
            raise Incomplete("PR projection discussion URL is unavailable")
        parsed, expected = urlsplit(row["html_url"]), urlsplit(repository.pull_url(number))
        if (
            (parsed.scheme, parsed.netloc, parsed.path.casefold())
            != (expected.scheme, expected.netloc, expected.path.casefold())
            or parsed.query
            or parsed.fragment
        ):
            raise Incomplete("PR projection belongs to another source discussion")
        if type(row.get("flow")) is not int:
            raise Incomplete("PR projection flow is unavailable")
        if row["flow"] != 0:
            raise Unsupported("AGit PR projections require their source adapter")
        if (
            type(row.get("merged")) is not bool
            or row.get("state") not in {"open", "closed"}
            or (row["merged"] and row["state"] != "closed")
        ):
            raise Incomplete("PR projection lifecycle is unavailable or inconsistent")
        return row


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
        self.reader = ForgejoReader(_ProjectionAPI(api))
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

    def invalidate(self, number=None):
        if number is not None:
            self.repository.pull_url(number)
        self.cache.invalidate(scope=(self.repository.key, number))

    def _read(self, operation, args, fetch, *, fresh=False, number=None):
        from tauceti_tools.forge import ForgeError, Incomplete

        self._failure.value = None
        try:
            self._bound()
            force, revision = transport.read_policy()

            def bounded_fetch():
                try:
                    value = fetch()
                except (ValueError, TypeError, KeyError):
                    raise Incomplete("direct source response does not satisfy its read contract") from None
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
            value, hit = self.cache.read(
                key, bounded_fetch, ttl=self.ttl, fresh=fresh or force, scope=(self.repository.key, number)
            )
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
        self.repository.pull_url(number)
        if (
            not isinstance(fields, (list, tuple))
            or not fields
            or not all(isinstance(field, str) and field for field in fields)
        ):
            raise ValueError("PR projections require an explicit nonempty field list")
        # Typed projections require the requested fields' proofs, rather than
        # all historical head/stat proofs needed for a whole raw REST record.
        return self._read(
            "pr_view",
            [number, fields],
            lambda: self.reader.pull(self.repository, number, fields),
            fresh=fresh,
            number=number,
        )

    @staticmethod
    def _utc(value):
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("source timestamp has no UTC offset")
        return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

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
            rows = [row for page in json.loads(output) for row in page]
            for row in rows:
                for field in ("created_at", "updated_at"):
                    row[field] = self._utc(row[field])
            return rows

        return self._read("issue_comments", [number], fetch, fresh=fresh, number=number)

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
            rows = [row for page in json.loads(output) for row in page]
            for row in rows:
                for field in ("submitted_at", "updated_at"):
                    if row.get(field) is not None:
                        row[field] = self._utc(row[field])
            return rows

        return self._read("reviews", [number], fetch, fresh=fresh, number=number)

    def review_comments(self, number):
        self.repository.pull_url(number)
        if self.binding_current():
            self._failure.value = ReadFailure("review_comments", 0, "Unsupported")
        return None

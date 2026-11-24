"""Direct qualified source reads for Worker's review-state consumer.

The client selects no credentials, process environment, shared-service fallback or
mutation path. Its explicitly supplied Forgejo client stays bound for its life.
Production bootstrap still refuses unfinished non-GitHub worker configurations.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import github_transport as transport
from .constants import SOURCE_FACTS_TTL
from .read_cache import prune_read_cache as prune_state_cache  # noqa: F401


@dataclass(frozen=True)
class ReadFailure:
    operation: str
    status: int
    kind: str


class ReadInvalidated(RuntimeError):
    """An overlapping write invalidated this observation before it returned."""


class SourceBindingChanged(RuntimeError):
    """This client no longer holds the origin/credential it was constructed for."""


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

    def peek(self, key, *, ttl, scope=None):
        """A current entry, or CacheOnlyMiss. Never fetches, coalesces or publishes."""
        self._validate_scope(scope)
        key = (
            scope,
            json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
        )
        now = self.clock()
        with self._lock:
            item = self._entries.get(key)
            if item is None or ttl <= 0 or not 0 <= now - item[1] < ttl or now >= item[2]:
                raise transport.CacheOnlyMiss("no current local observation")
            self._entries.move_to_end(key)
            return json.loads(item[0])

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
        return self.validate_pull(self.api.pull(repository, number), repository, number)

    @staticmethod
    def validate_pull(row, repository, number):
        from tauceti_tools.forge import Incomplete, Unsupported

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

    def pulls(self, repository, *, page=None, sort=None, **filters):
        from urllib.parse import urlencode

        from tauceti_tools.forge import Incomplete, RepositoryRef

        if page is None and sort is None:
            rows = self.api.pulls(repository, **filters)
        else:
            # One native page of open PRs, for incremental discovery.
            if type(page) is not int or page < 1 or filters != {"state": "open"}:
                raise ValueError("a single listing page reads one positive page of open PRs")
            query = {"state": "open", **({"sort": sort} if sort else {}), "limit": 50, "page": page}
            rows = self.api.request("GET", f"repos/{repository.name}/pulls?{urlencode(query)}")
            if not isinstance(rows, list):
                raise Incomplete("source listing page is not an array")
        for row in rows:
            number = row.get("number") if isinstance(row, dict) else None
            if type(number) is not int or number < 1:
                raise Incomplete("source inventory has no PR identity")
            self.validate_pull(row, repository, number)
            if row["state"] != "open" or row["merged"]:
                raise Incomplete("source open inventory contains a closed PR")
            head = row.get("head")
            if not isinstance(head, dict) or "repo" not in head:
                raise Incomplete("source inventory head repository is unavailable")
            native = head["repo"]
            if native is not None:
                if not isinstance(native, dict) or type(native.get("id")) is not int or native["id"] < 1:
                    raise Incomplete("source head repository identity is unavailable")
                ref = RepositoryRef(repository.forge, native.get("full_name"))
                if not isinstance(native.get("html_url"), str):
                    raise Incomplete("source head repository URL is unavailable")
                parsed, expected = urlsplit(native["html_url"]), urlsplit(ref.url)
                if (
                    (parsed.scheme, parsed.netloc, parsed.path.casefold())
                    != (expected.scheme, expected.netloc, expected.path.casefold())
                    or parsed.query
                    or parsed.fragment
                ):
                    raise Incomplete("source head repository belongs to another origin")
        return rows


LISTING_PAGE_ATTEMPTS = 3
# Updates committed while a listing pages, and the issue index's lag behind the database.
RECENT_MARGIN_SECONDS = 120


def _timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unavailable_server(failure):
    return failure.kind == "ForgeError" and (failure.status in (0, 408, 429) or failure.status >= 500)


def _updated(row):
    """A listed PR's update time in seconds; 0 when it has none."""
    try:
        return datetime.fromisoformat(row["updatedAt"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def _resume(rows, since, started):
    """Where the next incremental refresh starts: the newest update listed, less the margin and the
    time this refresh took. None, forcing a full listing next time, when nothing says."""
    newest = max(map(_updated, rows), default=0.0)
    if not newest:
        return since
    resume = newest - RECENT_MARGIN_SECONDS - (time.time() - started)
    return resume if since is None else max(since, resume)


def _page_retrying(api, *, attempts=LISTING_PAGE_ATTEMPTS, sleep=time.sleep):
    """A view of a native client whose GET requests retry transient failures, page by page.

    A listing spans many pages; one 5xx or dropped connection should cost that page another
    request, not the whole listing (and every survey waiting on it). Malformed responses, client
    errors and every non-GET request keep their single attempt. Other clients are returned as is.
    """
    import copy

    from tauceti_tools.forge import ForgeError, Forgejo, Incomplete

    if not isinstance(api, Forgejo):
        return api
    view = copy.copy(api)
    request = api.request

    def retrying(method, path, *args, **kwargs):
        for attempt in range(attempts):
            try:
                return request(method, path, *args, **kwargs)
            except Incomplete:
                raise
            except ForgeError as error:
                transient = error.status in (0, 408, 429) or error.status >= 500
                if method.upper() != "GET" or not transient or attempt == attempts - 1:
                    raise
                sleep(2**attempt)

    view.request = retrying
    return view


class ForgejoReadSource:
    """Worker-shaped PR metadata and discussion reads on one qualified source.

    This supplies review-state and survey inventory reads, not a full worker or CLI backend.
    Inline thread semantics, claims/reactions, authoring and writes remain
    separate adapters. None signals unavailable; [] is a verified empty read.
    """

    direct_source = True
    inventory_fields = (
        "number",
        "title",
        "body",
        "headRefOid",
        "headRefName",
        "headRepository",
        "headRepositoryOwner",
        "isDraft",
        "mergeable",
        "author",
        "labels",
        "updatedAt",
    )

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
        if getattr(self, "shared_inventory", False) is True:
            from .source_identity import advisory_scope

            transport.invalidate_shared(advisory_scope(self, number))

    def invalidate_discovery(self):
        if getattr(self, "shared_inventory", False) is True:
            from .source_identity import discovery_scope

            transport.invalidate_shared(discovery_scope(self))

    def _read(self, operation, args, fetch, *, fresh=False, number=None, cacheable=True):
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
            cache_only = transport.cache_only()
            if cache_only and (fresh or force or not cacheable):
                raise transport.CacheOnlyMiss("uncached source read")
            shared = (
                getattr(self, "shared_inventory", False) is True
                and transport.advisory_reads()
                and not (fresh or force)
                and cacheable
                and number is not None
            )
            if shared:
                from .source_identity import advisory_scope

                shared_scope = advisory_scope(self, number)
                global_epoch = transport.shared_generation(advisory_scope(self, None))

                def fresh_fetch():
                    # Reusing nested cached observations would restart their
                    # age in this outer cache. Only the outermost read shares.
                    with transport.read_context(fresh=True, advisory=False):
                        return bounded_fetch()

                value = transport.shared_read(
                    [*shared_scope, operation, args, global_epoch],
                    fresh_fetch,
                    # A survey read names the PR's (updatedAt, head) as its revision, and
                    # shared_read serves only that revision, so the observation answers until
                    # the PR changes, a write invalidates it, or the edit/deletion backstop ends.
                    ttl=SOURCE_FACTS_TTL if revision else self.ttl,
                    identity=self.cache_namespace,
                    lock_timeout=300,
                    generation_scope=shared_scope,
                    coalesce_failures=True,
                )
                # A cached projection never survives a credential/origin change in this process.
                self._bound()
            elif cache_only:
                value, hit = self.cache.peek(key, ttl=self.ttl, scope=(self.repository.key, number)), True
            elif cacheable:
                value, hit = self.cache.read(
                    key, bounded_fetch, ttl=self.ttl, fresh=fresh or force, scope=(self.repository.key, number)
                )
            else:
                value, hit = bounded_fetch(), False
            self._bound()
            if not shared and hit:
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

    def refresh_pr_state(self, number, expected, observed=""):
        """Correct a selected observation using only this bound source.

        The fresh row replaces this PR's row in the shared listing (or removes a closed PR) instead
        of expiring the whole listing, which every surveying worker would then have to refetch.
        A row that cannot be placed falls back to expiring the listing.
        """
        self.invalidate(number)
        data = self.pr_view(number, [*self.inventory_fields, "state"], fresh=True)
        if data is not None and self._correct_discovery(number, data):
            return dict(status="ok", outcome="refreshed", data=data)
        self.invalidate_discovery()
        if data is None:
            data = self.pr_view(number, ["headRefOid"], fresh=True)
        if data is None:
            return dict(status="unavailable", reason="refresh_unavailable")
        return dict(status="ok", outcome="refreshed", data=data)

    def _correct_discovery(self, number, data):
        if getattr(self, "shared_inventory", False) is not True:
            return True  # No shared listing; this adapter's own entries were invalidated above.
        row = {key: data[key] for key in self.inventory_fields if key in data}
        if set(row) != set(self.inventory_fields) or data.get("state") not in {"OPEN", "CLOSED", "MERGED"}:
            return False
        from .source_identity import discovery_scope
        from .source_survey import discovery_key

        def mutate(value):
            rows = value["rows"]
            if not isinstance(rows, list):
                raise ValueError("cached listing is not a list")
            kept = [item for item in rows if not (isinstance(item, dict) and item.get("number") == number)]
            if data["state"] == "OPEN":
                # Native listings are creation-descending, which PR numbers follow.
                index = next((i for i, item in enumerate(kept) if item.get("number", 0) < number), len(kept))
                kept.insert(index, row)
            return {**value, "rows": kept}

        return transport.update_shared(
            discovery_key(self), mutate, identity=self.cache_namespace, generation_scope=discovery_scope(self)
        )

    def _listing_reader(self, api):
        if api is self.api:
            return self.reader
        from tauceti_tools.forge_read import ForgejoReader

        return ForgejoReader(_ProjectionAPI(api))

    def open_prs(self, *, fresh=False):
        """Complete bounded open inventory, excluding unverified CI observations.

        Exhaust native pagination. No per-PR history, status or diff hydration is
        needed. Concurrent changes can still move rows between pages; this is a
        short-lived discovery observation, not a source snapshot or merge proof.
        """
        fields = list(self.inventory_fields)

        def fetch():
            return self._listing_reader(_page_retrying(self.api)).pulls(self.repository, fields, state="open")

        return self._read("open_prs", [], fetch, fresh=fresh)

    def inventory(self, previous=None):
        """The open inventory as {"rows", "since", "sweep"}, brought up to date from `previous`.

        Forgejo renders every listed PR with git work (head, base and a diff stat), so listing all
        of about 1,300 open PRs costs the server minutes. Without a usable `previous` this is
        open_prs. Otherwise it reads PRs closed since `previous` from the issue index, which does no
        git work; the update-ordered listing until it reaches PRs older than that; and one page of
        the creation-ordered listing, a different page each time, so a change that moves no update
        time (mergeability after the base branch moves) is seen once the pages come round. When
        they do, open PRs missing from the issue index are dropped too. A refresh that fails for
        any reason but an unavailable server lists in full instead. None when unavailable.
        """
        started = time.time()

        def full():
            rows = self.open_prs(fresh=True)
            return None if rows is None else {"rows": rows, "since": _resume(rows, None, started), "sweep": 1}

        try:
            since, sweep, rows = previous["since"], previous["sweep"], previous["rows"]
            usable = (
                _timestamp(since)
                and type(sweep) is int
                and sweep >= 1
                and isinstance(rows, list)
                and all(isinstance(row, dict) and type(row.get("number")) is int for row in rows)
            )
        except (TypeError, KeyError):
            usable = False
        if not usable:
            return full()
        fields = list(self.inventory_fields)

        def fetch():
            from urllib.parse import urlencode

            from tauceti_tools.forge import Incomplete

            api = _page_retrying(self.api)
            reader = self._listing_reader(api)

            def issue_numbers(state, **query):
                path = f"repos/{self.repository.name}/issues?" + urlencode({"type": "pulls", "state": state, **query})
                numbers = set()
                for batch in api.pages(path):
                    for row in batch:
                        if (
                            not isinstance(row, dict)
                            or type(row.get("number")) is not int
                            or not row.get("pull_request")
                        ):
                            raise Incomplete("issue index did not identify a PR")
                        numbers.add(row["number"])
                return numbers

            current = {row["number"]: row for row in rows}
            stamp = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            for number in issue_numbers("closed", since=stamp):
                current.pop(number, None)
            changed, page = [], 1
            while True:
                batch = reader.pulls(self.repository, fields, state="open", sort="recentupdate", page=page)
                changed += batch
                if not batch or _updated(batch[-1]) < since:
                    break
                page += 1
            swept = reader.pulls(self.repository, fields, state="open", page=sweep)
            for row in changed + swept:
                current[row["number"]] = row
            if not swept:
                listed = issue_numbers("open")
                current = {n: row for n, row in current.items() if n in listed or _updated(row) >= since}
            return {
                "rows": sorted(current.values(), key=lambda row: row["number"], reverse=True),
                "since": _resume(changed, since, started),
                "sweep": sweep + 1 if swept else 1,
            }

        value = self._read("open_inventory", [], fetch, fresh=True, cacheable=False)
        failure = self.last_failure
        if value is None and failure is not None and not _unavailable_server(failure):
            # A malformed base or a rejected query would fail every later refresh too.
            return full()
        return value

    def authenticated_actor(self):
        """Fresh native actor identity; never infer it from a login or GitHub."""
        from .source_identity import SourceActor

        def fetch():
            self._bound()
            actor = SourceActor.from_native(self.repository.forge, self.api.request("GET", "user"))
            return {"id": int(actor.principal.native_id), "login": actor.login}

        row = self._read("authenticated_actor", [], fetch, fresh=True, cacheable=False)
        return None if row is None else SourceActor.from_native(self.repository.forge, row)

    def repository_access(self):
        """Fresh native actor and repository user ACL, without token-scope inference.

        The server still enforces token scopes and permissions on each mutation.
        This record alone grants no publication, approval or merge authority.
        """
        from .source_identity import SourceActor, SourceRepositoryAccess

        def fetch():
            self._bound()
            actor = SourceActor.from_native(self.repository.forge, self.api.request("GET", "user"))
            self._bound()
            row = self.api.request("GET", "repos/" + self.repository.name)
            access = SourceRepositoryAccess.from_native(self.repository, actor, row)
            return {
                "actor": {"id": int(actor.principal.native_id), "login": actor.login},
                "repository": {
                    "id": access.repository_id,
                    "full_name": self.repository.name,
                    "html_url": self.repository.url,
                    "permissions": {
                        "pull": access.user_code_can_read,
                        "push": access.user_code_can_push,
                        "admin": access.user_is_repo_admin,
                    },
                    "archived": access.archived,
                    "mirror": access.mirror,
                },
            }

        row = self._read("repository_access", [], fetch, fresh=True, cacheable=False)
        if row is None:
            return None
        return SourceRepositoryAccess.from_native(
            self.repository, SourceActor.from_native(self.repository.forge, row["actor"]), row["repository"]
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

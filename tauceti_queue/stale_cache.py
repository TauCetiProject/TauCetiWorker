"""A per-key cache that keeps serving its last answer while GitHub cannot give a new one.

`BearerAuth` (token digest to login) and the repository-admin predicate (login to bool) both ask
GitHub a question whose answer changes rarely but whose lookup shares the operator's user quota
with other tooling. A fresh answer is served for `ttl` seconds; after that the entry stays, and
when the lookup raises `GitHubUnavailable` (rate limit, 5xx, network error) the stale answer is
served instead, logged once per key per `log_interval` seconds. A lookup that returns None is a
definite negative (GitHub said the token is invalid): the entry is evicted.

With a `store` the answers are kept in its `identity_cache` table under `namespace` and loaded at
start, so a restart while GitHub is unavailable serves what GitHub confirmed before it. An answer
GitHub has not confirmed for `max_age` seconds is dropped; until then only a definite negative or
`invalidate` (an operator's revocation) removes it.

After a failed lookup the cache stops asking GitHub about keys it holds an answer for until the
rate limit's reset (the `rate limited for Ns` of the error, else `UNAVAILABLE_BACKOFF_SECONDS`),
so a request is answered from the cache instead of waiting on GitHub. `revalidate_due()` refreshes
the entries older than `ttl` whose lookup arguments this process has seen; the server runs it in
the background, so the request path rarely asks GitHub about a known key at all.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

from .ports import GitHubUnavailable

MAX_ENTRIES = 1024
LOG_INTERVAL_SECONDS = 3600.0
MAX_AGE_SECONDS = 7 * 86400.0
UNAVAILABLE_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0
_RATE_LIMITED_FOR = re.compile(r"rate limited for (\d+(?:\.\d+)?)s")


def _backoff(error: Exception) -> float:
    match = _RATE_LIMITED_FOR.search(str(error))
    if match is None:
        return UNAVAILABLE_BACKOFF_SECONDS
    return min(MAX_BACKOFF_SECONDS, max(UNAVAILABLE_BACKOFF_SECONDS, float(match.group(1))))


class StaleCache:
    def __init__(
        self,
        lookup: Callable[..., Any],
        *,
        ttl: float,
        clock: Callable[[], float],
        logger: logging.Logger,
        what: str,
        log_interval: float = LOG_INTERVAL_SECONDS,
        store: Any = None,
        namespace: str = "",
        max_age: float = MAX_AGE_SECONDS,
        decode: Callable[[Any], Any] | None = None,
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl
        self._clock = clock
        self._log = logger
        self._what = what
        self._log_interval = log_interval
        self._store = store if namespace else None
        self._namespace = namespace
        self._max_age = max_age
        # key -> (the time GitHub last confirmed the answer, the answer)
        self._entries: dict[str, tuple[float, Any]] = {}
        # key -> the lookup's arguments, kept in memory only (a bearer token is never stored)
        self._args: dict[str, tuple[Any, ...]] = {}
        self._logged: dict[str, float] = {}
        self._unavailable_until = 0.0
        self._last_error = ""
        self._lock = threading.Lock()
        if self._store is not None:
            now = clock()
            for row in self._store.cached_identities(namespace):
                if now - float(row["verified_at"]) >= max_age:
                    self._store.forget_identity(namespace, row["key"])
                    continue
                value = decode(row["value"]) if decode is not None else row["value"]
                self._entries[row["key"]] = (float(row["verified_at"]), value)

    # -- reads

    def get(self, key: str, *args: Any) -> Any:
        """The cached answer for `key`, refreshed through `lookup(*args)` once it is older than `ttl`.

        Returns None when the lookup answers None (and forgets the key). Raises `GitHubUnavailable`
        only when the lookup cannot answer and nothing usable is cached for `key`.
        """
        now = self._clock()
        with self._lock:
            self._args[key] = args
            cached = self._usable(key, now)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
            if cached is not None and now < self._unavailable_until:
                self._serve_stale(key, cached, now)
                return cached[1]
        try:
            value = self._lookup(*args)
        except GitHubUnavailable as error:
            with self._lock:
                self._note_unavailable(now, error)
                cached = self._usable(key, now)
                if cached is None:
                    raise
                self._serve_stale(key, cached, now)
                return cached[1]
        self._record(key, value, now)
        return value

    def revalidate_due(self) -> int:
        """Ask GitHub again about every entry older than `ttl` whose lookup arguments are known.

        Stops at the first failure (and backs off as `get` does); does nothing while backing off.
        Returns the number of entries GitHub answered for.
        """
        now = self._clock()
        with self._lock:
            if now < self._unavailable_until:
                return 0
            for key in [k for k in self._entries if self._usable(k, now) is None]:
                self._drop(key)
            due = [
                (key, self._args[key])
                for key, (verified_at, _) in sorted(self._entries.items(), key=lambda item: item[1][0])
                if now - verified_at >= self._ttl and key in self._args
            ]
        answered = 0
        for key, args in due:
            try:
                value = self._lookup(*args)
            except GitHubUnavailable as error:
                with self._lock:
                    self._note_unavailable(self._clock(), error)
                break
            with self._lock:
                revoked = key not in self._entries
            if not revoked:
                self._record(key, value, self._clock())
            answered += 1
        return answered

    def keys_where(self, predicate: Callable[[Any], bool]) -> list[str]:
        with self._lock:
            return [key for key, (_, value) in self._entries.items() if predicate(value)]

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            unavailable = now < self._unavailable_until
            return {
                "entries": len(self._entries),
                "github_unavailable": unavailable,
                "rate_limited": unavailable and "rate limit" in self._last_error.lower(),
                "retry_at": self._unavailable_until if unavailable else None,
                "last_error": self._last_error,
            }

    # -- writes

    def reset_backoff(self) -> None:
        """Ask GitHub again on the next lookup, as after the rate limit's reset."""
        with self._lock:
            self._unavailable_until = 0.0
            self._last_error = ""

    def invalidate(self, key: str) -> bool:
        """Forget `key`, so the next `get` asks GitHub again (a revocation, or a permission change
        the caller knows about). Returns whether an answer was cached."""
        with self._lock:
            had = key in self._entries
            self._drop(key)
            self._args.pop(key, None)
            return had

    # -- internals (callers hold `_lock`, except `_record`)

    def _usable(self, key: str, now: float) -> tuple[float, Any] | None:
        cached = self._entries.get(key)
        if cached is None or now - cached[0] >= self._max_age:
            return None
        return cached

    def _drop(self, key: str) -> None:
        self._entries.pop(key, None)
        self._logged.pop(key, None)
        if self._store is not None:
            self._store.forget_identity(self._namespace, key)

    def _note_unavailable(self, now: float, error: Exception) -> None:
        self._last_error = str(error)
        self._unavailable_until = max(self._unavailable_until, now + _backoff(error))

    def _serve_stale(self, key: str, cached: tuple[float, Any], now: float) -> None:
        last = self._logged.get(key, float("-inf"))
        if now - last >= self._log_interval:
            self._logged[key] = now
            self._log.warning(
                "%s lookup for %s unavailable (%s); serving the answer GitHub confirmed %.0fs ago",
                self._what,
                key[:12],
                self._last_error,
                now - cached[0],
            )

    def _record(self, key: str, value: Any, now: float) -> None:
        with self._lock:
            self._unavailable_until = 0.0
            if value is None:
                self._drop(key)
                self._args.pop(key, None)
                return
            self._entries[key] = (now, value)
            self._logged.pop(key, None)
            if self._store is not None:
                self._store.cache_identity(self._namespace, key, value, now)
            if len(self._entries) > MAX_ENTRIES:
                for stale_key in sorted(self._entries, key=lambda k: self._entries[k][0])[
                    : len(self._entries) - MAX_ENTRIES
                ]:
                    self._drop(stale_key)
                    self._args.pop(stale_key, None)

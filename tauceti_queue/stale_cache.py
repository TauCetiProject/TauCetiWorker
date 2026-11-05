"""A per-key cache that keeps serving its last answer while GitHub cannot give a new one.

`BearerAuth` (token digest to login) and the repository-admin predicate (login to bool) both ask
GitHub a question whose answer changes rarely but whose lookup shares the operator's user quota
with other tooling. A fresh answer is served for `ttl` seconds; after that the entry stays, and
when the lookup raises `GitHubUnavailable` (rate limit, 5xx, network error) the stale answer is
served instead, logged once per key per `log_interval` seconds. A lookup that returns None is a
definite negative (GitHub said the token is invalid): the entry is evicted.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .ports import GitHubUnavailable

MAX_ENTRIES = 1024
LOG_INTERVAL_SECONDS = 600.0


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
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl
        self._clock = clock
        self._log = logger
        self._what = what
        self._log_interval = log_interval
        self._entries: dict[str, tuple[float, Any]] = {}
        self._logged: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, key: str, *args: Any) -> Any:
        """The cached answer for `key`, refreshed through `lookup(*args)` once it is older than `ttl`.

        Returns None when the lookup answers None (and forgets the key). Raises `GitHubUnavailable`
        only when the lookup cannot answer and nothing was ever cached for `key`.
        """
        now = self._clock()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        try:
            value = self._lookup(*args)
        except GitHubUnavailable as error:
            with self._lock:
                cached = self._entries.get(key)
                if cached is None:
                    raise
                last = self._logged.get(key, float("-inf"))
                if now - last >= self._log_interval:
                    self._logged[key] = now
                    self._log.warning(
                        "%s lookup for %s failed (%s); serving the answer cached %.0fs ago",
                        self._what,
                        key[:12],
                        error,
                        now - (cached[0] - self._ttl),
                    )
                return cached[1]
        with self._lock:
            if value is None:
                self._entries.pop(key, None)
                self._logged.pop(key, None)
                return None
            self._entries[key] = (now + self._ttl, value)
            if len(self._entries) > MAX_ENTRIES:
                for stale_key in sorted(self._entries, key=lambda k: self._entries[k][0])[
                    : len(self._entries) - MAX_ENTRIES
                ]:
                    del self._entries[stale_key]
                    self._logged.pop(stale_key, None)
        return value

    def invalidate(self, key: str) -> None:
        """Forget `key`, so the next `get` asks GitHub again (a permission change the caller knows about)."""
        with self._lock:
            self._entries.pop(key, None)
            self._logged.pop(key, None)

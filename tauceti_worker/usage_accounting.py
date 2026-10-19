"""Content-free, crash-durable Claude usage evidence; also copied beside the shim.

Each response ID is a cumulative counter, not an increment. Checkpoints are atomic;
the append-only delta journal is a second recovery source, including after SIGKILL.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

TOKENS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)
MAX_CHECKPOINT = 16 * 1024**2


def price_usage(model, usage, at, prices=None):
    """Decimal list-price estimate using the vendored, dated price table."""
    table = prices or json.loads(Path(__file__).with_name("prices.json").read_text())["models"]
    if model not in table:
        candidates = [key for key in table if model and (key in model or model == key.removeprefix("claude-"))]
        if not candidates:
            return None
        model = max(candidates, key=len)
    day = datetime.fromtimestamp(at, UTC).date().isoformat()
    windows = [w for w in table[model] if w["effective"] <= day]
    if not windows:
        return None
    rates = max(windows, key=lambda w: w["effective"])
    if "cache_write_5m" not in rates:
        return None  # This journal describes native Claude, not other providers.

    def n(key):
        return Decimal(usage.get(key, 0))

    def rate(key):
        return Decimal(str(rates[key]))

    five, hour = n("cache_creation_5m_input_tokens"), n("cache_creation_1h_input_tokens")
    five += max(Decimal(0), n("cache_creation_input_tokens") - five - hour)
    return (
        n("input_tokens") * rate("input")
        + n("output_tokens") * rate("output")
        + n("cache_read_input_tokens") * rate("cache_read")
        + five * rate("cache_write_5m")
        + hour * rate("cache_write_1h")
    ) / 1_000_000


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise ValueError("unsafe or oversized usage evidence")
        return stream.read(limit + 1)


def _validate(value, identity):
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("id") != identity
        or not isinstance(value.get("messages"), dict)
        or type(value.get("inflight")) is not bool
        or type(value.get("closed")) is not bool
    ):
        raise ValueError("invalid usage checkpoint")
    for key in ("started", "observed_at"):
        if type(value.get(key)) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise ValueError("invalid usage timestamp")
    for key, item in value["messages"].items():
        if (
            not isinstance(key, str)
            or not isinstance(item, dict)
            or not isinstance(item.get("usage"), dict)
            or not isinstance(item.get("session"), str)
            or not isinstance(item.get("message_id"), str)
            or not isinstance(item.get("model"), str)
            or type(item.get("complete")) is not bool
            or type(item.get("at")) not in (int, float)
            or not math.isfinite(item["at"])
            or item["at"] < 0
        ):
            raise ValueError("invalid usage response")
        if any(k not in TOKENS or type(v) is not int or v < 0 for k, v in item["usage"].items()):
            raise ValueError("invalid usage counters")
        cost = item.get("cost")
        if cost is not None and (not isinstance(cost, str) or not Decimal(cost).is_finite() or Decimal(cost) < 0):
            raise ValueError("invalid usage price")
    return value


def read_usage(path):
    """Reject links, foreign files and malformed data. Absence is never zero spend."""
    path = Path(path)
    try:
        return _validate(json.loads(_read(path, MAX_CHECKPOINT)), path.stem)
    except (OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        pass
    try:
        state = None
        for raw in _read(path.with_suffix(".jsonl"), 128 * 1024**2).splitlines():
            try:
                record = json.loads(raw)
                if state is None:
                    candidate = record
                else:
                    candidate = {**state, **record, "messages": {**state["messages"], **record.get("messages", {})}}
                _validate(candidate, path.stem)
                state = candidate
            except (ValueError, TypeError, AttributeError, ArithmeticError):
                break  # A torn suffix cannot invalidate the preceding durable records.
        return _validate(state, path.stem)
    except (OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        return None


class UsageJournal:
    def __init__(self, directory, invocation, *, model, clock=time.time):
        if len(invocation) != 32 or any(c not in "0123456789abcdef" for c in invocation):
            raise ValueError("invalid usage invocation ID")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / (invocation + ".json")
        self.log = self.directory / (invocation + ".jsonl")
        self.clock = clock
        self.value = dict(
            version=1,
            id=invocation,
            model=model,
            started=clock(),
            observed_at=clock(),
            messages={},
            inflight=True,
            closed=False,
            sequence=0,
            prices_sha256=hashlib.sha256(Path(__file__).with_name("prices.json").read_bytes()).hexdigest(),
        )
        self.current = {}
        self.waiting = {"unknown"}
        self.dirty = set()
        self.flush()

    def observe(self, event):
        kind, changed = event.get("type"), False
        session = str(event.get("session_id") or "unknown")
        lane = session + ":" + str(event.get("parent_tool_use_id") or "root")
        e = event.get("event", {}) if kind == "stream_event" else event
        if not isinstance(e, dict):
            return
        ek = e.get("type")
        if session != "unknown" and (
            (kind == "system" and event.get("subtype") == "init")
            or (kind == "assistant" and not event.get("parent_tool_use_id") and not self.value.get("provider_session"))
        ):
            self.value["provider_session"] = session
            changed = True
        if kind == "user" or kind == "system" and event.get("subtype") == "api_retry":
            self.waiting.add(lane)
            changed = True
        message = event.get("message") if kind == "assistant" else e.get("message") if ek == "message_start" else None
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            key = session + ":" + message["id"]
            self.current[lane] = key
            item = self.value["messages"].setdefault(
                key,
                dict(
                    session=session,
                    message_id=message["id"],
                    model=message.get("model") or self.value["model"],
                    at=self.clock(),
                    usage={},
                    complete=False,
                ),
            )
            self._usage(item, message.get("usage") or {})
            if kind == "assistant":
                # Several content blocks may repeat the same response. Token
                # counters are merged by ID, never summed across those blocks.
                item["complete"] = "input_tokens" in item["usage"] and "output_tokens" in item["usage"]
                self.waiting.discard(lane)
                self.waiting.discard("unknown")
            else:
                self.waiting.add(lane)
            self.dirty.add(key)
            changed = True
        elif ek in ("message_delta", "message_stop") and lane in self.current:
            key = self.current[lane]
            item = self.value["messages"][key]
            self._usage(item, e.get("usage") or {})
            if ek == "message_stop":
                item["complete"] = "input_tokens" in item["usage"] and "output_tokens" in item["usage"]
                self.waiting.discard(lane)
                self.waiting.discard("unknown")
            self.dirty.add(key)
            changed = True
        if changed:
            self.value["inflight"] = bool(self.waiting)
            self.value["observed_at"] = self.clock()
            # Persist each usage change before forwarding it. No timer thread
            # or subsequent stdout event is needed to satisfy the 5s bound.
            self.flush()

    def _usage(self, item, usage):
        if not isinstance(usage, dict):
            return
        values = dict(usage)
        cache = usage.get("cache_creation")
        if isinstance(cache, dict):
            values.update(
                cache_creation_5m_input_tokens=cache.get("ephemeral_5m_input_tokens", 0),
                cache_creation_1h_input_tokens=cache.get("ephemeral_1h_input_tokens", 0),
            )
        for key, value in values.items():
            if key in TOKENS and type(value) is int and value >= 0:
                item["usage"][key] = max(item["usage"].get(key, 0), value)
        item["cost"] = (
            str(cost)
            if item["usage"] and (cost := price_usage(item["model"], item["usage"], item["at"])) is not None
            else None
        )

    def flush(self, *, closed=False):
        self.value["closed"] |= closed
        self.value["sequence"] += 1
        delta = {**self.value, "messages": {key: self.value["messages"][key] for key in self.dirty}}
        raw = (json.dumps(delta, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        raw = (json.dumps(self.value, separators=(",", ":")) + "\n").encode()
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.dirty.clear()

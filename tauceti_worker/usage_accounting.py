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
JOURNAL_FORMAT = 2
CHECKPOINT_SECONDS = 60
UTC = UTC


def price_usage(model, usage, at, prices=None):
    """Decimal list-price estimate using the vendored, dated price table."""
    table = prices or json.loads(Path(__file__).with_name("prices.json").read_text())["models"]
    if model not in table:
        candidates = [
            key for key in table if model and (key in model or model == (key[7:] if key.startswith("claude-") else key))
        ]
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


def _read(path, limit, *, allow_foreign=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_uid != os.getuid() and not allow_foreign) or info.st_size > limit:
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


def usage_path(directory, identity, bridge=None):
    namespace = Path(bridge).name if bridge else ""
    if len(namespace) == 32 and all(c in "0123456789abcdef" for c in namespace):
        return Path(directory) / "usage" / namespace / (identity + ".json")
    return Path(directory) / "usage" / (identity + ".json")


def session_usage(directory, session):
    path = usage_path(directory, session["id"], session.get("bridge"))
    external = bool(session.get("external") or session.get("lifecycle", {}).get("external"))
    return read_usage(path, allow_foreign=external) or (
        read_usage(Path(directory) / "usage" / (session["id"] + ".json"), allow_foreign=external)
        if path.parent.name != "usage"
        else None
    )


def _latest_sequence(path, *, allow_foreign=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_uid != os.getuid() and not allow_foreign):
                return 0
            offset = max(0, info.st_size - 65536)
            stream.seek(offset)
            lines = stream.read(65536).splitlines(keepends=True)
            if offset:
                lines = lines[1:]
            for raw in reversed(lines):
                if not raw.endswith(b"\n"):
                    continue
                try:
                    record = json.loads(raw)
                    sequence = record.get("sequence", 0)
                    if type(sequence) is int and sequence >= 0:
                        return sequence
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    return 0


def read_usage(path, *, allow_foreign=False):
    """Reject links and malformed data; only explicit remote bridges allow shifted file owners.

    Absence is never zero spend.
    """
    path = Path(path)
    checkpoint = None
    try:
        checkpoint = _validate(json.loads(_read(path, MAX_CHECKPOINT, allow_foreign=allow_foreign)), path.stem)
        if checkpoint.get("journal_format") == JOURNAL_FORMAT:
            value = _read_suffix(path, checkpoint, allow_foreign=allow_foreign)
            if value is not None:
                return value
        if checkpoint.get("sequence", 0) >= _latest_sequence(path.with_suffix(".jsonl"), allow_foreign=allow_foreign):
            return checkpoint
    except (OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        pass
    try:
        state = None
        for raw in _read(path.with_suffix(".jsonl"), 128 * 1024**2, allow_foreign=allow_foreign).splitlines():
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
        recovered = _validate(state, path.stem)
        return checkpoint if checkpoint and checkpoint.get("sequence", 0) > recovered.get("sequence", 0) else recovered
    except (OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        return checkpoint


def _read_suffix(path, checkpoint, *, allow_foreign=False):
    """Replay only bytes following a checkpoint's exact, verified journal prefix."""
    fd = os.open(path.with_suffix(".jsonl"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        offset = checkpoint.get("journal_offset", -1)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            and not allow_foreign
            or checkpoint.get("journal_identity") != [info.st_dev, info.st_ino]
            or type(offset) is not int
            or not 0 <= offset <= info.st_size
        ):
            return None
        stream.seek(max(0, offset - 256))
        if hashlib.sha256(stream.read(min(offset, 256))).hexdigest() != checkpoint.get("journal_anchor"):
            return None
        stream.seek(offset)
        value = checkpoint
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            try:
                record = json.loads(raw)
                if record.get("sequence") != value.get("sequence", 0) + 1:
                    break
                candidate = {**value, **record, "messages": {**value["messages"], **record.get("messages", {})}}
                value = _validate(candidate, path.stem)
                offset = stream.tell()
            except (ValueError, TypeError, AttributeError, ArithmeticError):
                break
        # A consumed offset, unlike a post-read stat, never includes unparsed bytes.
        stream.seek(max(0, offset - 256))
        return {
            **value,
            "journal_offset": offset,
            "journal_identity": [info.st_dev, info.st_ino],
            "journal_anchor": hashlib.sha256(stream.read(min(offset, 256))).hexdigest(),
        }


def read_summary(path, *, allow_foreign=False):
    """Small optional reporting cache. Never proof of complete expenditure."""
    try:
        try:
            raw = _read(Path(path).with_suffix(".summary"), 8192, allow_foreign=allow_foreign)
        except FileNotFoundError:
            # Accept summaries from the first live release while copied shims
            # finish naturally. New summaries do not match checkpoint *.json.
            raw = _read(Path(path).with_suffix(".summary.json"), 8192, allow_foreign=allow_foreign)
        value = json.loads(raw)
        if (
            value.get("version") != 1
            or value.get("id") != Path(path).stem
            or type(value.get("journal_offset")) is not int
            or type(value.get("journal_start_bytes")) is not int
            or type(value.get("sequence")) is not int
            or value["sequence"] < 0
            or value.get("journal_format") != JOURNAL_FORMAT
            or not 0 <= value["journal_start_bytes"] <= value["journal_offset"]
            or not isinstance(value.get("journal_identity"), list)
            or len(value["journal_identity"]) != 2
            or any(type(n) is not int or n < 0 for n in value["journal_identity"])
            or not isinstance(value.get("model"), str)
            or not isinstance(value.get("prices_sha256"), str)
            or type(value.get("usage_seen")) is not bool
            or not Decimal(value["cost"]).is_finite()
            or Decimal(value["cost"]) < 0
            or type(value.get("observed_at")) not in (int, float)
            or not math.isfinite(value["observed_at"])
        ):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
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
        self.prices = json.loads(Path(__file__).with_name("prices.json").read_text())["models"]
        self.total = Decimal(0)
        self.last_checkpoint = self.last_summary = float("-inf")
        self.summary_seen = False
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
            journal_format=JOURNAL_FORMAT,
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
            if ek == "message_start":
                item["streamed"] = True
            self._usage(item, message.get("usage") or {})
            if kind == "assistant":
                if not item.get("streamed"):
                    item["complete"] = (
                        "input_tokens" in item["usage"]
                        and "output_tokens" in item["usage"]
                        and not item.get("unpriced_extra")
                    )
                if item["complete"]:
                    self.waiting.discard(lane)
                    self.waiting.discard("unknown")
                else:
                    self.waiting.add(lane)
            else:
                self.waiting.add(lane)
            self.dirty.add(key)
            changed = True
        elif ek in ("message_delta", "message_stop") and lane in self.current:
            key = self.current[lane]
            item = self.value["messages"][key]
            self._usage(item, e.get("usage") or {})
            if ek == "message_stop":
                item["complete"] = (
                    "input_tokens" in item["usage"]
                    and "output_tokens" in item["usage"]
                    and not item.get("unpriced_extra")
                )
                self.waiting.discard(lane)
                self.waiting.discard("unknown")
            self.dirty.add(key)
            changed = True
        if changed:
            self.value["inflight"] = bool(self.waiting)
            self.value["observed_at"] = self.clock()
            # Persist each usage change before forwarding it. No timer thread
            # or subsequent stdout event is needed to satisfy the 5s bound.
            self.flush(checkpoint=False)

    def _usage(self, item, usage):
        if not isinstance(usage, dict):
            return
        values = dict(usage)
        fees = usage.get("server_tool_use")
        if (
            isinstance(fees, dict)
            and any(fees.values())
            or usage.get("service_tier") not in (None, "standard", "default")
        ):
            item["unpriced_extra"] = True
        cache = usage.get("cache_creation")
        if isinstance(cache, dict):
            values.update(
                cache_creation_5m_input_tokens=cache.get("ephemeral_5m_input_tokens", 0),
                cache_creation_1h_input_tokens=cache.get("ephemeral_1h_input_tokens", 0),
            )
        for key, value in values.items():
            if key in TOKENS and type(value) is int and value >= 0:
                item["usage"][key] = max(item["usage"].get(key, 0), value)
        previous = Decimal(item.get("cost") or "0")
        item["cost"] = (
            str(cost)
            if item["usage"]
            and (cost := price_usage(item["model"], item["usage"], item["at"], getattr(self, "prices", None)))
            is not None
            else None
        )
        self.total = getattr(self, "total", Decimal(0)) + Decimal(item["cost"] or "0") - previous

    def flush(self, *, closed=False, checkpoint=True):
        self.value["closed"] |= closed
        self.value["sequence"] += 1
        delta = {
            **{
                k: v for k, v in self.value.items() if k not in {"journal_offset", "journal_anchor", "journal_identity"}
            },
            "messages": {key: self.value["messages"][key] for key in self.dirty},
        }
        raw = (json.dumps(delta, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
        self.value.update(
            journal_identity=[info.st_dev, info.st_ino],
            journal_offset=info.st_size,
            journal_anchor=hashlib.sha256(raw[-256:]).hexdigest(),
        )
        self.value.setdefault("journal_start_bytes", info.st_size)
        self.dirty.clear()  # The delta is durable even if the optional views fail.
        now = self.clock()
        if checkpoint or closed or now - self.last_checkpoint >= CHECKPOINT_SECONDS:
            self._checkpoint()
            self.last_checkpoint = now
        seen = self.total > 0
        if checkpoint or closed or seen and not self.summary_seen or now - self.last_summary >= CHECKPOINT_SECONDS:
            # Rebuildable reporting metadata has no fsync barrier. Failure must
            # not mark durable accounting unhealthy or prevent clean closure.
            try:
                summary = {
                    k: self.value[k]
                    for k in (
                        "id",
                        "model",
                        "sequence",
                        "observed_at",
                        "inflight",
                        "closed",
                        "prices_sha256",
                        "journal_format",
                        "journal_start_bytes",
                        "journal_offset",
                        "journal_identity",
                    )
                }
                summary.update(
                    version=1,
                    cost=str(self.total),
                    usage_seen=seen,
                    provider_session=self.value.get("provider_session"),
                )
                self._replace(self.path.with_suffix(".summary"), summary, durable=False)
                self.last_summary, self.summary_seen = now, seen
            except OSError:
                pass

    def _checkpoint(self):
        self._replace(self.path, self.value, durable=True)
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _replace(path, value, *, durable):
        raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(tmp, path)
